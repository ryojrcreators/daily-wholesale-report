"""
Yahoo!ショッピング商品リストAPI（myItemList）で出品中の全商品（商品名・価格・在庫数）を取得し、
専用スプレッドシートの「Yahoo_出品データ」タブへ毎日の最新スナップショットとして書き込む。

- 2店舗とも同一のYahoo!ショッピングアカウントで管理されているため、OAuth連携（Client ID/Secret/
  リフレッシュトークン）は1組のみ。API呼び出し時に seller_id を店舗ごとに切り替える。
- リフレッシュトークンは使うたびに新しい値にローテーションされる（有効期限は公開鍵登録済みで4週間、
  使うたびにリセットされる）。そのためGitHub Secretsではなく、スプレッドシートの非公開タブ
  「Yahoo_Config」に保存し、毎回読み書きする。
- API取得が失敗・0件だった場合はシートを上書きせず終了する（rakuten_listing_sync.py と同じ考え方）。
- myItemList は query または stcat_key のどちらかが必須パラメータ。絞り込みなしで全件取得できるかは
  未検証のため、まず query="" を試す。エラーになった場合はログを見て取得方式を調整する。
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
            ws.update(f"A{i}:B{i}", [["refresh_token", new_token]])
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


# ── myItemList をページングしながら1店舗分を取得 ────
def fetch_store_rows(store: dict, access_token: str, fetched_at: str) -> list:
    headers = {"Authorization": f"Bearer {access_token}"}
    rows = []
    start = 1
    page = 1

    while True:
        params = {
            "seller_id": store["seller_id"],
            "stock": "true",
            "start": start,
            "results": RESULTS_PER_PAGE,
            "query": "",  # 要検証：絞り込みなしで全件取得できるか未確認
        }
        res = requests.get(ITEM_LIST_URL, headers=headers, params=params, timeout=30)

        if res.status_code == 401:
            raise RuntimeError(f"[{store['name']}] 認証エラー（401）。アクセストークンが無効です。")
        res.raise_for_status()

        root = ElementTree.fromstring(res.content)
        results = root.findall("Result")
        total = root.get("totalResultsAvailable", "?")
        print(f"    [{store['name']}] ページ{page}: {len(results)}件取得（累計 {total}件中）")

        for result in results:
            item_code = (result.findtext("ItemCode") or "").strip()
            name = (result.findtext("Name") or "").strip()
            price = (result.findtext("Price") or "").strip()
            quantity = (result.findtext("Quantity") or "").strip()
            rows.append([store["name"], item_code, name, price, quantity, fetched_at])

        if not results or start + len(results) > int(total):
            break
        start += len(results)
        page += 1
        time.sleep(PAGE_INTERVAL)

    print(f"  [{store['name']}] 取得件数: {len(rows)}件")
    return rows


# ── メイン処理 ────────────────────────────────────
def main():
    print("=== Yahoo出品データ同期 開始 ===")

    spreadsheet = get_spreadsheet()
    fetched_at = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    all_rows = []

    try:
        access_token = refresh_access_token(spreadsheet)
        for store in STORES:
            all_rows.extend(fetch_store_rows(store, access_token, fetched_at))
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
