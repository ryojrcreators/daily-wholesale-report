"""
Yahoo!ショッピング商品リストAPI（myItemList）で出品中の全商品（商品名・価格・在庫数）を取得し、
専用スプレッドシートの「Yahoo_出品データ」タブへ毎日の最新スナップショットとして書き込む。

- 2店舗とも同一のYahoo!ショッピングアカウントで管理されているため、OAuth連携（Client ID/Secret/
  リフレッシュトークン）は1組のみ。API呼び出し時に seller_id を店舗ごとに切り替える。
- リフレッシュトークンは使うたびに新しい値にローテーションされる（有効期限は公開鍵登録済みで4週間、
  使うたびにリセットされる）。そのためGitHub Secretsではなく、スプレッドシートの非公開タブ
  「Yahoo_Config」に保存し、毎回読み書きする。
- API取得が失敗・0件だった場合はシートを上書きせず終了する（rakuten_listing_sync.py と同じ考え方）。
- myItemList は query または stcat_key のどちらかが必須パラメータで、query=""（空文字）では
  「queryかstcat_keyのどちらか片方が必須です」エラーになることを実機で確認済み。そのため、
  ストアカテゴリ一覧API（stCategoryList）でカテゴリツリーを辿って全stcat_keyを集め、
  カテゴリごとにmyItemListをページングする方式にしている。同じ商品が複数カテゴリに
  属していることがあるため、商品コードで重複除去する。
  ※ どのストアカテゴリにも属していない商品がもしあれば取得漏れになる可能性がある
    （未検証。件数が想定より少ない場合はこの点を疑う）。
- カテゴリ数が数百に及ぶ店舗もあり、全件取得に1時間を超えることがある。アクセストークンは
  短命なため、401が返ってきたらその場でリフレッシュトークンを使って再取得し、同じリクエストを
  リトライする（authed_get）。
"""

import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree

import requests
import gspread
from google.oauth2.service_account import Credentials

from status_sheet import write_rows_in_batches, update_status

JST = timezone(timedelta(hours=9))

# ── 設定（環境変数から読み込み） ──────────────────
CLIENT_ID = os.environ["YAHOO_CLIENT_ID"]
CLIENT_SECRET = os.environ["YAHOO_CLIENT_SECRET"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

# 店舗一覧。2店舗とも同一アカウントなので違うのは seller_id のみ。
STORES = [
    {"name": os.environ["YAHOO_SHOP_NAME_1"], "seller_id": os.environ["YAHOO_SELLER_ID_1"]},
    {"name": os.environ["YAHOO_SHOP_NAME_2"], "seller_id": os.environ["YAHOO_SELLER_ID_2"]},
]

SHEET_NAME = "Yahoo_出品データ"
CONFIG_SHEET_NAME = "Yahoo_Config"

TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
ITEM_LIST_URL = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1/myItemList"
STCAT_LIST_URL = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1/stCategoryList"
RESULTS_PER_PAGE = 100  # myItemList の1ページ最大件数
PAGE_INTERVAL = 1.0  # レート制限（1クエリー/秒）対策

HEADER = ["店舗名", "商品コード", "商品名", "価格", "在庫数", "取得日時(JST)"]


# ── スプレッドシート共通 ──────────────────────────
def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(LISTING_SPREADSHEET_ID)


def get_or_create_worksheet(spreadsheet, name: str, cols: int):
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  「{name}」タブが存在しないため新規作成します")
        return spreadsheet.add_worksheet(title=name, rows=1000, cols=cols)


# ── リフレッシュトークンの読み書き（Yahoo_Configタブ） ─
def load_refresh_token(spreadsheet) -> str:
    ws = get_or_create_worksheet(spreadsheet, CONFIG_SHEET_NAME, cols=2)
    values = ws.get_all_values()
    for row in values:
        if row and row[0] == "refresh_token":
            return row[1]
    raise RuntimeError(
        f"「{CONFIG_SHEET_NAME}」タブに refresh_token が見つかりません。"
        "初回セットアップとして、A列に「refresh_token」、B列に実際の値を手動で入力してください。"
    )


def save_refresh_token(spreadsheet, new_token: str):
    """トークン更新直後、他の処理より先に呼び出す（失敗時もトークンだけは確実に残すため）。"""
    ws = get_or_create_worksheet(spreadsheet, CONFIG_SHEET_NAME, cols=2)
    values = ws.get_all_values()
    for i, row in enumerate(values, start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_token]])
            return
    ws.append_row(["refresh_token", new_token])


# ── OAuth アクセストークン更新 ────────────────────
def refresh_access_token(spreadsheet) -> str:
    current_refresh_token = load_refresh_token(spreadsheet)

    res = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": current_refresh_token},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(
            f"アクセストークン更新に失敗しました（status={res.status_code}）: {res.text[:500]}"
        )
    data = res.json()

    # リフレッシュトークンはローテーションされるため、新しい値をすぐに保存する
    new_refresh_token = data.get("refresh_token", current_refresh_token)
    save_refresh_token(spreadsheet, new_refresh_token)

    return data["access_token"]


