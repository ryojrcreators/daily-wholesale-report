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
TARGET_CASE_TYPES = ("Close (Temporary)", "Close (Permanent)")
REPLY_MESSAGE = "Rakuten/Yahoo Closed"

# Related Skus の Shop 列の表記
SHOP_RAKUTEN = "楽天"
SHOP_YAHOO = "Yahoo(new)"

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


def rakuten_hide(manage_number: str) -> list:
    """
    2店舗を順に確認し、存在する店舗すべてで hideItem=true にする。
    戻り値は [(店舗名, 結果文字列, 成功したか)] のリスト。
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
        results.append(("-", "どちらの店舗にも存在しません", False))
    return results


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


def yahoo_close(token: str, item_code: str) -> list:
    """2店舗を順に確認し、存在する店舗すべてで在庫を0にする。"""
    results = []
    for store in YAHOO_STORES:
        try:
            item = yahoo_get_item(token, store, item_code)
        except Exception as e:
            results.append((store["name"], f"取得エラー: {e}", False))
            continue
        finally:
            time.sleep(API_INTERVAL)

        if item is None:
            continue  # この店舗には存在しない

        if item.get("Quantity") == "0":
            results.append((store["name"], "すでに在庫0", True))
            continue

        if DRY_RUN:
            results.append((store["name"], f"【DRY RUN】在庫{item.get('Quantity')}→0の対象", True))
            continue

        try:
            res = requests.post(
                f"{YAHOO_BASE}/setStock",
                headers={"Authorization": f"Bearer {token}"},
                data={"seller_id": store["seller_id"], "item_code": item_code, "quantity": "0"},
                timeout=30,
            )
            if res.status_code < 400:
                results.append((store["name"], f"在庫{item.get('Quantity')}→0にしました", True))
            else:
                results.append((store["name"], f"在庫更新失敗({res.status_code})", False))
        except Exception as e:
            results.append((store["name"], f"在庫更新エラー: {e}", False))
        finally:
            time.sleep(API_INTERVAL)

    if not results:
        results.append(("-", "どちらの店舗にも存在しません", False))
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


def fetch_target_cases(page) -> list:
    """New かつ Rakuten/Yahoo のケースのうち、Close系のものを返す"""
    page.goto(CASE_LIST_URL, wait_until="networkidle")
    page.wait_for_timeout(500)

    rows = page.evaluate(
        """() => {
            const table = document.querySelector('table');
            if (!table) return [];
            const headers = [...table.querySelectorAll('thead th')].map(th => th.textContent.trim());
            const idxId = headers.indexOf('Id');
            const idxType = headers.indexOf('Case Type');
            const idxProduct = headers.indexOf('Product');
            return [...table.querySelectorAll('tbody tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                return {
                    id: (tds[idxId] || '').replace(/,/g, ''),
                    caseType: tds[idxType] || '',
                    product: tds[idxProduct] || '',
                };
            });
        }"""
    )

    targets = [r for r in rows if r["caseType"] in TARGET_CASE_TYPES and r["id"].isdigit()]
    print(f"New + Rakuten/Yahoo のケース: {len(rows)}件、うちClose系: {len(targets)}件")
    return targets


def fetch_case_skus(page, case_id: str) -> list:
    """
    ケース詳細ページの Related Skus から、楽天・Yahooの商品コードを取り出す。
    戻り値は [{"mall": "楽天"|"Yahoo(new)", "sku": "..."}]
    """
    page.goto(f"{BASE_URL}/case-orders/view/{case_id}", wait_until="networkidle")
    page.wait_for_timeout(300)

    return page.evaluate(
        """() => {
            // 「Related Skus」見出しの直後のテーブルを探す
            const heading = [...document.querySelectorAll('h1,h2,h3,h4,legend,div,span')]
                .find(el => el.textContent.trim() === 'Related Skus');
            if (!heading) return [];
            let table = null;
            for (let el = heading; el && !table; el = el.nextElementSibling) {
                table = el.querySelector ? el.querySelector('table') : null;
                if (el.tagName === 'TABLE') table = el;
            }
            if (!table) {
                // 見出しの親要素から探す（マークアップが入れ子の場合）
                const parent = heading.closest('div');
                table = parent ? parent.querySelector('table') : null;
            }
            if (!table) return [];

            const headers = [...table.querySelectorAll('thead th')].map(th => th.textContent.trim());
            const idxSku = headers.indexOf('Sku');
            const idxShop = headers.indexOf('Shop');
            if (idxSku < 0 || idxShop < 0) return [];

            return [...table.querySelectorAll('tbody tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                return { mall: tds[idxShop] || '', sku: tds[idxSku] || '' };
            }).filter(r => r.sku);
        }"""
    )


def mark_case_in_progress(page, case_id: str):
    """Case Status を In-Progress にし、Reply を入れて保存する"""
    page.goto(f"{BASE_URL}/case-orders/edit/{case_id}", wait_until="networkidle")
    page.wait_for_timeout(300)

    page.select_option("#case-status-id", CASE_STATUS_IN_PROGRESS)
    # Replyのtextareaは case-order-replies-{n}-message という形でnが可変
    page.fill('textarea[id^="case-order-replies-"][id$="-message"]', REPLY_MESSAGE)
    page.click('form[action*="/case-orders/edit/"] button[type="submit"]')
    page.wait_for_load_state("networkidle")


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

            print(f"  対象SKU: {len(targets)}件")
            all_ok = True

            for target in targets:
                mall, sku = target["mall"], target["sku"]
                if mall == SHOP_RAKUTEN:
                    results = rakuten_hide(sku)
                else:
                    results = yahoo_close(yahoo_token, sku)

                for store_name, message, ok in results:
                    print(f"    [{mall}] {sku} / {store_name}: {message}")
                    log_rows.append([now, case_id, case["caseType"], mall, store_name, sku, message])
                    if not ok:
                        all_ok = False

            if not all_ok:
                print("  ⚠️ 失敗があったため、ケースは New のまま残します（次回再挑戦）。")
                continue

            if DRY_RUN:
                print("  【DRY RUN】本番ならここでケースを In-Progress にします。")
                continue

            try:
                mark_case_in_progress(page, case_id)
                print(f"  ✅ ケースを In-Progress にして「{REPLY_MESSAGE}」を投稿しました。")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", "ケース更新完了"])
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
