"""
社内システムのCase Orders（Close依頼）を読み取り、対応する楽天・Yahoo出品を自動で停止する。

処理の流れ:
  1. Playwrightで app.jrcreators.com にログイン（Basic認証 + フォームログインの2段階）
  2. /case-orders?case_status_id=1&case_group_id[0]=4 で
     「Case Status = New」かつ「Case Group に Rakuten/Yahoo (Mkt) を含む」ケースを一覧取得
  3. そのうち Case Type が Close (Temporary) / Close (Permanent) のものだけを対象にする
  4. 各ケースの /case-orders/view/{id} を開き、Related Skus テーブルから
     Shop が「楽天」「Yahoo(new)」の行の Sku（＝各モールの商品コード）を集める
  5. 楽天: RMS API で hideItem=true（＝倉庫。2店舗とも試し、存在する店舗で実行）
     Yahoo: setStock API で quantity=0（＝在庫切れ。同上）
  6. そのケースのSKUが全て成功した場合のみ、/case-orders/edit/{id} で
     Case Status を In-Progress にし、Reply に「Rakuten/Yahoo Closed」を入れて保存する
     （1つでも失敗したら New のまま残し、次回の実行で再挑戦させる）
  7. 実行結果をスプレッドシートの「自動Close_ログ」タブに追記する

各モールの停止方式について:
  楽天は PATCH /es/2.0/items/manage-numbers/{商品管理番号} に {"hideItem": true} を送る方式。
  Yahooは editItem が全置換型（省略した項目がデフォルト値で上書きされる）で商品ページを
  壊す危険があるため使わず、在庫数だけを更新する setStock で在庫0にして注文を止める。
  そのため見え方は楽天＝ページ非公開、Yahoo＝在庫切れ表示と異なるが、注文を防ぐ目的は満たす。

Wowmaについて（2026-08-18追加）:
  Wowma!（現au PAYマーケット）のAPIは登録した固定IPからしか呼べないため、
  GitHub Actions（毎時の定期実行を含む、IPが毎回変わる）からは接続できない。
  そのため WOWMA_API_KEY/WOWMA_SHOP_ID を環境変数に設定したとき（GitHub Secretsには
  登録しない）だけ有効になり、固定IPを許可登録したPC上で手動実行したときだけ、
  楽天SKUに対応するWowma出品の在庫も0に更新する（stockCount=0を送るとWowma側が
  自動的に販売終了にしてくれる仕様）。GitHub Actionsの定期実行が先にケースを
  In-Progressにしてしまうケースの後追いは case_orders_wowma_close_catchup.py で行う。
"""

import os
import sys
import time
import json
import base64
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from xml.etree import ElementTree

import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))

# Wowmaは固定IPからしか呼べない（Wow!manager側のIP許可リスト制限）ため、
# GitHub Actions（Secrets未設定）では自然にスキップされ、
# WOWMA_API_KEY/WOWMA_SHOP_ID を設定したこのPC上で手動実行したときだけ動く。
WOWMA_ENABLED = bool(os.environ.get("WOWMA_API_KEY")) and bool(os.environ.get("WOWMA_SHOP_ID"))
if WOWMA_ENABLED:
    from case_orders_wowma import SHOP_WOWMA, wowma_get_item, wowma_update_stock

# ── 実行モード ────────────────────────────────────
# DRY_RUN=true の間は、対象の抽出とログ出力だけ行い、モール側もケース側も一切変更しない
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "20"))

# ── 社内システム ──────────────────────────────────
DOMAIN = "app.jrcreators.com"
BASE_URL = f"https://{DOMAIN}"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_URL = f"https://{quote(LOGIN_ID_1, safe='')}:{quote(LOGIN_PASS_1, safe='')}@{DOMAIN}/"

# 一覧の絞り込み。case_status_id=1 が New、case_group_id=4 が Rakuten/Yahoo (Mkt)
CASE_LIST_URL = f"{BASE_URL}/case-orders?case_status_id=1&case_group_id%5B0%5D=4"