class TokenState:
    """実行中に401で失効した場合、その場でリフレッシュして持ち直すための入れ物。"""

    def __init__(self, access_token: str):
        self.access_token = access_token


def authed_get(spreadsheet, token_state: TokenState, url: str, params: dict, store_name: str, api_name: str):
    """
    アクセストークンで認証しつつGETする。401が返ってきた場合は1回だけ
    リフレッシュトークンでアクセストークンを取り直してリトライする
    （カテゴリ数が多い店舗では全件取得に1時間以上かかり、その間にアクセストークンが
    失効することがあるため）。
    """
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {token_state.access_token}"}
        res = requests.get(url, headers=headers, params=params, timeout=30)

        if res.status_code == 401 and attempt == 0:
            print(f"    [{store_name}] アクセストークンが失効したため再取得します...")
            token_state.access_token = refresh_access_token(spreadsheet)
            continue

        if res.status_code >= 400:
            raise RuntimeError(
                f"[{store_name}] {api_name} エラー（status={res.status_code}）: {res.text[:1000]}"
            )
        return res

    raise RuntimeError(f"[{store_name}] {api_name}: アクセストークン再取得後も認証エラーが続きました。")


# ── ストアカテゴリをツリー状に辿って全stcat_keyを集める ─
def fetch_all_stcat_keys(spreadsheet, token_state: TokenState, store: dict) -> list:
    keys = []

    def walk(page_key):
        params = {"seller_id": store["seller_id"]}
        if page_key:
            params["page_key"] = page_key
        res = authed_get(spreadsheet, token_state, STCAT_LIST_URL, params, store["name"], "stCategoryList")

        root = ElementTree.fromstring(res.content)
        for result in root.findall("Result"):
            key = (result.findtext("PageKey") or "").strip()
            if not key:
                continue
            keys.append(key)
            if len(keys) % 20 == 0:
                print(f"    [{store['name']}] カテゴリ一覧収集中... {len(keys)}件見つかりました")
            time.sleep(PAGE_INTERVAL)
            walk(key)

    print(f"  [{store['name']}] カテゴリ一覧を収集中...")
    walk(None)
    return keys


# ── 1カテゴリ分、myItemListをページングして取得 ────
def fetch_items_in_category(spreadsheet, token_state: TokenState, store: dict, stcat_key: str, items_by_code: dict):
    start = 1

    while True:
        params = {
            "seller_id": store["seller_id"],
            "stock": "true",
            "start": start,
            "results": RESULTS_PER_PAGE,
            "stcat_key": stcat_key,
        }
        res = authed_get(spreadsheet, token_state, ITEM_LIST_URL, params, store["name"], "myItemList")

        root = ElementTree.fromstring(res.content)
        results = root.findall("Result")
        total = root.get("totalResultsAvailable", "?")

        for result in results:
            item_code = (result.findtext("ItemCode") or "").strip()
            if not item_code:
                continue
            name = (result.findtext("Name") or "").strip()
            price = (result.findtext("Price") or "").strip()
            quantity = (result.findtext("Quantity") or "").strip()
            items_by_code[item_code] = [store["name"], item_code, name, price, quantity]

        if not results or start + len(results) > int(total):
            break
        start += len(results)
        time.sleep(PAGE_INTERVAL)


# ── 1店舗分：カテゴリを全て辿って商品を重複除去しながら取得 ─
def fetch_store_rows(spreadsheet, token_state: TokenState, store: dict, fetched_at: str) -> list:
    stcat_keys = fetch_all_stcat_keys(spreadsheet, token_state, store)
    print(f"  [{store['name']}] ストアカテゴリ数: {len(stcat_keys)}")

    items_by_code = {}
    for i, stcat_key in enumerate(stcat_keys, start=1):
        fetch_items_in_category(spreadsheet, token_state, store, stcat_key, items_by_code)
        print(f"    [{store['name']}] カテゴリ{i}/{len(stcat_keys)}処理済み（累計商品数: {len(items_by_code)}件）")

    rows = [row + [fetched_at] for row in items_by_code.values()]
    print(f"  [{store['name']}] 取得件数（重複除去後）: {len(rows)}件")
    return rows


# ── メイン処理 ────────────────────────────────────
def main():
    print("=== Yahoo出品データ同期 開始 ===")

    spreadsheet = get_spreadsheet()
    fetched_at = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    all_rows = []

    try:
        token_state = TokenState(refresh_access_token(spreadsheet))
        for store in STORES:
            all_rows.extend(fetch_store_rows(spreadsheet, token_state, store, fetched_at))
    except Exception as e:
        print(f"取得失敗のため中断します（シートは前回のまま更新しません）: {e}")
        sys.exit(1)

    if not all_rows:
        print("取得件数が0件でした。API側の異常の可能性があるため、シートは更新せず終了します。")
        sys.exit(1)

    print(f"全店舗合計: {len(all_rows)}件。スプレッドシートへ書き込みます...")

    worksheet = get_or_create_worksheet(spreadsheet, SHEET_NAME, cols=len(HEADER))
    write_rows_in_batches(worksheet, [HEADER] + all_rows)
    update_status("Yahoo_出品データ")

    print("=== Yahoo出品データ同期 完了 ===")


if __name__ == "__main__":
    main()