CASE_STATUS_IN_PROGRESS = "2"  # Case Status の In-Progress
CASE_GROUP_RAKUTEN_YAHOO = "4"  # Case Group の Rakuten/Yahoo (Mkt)
TARGET_CASE_TYPES = ("Close (Temporary)", "Close (Permanent)")
REPLY_MESSAGE = "Rakuten/Yahoo Closed"
# 両モールとも該当出品が1件も見つからなかった場合のReply。
# すでに削除済みなら正しい結果だが、SKUのタイポや古いコードでも同じ見え方になるため、
# 「何も止めていない」ことがケース上で分かるようにしておく。
REPLY_MESSAGE_NOT_FOUND = "Rakuten/Yahoo Closed（該当出品なし）"

# 出品が見つからなかったときのメッセージ。処理結果の判定にも使う
RAKUTEN_NOT_FOUND = "楽天には出品が見つかりませんでした"
YAHOO_NOT_FOUND = "Yahooには出品が見つかりませんでした"

# Related Skus の Shop 列の表記
SHOP_RAKUTEN = "楽天"
SHOP_YAHOO = "Yahoo(new)"

# Yahooの商品コードは「楽天の商品管理番号 + 店舗ごとの接尾辞」という規則になっている。
# 社内システムのRelated Skusには売れたことのあるSKUしか登録されていないため、
# Yahooのコードが載っていないケースが多い。そこで楽天コードを基準に、この接尾辞を付けた
# コードが実在するかを getItem で1つずつ確かめる（シートに頼らないので常に最新）。
#
# 接尾辞は sku_suffix_survey.py で全出品データ（62,438件）を集計して洗い出したもの。
#   msy 27,477件 / akc 20,200件 / -ak 8,123件 / -msy 2,806件 / -akc 1,724件 / -mys 6件
# 店舗ごとに使う接尾辞は分かれている（akc系=American Kitchen、msy系=Meta Store）が、
# どの環境変数がどちらの店舗かを取り違えると取りこぼすため、両店舗で全パターンを試す。
# 1SKUあたり最大 6パターン×2店舗×2回(存在確認+更新) の呼び出しになるが、
# 1回の実行で扱うケース数は少ないため許容する。
YAHOO_SUFFIXES = ["", "msy", "akc", "-ak", "-msy", "-akc", "-mys"]

# ── 楽天 RMS API ──────────────────────────────────
RMS_BASE = "https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers"
RAKUTEN_STORES = [
    {
        "name": os.environ["RAKUTEN_SHOP_NAME_1"],
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_1"],
    },
    {
        "name": os.environ["RAKUTEN_SHOP_NAME_2"],
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_2"],
    },
]

# ── Yahoo API ─────────────────────────────────────
YAHOO_CLIENT_ID = os.environ["YAHOO_CLIENT_ID"]
YAHOO_CLIENT_SECRET = os.environ["YAHOO_CLIENT_SECRET"]
YAHOO_TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
YAHOO_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
YAHOO_STORES = [
    {"name": os.environ["YAHOO_SHOP_NAME_1"], "seller_id": os.environ["YAHOO_SELLER_ID_1"]},
    {"name": os.environ["YAHOO_SHOP_NAME_2"], "seller_id": os.environ["YAHOO_SELLER_ID_2"]},
]

# ── スプレッドシート ──────────────────────────────
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
CONFIG_SHEET_NAME = "Yahoo_Config"
LOG_SHEET_NAME = "自動Close_ログ"
LOG_HEADER = ["実行日時(JST)", "ケースID", "Case Type", "モール", "店舗", "商品コード", "結果"]

API_INTERVAL = 1.0  # 各モールAPIのレート制限対策（秒）


# ══ スプレッドシート ══════════════════════════════
def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def append_log(spreadsheet, rows: list):
    if not rows:
        return
    try:
        ws = spreadsheet.worksheet(LOG_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_HEADER))
        ws.append_row(LOG_HEADER)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


# ══ Yahoo 認証（yahoo_listing_sync.py と同じ仕組み） ══
def load_refresh_token(spreadsheet) -> str:
    ws = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    for row in ws.get_all_values():
        if row and row[0] == "refresh_token":
            return row[1]
    raise RuntimeError(f"「{CONFIG_SHEET_NAME}」タブに refresh_token が見つかりません。")


def save_refresh_token(spreadsheet, new_token: str):
    ws = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_token]])
            return
    ws.append_row(["refresh_token", new_token])


def get_yahoo_access_token(spreadsheet) -> str:
    current = load_refresh_token(spreadsheet)
    res = requests.post(
        YAHOO_TOKEN_URL,
        auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": current},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Yahooアクセストークン更新失敗（status={res.status_code}）: {res.text[:300]}")
    data = res.json()
    # リフレッシュトークンは使うたびにローテーションされるので、すぐ保存する
    save_refresh_token(spreadsheet, data.get("refresh_token", current))
    return data["access_token"]


# ══ 楽天：商品を倉庫に入れる ══════════════════════
def rakuten_auth_headers(store: dict) -> dict:
    token = base64.b64encode(
        f"{store['service_secret']}:{store['license_key']}".encode()
    ).decode()
    return {
        "Authorization": f"ESA {token}",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }


def rakuten_get_current_prices(manage_number: str) -> dict:
    """
    2店舗の現在価格を読むだけ（変更しない）。戻り値は {店舗名: 価格 or None}。
    価格調整後の検証（実際に書き込まれた値の確認）に使う。
    """
    prices = {}
    for store in RAKUTEN_STORES:
        headers = rakuten_auth_headers(store)
        try:
            res = requests.get(f"{RMS_BASE}/{manage_number}", headers=headers, timeout=30)
        except Exception:
            continue
        finally:
            time.sleep(API_INTERVAL)
        if res.status_code != 200:
            continue
        variants = (res.json().get("variants") or {})
        for variant in variants.values():
            price = variant.get("standardPrice")
            try:
                prices[store["name"]] = int(float(price)) if price is not None else None
            except (TypeError, ValueError):
                prices[store["name"]] = None
            break  # 単一バリアント想定。複数ある場合は最初の1つで代表する
    return prices


def rakuten_hide(manage_number: str) -> list:
    """
    2店舗を順に確認し、存在する店舗すべてで hideItem=true にする。
    戻り値は [(店舗名, 結果文字列, 成功したか)] のリスト。

    どちらの店舗にも存在しない場合は失敗にしない。Related Skus に楽天として
    登録されていても、すでに楽天から削除済みの商品が実在するため
    （＝止めるべき出品が無いだけで異常ではない）。ログには残す。
    """
    results = []
    for store in RAKUTEN_STORES:
        headers = rakuten_auth_headers(store)
        url = f"{RMS_BASE}/{manage_number}"

        try:
            res = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            results.append((store["name"], f"取得エラー: {e}", False))
            continue
        finally:
            time.sleep(API_INTERVAL)

        if res.status_code == 404:
            continue  # この店舗には存在しない
        if res.status_code >= 400:
            results.append((store["name"], f"取得エラー({res.status_code})", False))
            continue

        if res.json().get("hideItem") is True:
            results.append((store["name"], "すでに倉庫", True))
            continue

        if DRY_RUN:
            results.append((store["name"], "【DRY RUN】倉庫に入れる対象", True))
            continue

        try:
            patch = requests.patch(url, headers=headers, json={"hideItem": True}, timeout=30)
            if patch.status_code == 204:
                results.append((store["name"], "倉庫に入れました", True))
            else:
                results.append((store["name"], f"停止失敗({patch.status_code}) {patch.text[:100]}", False))
        except Exception as e:
            results.append((store["name"], f"停止エラー: {e}", False))
        finally:
            time.sleep(API_INTERVAL)

    if not results:
        results.append(("-", RAKUTEN_NOT_FOUND, True))
    return results


def rakuten_reopen(manage_number: str) -> list:
    """
    rakuten_hideの逆。2店舗を順に確認し、hideItem=trueになっている店舗だけ
    hideItem=falseに戻す（＝販売再開）。戻り値の形式はrakuten_hideと同じ。
    """
    results = []
    for store in RAKUTEN_STORES:
        headers = rakuten_auth_headers(store)
        url = f"{RMS_BASE}/{manage_number}"

        try:
            res = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            results.append((store["name"], f"取得エラー: {e}", False))
            continue
        finally:
            time.sleep(API_INTERVAL)

        if res.status_code == 404:
            continue  # この店舗には存在しない
        if res.status_code >= 400:
            results.append((store["name"], f"取得エラー({res.status_code})", False))
            continue

        if res.json().get("hideItem") is not True:
            results.append((store["name"], "すでに公開中", True))
            continue

        if DRY_RUN:
            results.append((store["name"], "【DRY RUN】販売再開の対象", True))
            continue

        try:
            patch = requests.patch(url, headers=headers, json={"hideItem": False}, timeout=30)
            if patch.status_code == 204:
                results.append((store["name"], "販売を再開しました", True))
            else:
                results.append((store["name"], f"再開失敗({patch.status_code}) {patch.text[:100]}", False))
        except Exception as e:
            results.append((store["name"], f"再開エラー: {e}", False))
        finally:
            time.sleep(API_INTERVAL)

    if not results:
        results.append(("-", RAKUTEN_NOT_FOUND, True))
    return results


def strip_yahoo_suffix(item_code: str) -> str:
    """
    Yahooの商品コードから接尾辞を外して、楽天の商品管理番号（基準コード）を推測する。
    例: 13000504-ak → 13000504
    長い接尾辞から順に照合する（-akc を akc と誤って切らないため）。
    """
    for suffix in sorted([s for s in YAHOO_SUFFIXES if s], key=len, reverse=True):
        if item_code.endswith(suffix):
            return item_code[: -len(suffix)]
    return item_code


# ══ Yahoo：在庫を0にする ══════════════════════════
def yahoo_get_item(token: str, store: dict, item_code: str):
    """存在しない場合は None を返す（Yahooは 400 + it-05002 が返る）"""
    res = requests.get(
        f"{YAHOO_BASE}/getItem",
        headers={"Authorization": f"Bearer {token}"},
        params={"seller_id": store["seller_id"], "item_code": item_code},
        timeout=30,
    )
    if res.status_code >= 400:
        if "it-05002" in res.text:
            return None
        raise RuntimeError(f"getItem エラー({res.status_code}): {res.text[:200]}")

    root = ElementTree.fromstring(res.content)
    fields = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        if text and tag not in fields:
            fields[tag] = text
    return fields


def yahoo_close(token: str, candidates: list) -> list:
    """
    候補の商品コードを2店舗それぞれで実在確認し、見つかったものすべての在庫を0にする。

    candidates は「楽天コード + 接尾辞」で組み立てた候補、または
    Related Skus に直接載っていたYahooコード。存在しないものは静かに読み飛ばす。
    """
    results = []
    found_any = False

    for store in YAHOO_STORES:
        for item_code in candidates:
            try:
                item = yahoo_get_item(token, store, item_code)
            except Exception as e:
                results.append((store["name"], f"{item_code}: 取得エラー: {e}", False))
                continue
            finally:
                time.sleep(API_INTERVAL)

            if item is None:
                continue  # この店舗にこのコードは無い（候補の外れ。正常）

            found_any = True

            if item.get("Quantity") == "0":
                results.append((store["name"], f"{item_code}: すでに在庫0", True))
                continue

            if DRY_RUN:
                results.append((store["name"], f"{item_code}: 【DRY RUN】在庫{item.get('Quantity')}→0の対象", True))
                continue

            try:
                res = requests.post(
                    f"{YAHOO_BASE}/setStock",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"seller_id": store["seller_id"], "item_code": item_code, "quantity": "0"},
                    timeout=30,
                )
                if res.status_code < 400:
                    results.append((store["name"], f"{item_code}: 在庫{item.get('Quantity')}→0にしました", True))
                else:
                    results.append((store["name"], f"{item_code}: 在庫更新失敗({res.status_code})", False))
            except Exception as e:
                results.append((store["name"], f"{item_code}: 在庫更新エラー: {e}", False))
            finally:
                time.sleep(API_INTERVAL)

    if not found_any:
        # Yahooに出品が無い商品も普通にあるため、失敗ではなく情報として扱う
        results.append(("-", YAHOO_NOT_FOUND, True))
    return results


def yahoo_restock(token: str, candidates: list, quantity: int = 100) -> list:
    """
    yahoo_closeの逆。候補の商品コードを2店舗それぞれで実在確認し、
    在庫が0になっているものだけ指定数量（既定100）に戻す。
    """
    results = []
    found_any = False

    for store in YAHOO_STORES:
        for item_code in candidates:
            try:
                item = yahoo_get_item(token, store, item_code)
            except Exception as e:
                results.append((store["name"], f"{item_code}: 取得エラー: {e}", False))
                continue
            finally:
                time.sleep(API_INTERVAL)

            if item is None:
                continue  # この店舗にこのコードは無い（候補の外れ。正常）

            found_any = True

            if item.get("Quantity") != "0":
                results.append((store["name"], f"{item_code}: 在庫{item.get('Quantity')}のため対象外", True))
                continue

            if DRY_RUN:
                results.append((store["name"], f"{item_code}: 【DRY RUN】在庫0→{quantity}の対象", True))
                continue

            try:
                res = requests.post(
                    f"{YAHOO_BASE}/setStock",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"seller_id": store["seller_id"], "item_code": item_code, "quantity": str(quantity)},
                    timeout=30,
                )
                if res.status_code < 400:
                    results.append((store["name"], f"{item_code}: 在庫0→{quantity}にしました", True))
                else:
                    results.append((store["name"], f"{item_code}: 在庫更新失敗({res.status_code})", False))
            except Exception as e:
                results.append((store["name"], f"{item_code}: 在庫更新エラー: {e}", False))
            finally:
                time.sleep(API_INTERVAL)

    if not found_any:
        results.append(("-", YAHOO_NOT_FOUND, True))
    return results


# ══ 社内システム（Playwright） ════════════════════
def login(page):
    print("社内システムにログイン中...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


def fetch_target_cases(page, case_types=TARGET_CASE_TYPES, label="Close系") -> list:
    """New かつ Rakuten/Yahoo のケースのうち、指定した Case Type のものを返す"""
    page.goto(CASE_LIST_URL, wait_until="networkidle")
    page.wait_for_timeout(500)

    # ページ内に複数のtableがある（ヘッダーのアラート用など）ため、
    # 「Case Type」列を持つテーブルを一覧として選ぶ
    info = page.evaluate(
        """() => {
            const tables = [...document.querySelectorAll('table')].map((t, i) => ({
                i,
                headers: [...t.querySelectorAll('th')].map(th => th.textContent.trim()),
                rowCount: t.querySelectorAll('tbody tr').length,
            }));
            const target = [...document.querySelectorAll('table')].find(
                t => [...t.querySelectorAll('th')].some(th => th.textContent.trim() === 'Case Type')
            );
            if (!target) return { tables, rows: null };

            const headers = [...target.querySelectorAll('th')].map(th => th.textContent.trim());
            const idxId = headers.indexOf('Id');
            const idxType = headers.indexOf('Case Type');
            const idxProduct = headers.indexOf('Product');
            const rows = [...target.querySelectorAll('tbody tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                return {
                    id: (tds[idxId] || '').replace(/,/g, ''),
                    caseType: tds[idxType] || '',
                    product: tds[idxProduct] || '',
                    raw: tds.join(' | '),
                };
            });
            return { tables, headers, rows, bodyText: document.body.innerText.slice(0, 1500) };
        }"""
    )

    if not info.get("rows"):
        print("！ケース一覧のテーブルが見つかりませんでした。ページ内のテーブル構成:")
        for t in info.get("tables", []):
            print(f"  table[{t['i']}] rows={t['rowCount']} headers={t['headers']}")
        print(f"  ページ本文の先頭:\n{info.get('bodyText', '')}")
        try:
            page.screenshot(path="debug_case_list.png", full_page=True)
        except Exception:
            pass
        return []

    rows = info["rows"]
    print(f"一覧のヘッダー: {info.get('headers')}")
    print(f"読み取れた行数: {len(rows)}")
    for r in rows:
        print(f"  {r['id']} | Type={r['caseType']} | {r['raw'][:160]}")

    targets = [r for r in rows if r["caseType"] in case_types and r["id"].isdigit()]
    print(f"うち{label}: {len(targets)}件")
    return targets


def fetch_case_skus(page, case_id: str) -> list:
    """
    ケース詳細ページの Related Skus から、楽天・Yahooの商品コードを取り出す。
    戻り値は [{"mall": "楽天"|"Yahoo(new)", "sku": "..."}]
    """
    page.goto(f"{BASE_URL}/case-orders/view/{case_id}", wait_until="networkidle")
    page.wait_for_timeout(300)

    # 一覧と同じく、列名（Sku / Shop）を持つテーブルを探して特定する。
    # 見出し「Related Skus」からの相対位置に頼るとマークアップ変更で壊れやすい。
    info = page.evaluate(
        """() => {
            const tables = [...document.querySelectorAll('table')];
            const target = tables.find(t => {
                const hs = [...t.querySelectorAll('th')].map(th => th.textContent.trim());
                return hs.includes('Sku') && hs.includes('Shop');
            });
            if (!target) {
                return {
                    rows: null,
                    tables: tables.map((t, i) => ({
                        i,
                        headers: [...t.querySelectorAll('th')].map(th => th.textContent.trim()),
                        rowCount: t.querySelectorAll('tbody tr').length,
                    })),
                };
            }
            const headers = [...target.querySelectorAll('th')].map(th => th.textContent.trim());
            const idxSku = headers.indexOf('Sku');
            const idxShop = headers.indexOf('Shop');
            const rows = [...target.querySelectorAll('tbody tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                return { mall: tds[idxShop] || '', sku: tds[idxSku] || '' };
            }).filter(r => r.sku);
            return { headers, rows };
        }"""
    )

    if info.get("rows") is None:
        print("  ！Related Skus のテーブルが見つかりません。ページ内のテーブル構成:")
        for t in info.get("tables", []):
            print(f"    table[{t['i']}] rows={t['rowCount']} headers={t['headers']}")
        return []

    rows = info["rows"]
    print(f"  Related Skus: {len(rows)}件 → {[(r['mall'], r['sku']) for r in rows]}")
    return rows


def update_case(page, case_id: str, reply_message: str) -> str:
    """
    楽天・Yahooの対応が終わったことをケースに反映する。戻り値は行った内容の説明。

    運用ルール（ヒアリング内容）:
      - 担当店舗の対応が終わったら、Case Groups から自分の店舗を外す
      - 他の担当（Amazon / CA mart など）がまだ残っている場合、Status は New のまま。
        勝手に In-Progress にすると、Newで未対応案件を探している他の担当者の
        リストから消えてしまうため
      - Case Groups が Rakuten/Yahoo だけだった場合は、外す相手がいないので
        Status を In-Progress にする
    """
    page.goto(f"{BASE_URL}/case-orders/edit/{case_id}", wait_until="networkidle")
    page.wait_for_timeout(300)

    groups = page.evaluate(
        """() => [...document.querySelectorAll('#case-groups-ids option')]
                 .filter(o => o.selected)
                 .map(o => ({ value: o.value, text: o.textContent.trim() }))"""
    )
    print(f"    現在のCase Groups: {[g['text'] for g in groups]}")

    remaining = [g for g in groups if g["value"] != CASE_GROUP_RAKUTEN_YAHOO]

    if remaining:
        # 「Assigned To」欄は select2 のタグUI。人と同じくチップの × をクリックして外す
        # （裏の <select> をJSで書き換える方法だと、select2の内部状態と食い違う恐れがある）
        removed = page.evaluate(
            """() => {
                const chips = [...document.querySelectorAll('.select2-selection__choice')];
                const target = chips.find(c => c.textContent.includes('Rakuten/Yahoo'));
                if (!target) return false;
                const x = target.querySelector('.select2-selection__choice__remove');
                if (!x) return false;
                x.click();
                return true;
            }"""
        )
        if not removed:
            raise RuntimeError("Assigned To から Rakuten/Yahoo を外せませんでした（チップが見つかりません）")

        page.wait_for_timeout(300)
        still_selected = page.evaluate(
            """() => [...document.querySelectorAll('#case-groups-ids option')]
                     .filter(o => o.selected).map(o => o.value)"""
        )
        if CASE_GROUP_RAKUTEN_YAHOO in still_selected:
            raise RuntimeError("Rakuten/Yahoo が選択されたままです（select2の操作が効いていません）")

        action = f"Assigned ToからRakuten/Yahooを外しました（残り: {[g['text'] for g in remaining]}／StatusはNewのまま）"
    else:
        page.select_option("#case-status-id", CASE_STATUS_IN_PROGRESS)
        action = "Case GroupsがRakuten/Yahooのみのため、Statusを In-Progress にしました"

    # Replyのtextareaは case-order-replies-{n}-message という形でnが可変
    page.fill('textarea[id^="case-order-replies-"][id$="-message"]', reply_message)
    page.click('form[action*="/case-orders/edit/"] button[type="submit"]')
    page.wait_for_load_state("networkidle")

    # 保存後のページに出るメッセージを控えておく（失敗時の手がかりになる）
    flash = page.evaluate(
        """() => {
            const el = document.querySelector('.message, .alert, .flash, .error-message');
            return el ? el.textContent.trim().slice(0, 200) : '';
        }"""
    )
    if flash:
        print(f"    保存後のメッセージ: {flash}")

    # 本当に保存されたかを確認する。押しただけで確認しないと、
    # 失敗しているのに「完了」と記録してしまう（実際にそれで取りこぼした）
    page.goto(f"{BASE_URL}/case-orders/edit/{case_id}", wait_until="networkidle")
    page.wait_for_timeout(300)

    saved = page.evaluate(
        """() => ({
            groups: [...document.querySelectorAll('#case-groups-ids option')]
                    .filter(o => o.selected).map(o => o.value),
            status: document.querySelector('#case-status-id')?.value,
            body: document.body.innerText,
        })"""
    )
    print(f"    保存後の状態: Groups={saved['groups']} / Status={saved['status']}")

    if reply_message not in saved["body"]:
        raise RuntimeError(f"保存されていません（Replyが見当たりません）。URL={page.url}")
    if remaining and CASE_GROUP_RAKUTEN_YAHOO in saved["groups"]:
        raise RuntimeError("保存されていません（Rakuten/Yahooが残ったままです）")
    if not remaining and saved["status"] != CASE_STATUS_IN_PROGRESS:
        raise RuntimeError("保存されていません（StatusがIn-Progressになっていません）")

    return action


# ══ メイン ════════════════════════════════════════
def main():
    print("=== Case Orders 自動Close 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：モール側もケース側も一切変更しません")

    spreadsheet = get_spreadsheet()
    yahoo_token = get_yahoo_access_token(spreadsheet)
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        try:
            login(page)
            cases = fetch_target_cases(page)
        except Exception as e:
            print(f"ケース一覧の取得に失敗しました: {e}")
            browser.close()
            sys.exit(1)

        if not cases:
            print("対象ケースなし。終了。")
            browser.close()
            return

        for case in cases[:MAX_PER_RUN]:
            case_id = case["id"]
            print(f"\n--- ケース {case_id}（{case['caseType']} / {case['product']}） ---")

            try:
                skus = fetch_case_skus(page, case_id)
            except Exception as e:
                print(f"  Related Skus の取得に失敗: {e}")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", f"SKU取得失敗: {e}"])
                continue

            targets = [s for s in skus if s["mall"] in (SHOP_RAKUTEN, SHOP_YAHOO)]
            if not targets:
                # 楽天/YahooのSKUが無いケースは触らず New のまま残し、人に見てもらう
                print("  楽天・YahooのSKUがありません。Newのまま残します。")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", "対象SKUなし（Newのまま）"])
                continue

            rakuten_skus = [t["sku"] for t in targets if t["mall"] == SHOP_RAKUTEN]
            yahoo_skus = [t["sku"] for t in targets if t["mall"] == SHOP_YAHOO]
            print(f"  楽天SKU: {rakuten_skus} / YahooSKU: {yahoo_skus}")

            all_ok = True
            # 実際に出品が見つかったか。1件も見つからなければ、削除済みなのか
            # SKUが古い・間違っているのか区別できないため、Replyで分かるようにする
            found_any = False

            # Yahooコードしか載っていないケースに備えて、接尾辞を外して楽天コードを推測する
            # （例: 13000504-ak → 13000504）。推測が外れて存在しなくても失敗扱いにしない。
            derived_bases = []
            for ysku in yahoo_skus:
                base = strip_yahoo_suffix(ysku)
                if base and base not in rakuten_skus and base not in derived_bases:
                    derived_bases.append(base)
            if derived_bases:
                print(f"  Yahooコードから推測した楽天コード: {derived_bases}")

            # 楽天：Related Skus に載っているコードと、上で推測したコード
            for sku in rakuten_skus:
                for store_name, message, ok in rakuten_hide(sku):
                    print(f"    [楽天] {sku} / {store_name}: {message}")
                    log_rows.append([now, case_id, case["caseType"], "楽天", store_name, sku, message])
                    if message != RAKUTEN_NOT_FOUND:
                        found_any = True
                    if not ok:
                        all_ok = False
            for sku in derived_bases:
                for store_name, message, ok in rakuten_hide(sku):
                    print(f"    [楽天(推測)] {sku} / {store_name}: {message}")
                    log_rows.append([now, case_id, case["caseType"], "楽天(推測)", store_name, sku, message])
                    if message != RAKUTEN_NOT_FOUND:
                        found_any = True
                    if not ok:
                        all_ok = False

            # Yahoo：直接載っているコードに加えて、楽天コード+接尾辞の候補も試す
            # （Related Skus には売れたことのあるSKUしか無いため、Yahooのコードは
            #   載っていないことが多い。楽天コードを基準に組み立てて実在確認する）
            # Related Skus では楽天が小文字・Yahooが大文字ということがあるが
            # （例: 楽天 ye8332620 / Yahoo YE8332620akc）、YahooのgetItemは
            # 大文字小文字を区別しないことを実機で確認済みなので、片方だけ試せばよい。
            candidates = list(yahoo_skus)
            for base in rakuten_skus + derived_bases:
                for suffix in YAHOO_SUFFIXES:
                    code = base + suffix
                    if code not in candidates:
                        candidates.append(code)

            if candidates:
                for store_name, message, ok in yahoo_close(yahoo_token, candidates):
                    print(f"    [Yahoo] {store_name}: {message}")
                    log_rows.append([now, case_id, case["caseType"], "Yahoo", store_name, "-", message])
                    if message != YAHOO_NOT_FOUND:
                        found_any = True
                    if not ok:
                        all_ok = False

            # Wowmaは楽天と同じ商品コードで出品されている。楽天SKU・推測コードそれぞれ確認し、
            # 出品があれば在庫を0にする（無ければ静かにスキップ、失敗にはしない）。
            if WOWMA_ENABLED:
                for sku in dict.fromkeys(rakuten_skus + derived_bases):
                    try:
                        item = wowma_get_item(sku)
                    except Exception as e:
                        print(f"    [Wowma] {sku}: 取得エラー: {e}")
                        log_rows.append([now, case_id, case["caseType"], SHOP_WOWMA, "-", sku, f"取得エラー: {e}"])
                        all_ok = False
                        continue
                    if item is None:
                        continue
                    found_any = True
                    stock = item.get("stockCount")
                    if stock == "0":
                        print(f"    [Wowma] {sku}: すでに在庫0")
                        log_rows.append([now, case_id, case["caseType"], SHOP_WOWMA, "-", sku, "すでに在庫0"])
                        continue
                    ok, message = wowma_update_stock(sku, 0, dry_run=DRY_RUN)
                    print(f"    [Wowma] {sku}: {message}")
                    log_rows.append([now, case_id, case["caseType"], SHOP_WOWMA, "-", sku, message])
                    if not ok:
                        all_ok = False

            if not all_ok:
                print("  ⚠️ 失敗があったため、ケースは New のまま残します（次回再挑戦）。")
                continue

            if DRY_RUN:
                print("  【DRY RUN】本番ならここでケースを In-Progress にします。")
                continue

            reply_message = REPLY_MESSAGE if found_any else REPLY_MESSAGE_NOT_FOUND
            if not found_any:
                print("  ※ 両モールとも該当出品が見つかりませんでした（すでに削除済みか、SKUが古い可能性）")

            try:
                action = update_case(page, case_id, reply_message)
                print(f"  ✅ {action}／Reply「{reply_message}」を投稿しました。")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", action])
            except Exception as e:
                print(f"  ⚠️ ケース更新に失敗しました（モール側は停止済み）: {e}")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", f"ケース更新失敗: {e}"])

        browser.close()

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを「{LOG_SHEET_NAME}」タブに{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print("=== Case Orders 自動Close 完了 ===")


if __name__ == "__main__":
    main()
