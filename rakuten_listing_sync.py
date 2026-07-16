"""
楽天RMS商品検索API（items.search）で複数店舗分の出品商品（商品名・価格・在庫状況）を取得し、
専用スプレッドシートの「楽天_出品データ」タブへ毎日の最新スナップショットとして書き込む。

- API取得が失敗・0件だった場合はシートを上書きせず終了する
  （前日までのデータを誤って消さないため。rakuten_price_check.py と同じ考え方）
- いずれか1店舗でも取得に失敗した場合は、他店舗分も含めて書き込みを中止する
  （一部店舗のデータだけで上書きして、失敗した店舗の前日データを消してしまわないため）
- items.search のレスポンスには在庫の「数」は含まれない（在庫関連の設定のみ）。
  そのため在庫は isItemStockout フィルタを使った別クエリで「在庫あり/在庫切れ」の二値として持つ。
- 楽天RMSの認証（serviceSecret/licenseKey）は店舗ごとに発行されるため、店舗ごとに個別に呼び出す。
"""

import os
import sys
import time
import base64
import json
from datetime import datetime, timezone, timedelta

import requests
import gspread
from google.oauth2.service_account import Credentials

from status_sheet import write_rows_in_batches, update_status

JST = timezone(timedelta(hours=9))

# ── 設定（環境変数から読み込み） ──────────────────
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

# 店舗一覧。店舗を増やす場合はここに要素を追加し、対応する環境変数をGitHub Secretsに登録する。
STORES = [
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

SHEET_NAME = "楽天_出品データ"
SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/items/search"
HITS_PER_PAGE = 100  # items.search の1ページ最大件数（デフォルトは10のため明示指定が必須）
PAGE_INTERVAL = 1.0  # ページ間の待機（秒）。APIへの過度な連続アクセスを避ける

HEADER = ["店舗名", "商品管理番号", "商品名", "販売価格", "在庫状況", "取得日時(JST)"]


# ── RMS API 認証ヘッダー ──────────────────────────
def get_auth_header(store: dict) -> dict:
    token = base64.b64encode(
        f"{store['service_secret']}:{store['license_key']}".encode()
    ).decode()
    return {"Authorization": f"ESA {token}"}


# ── items.search をcursorMarkでページングしながら全件取得 ─
def search_all(store: dict, extra_params: dict) -> list:
    """
    取得中に何らかのエラーが起きた場合は例外を投げる。
    呼び出し側で「シートを書き換えずに終了する」判断に使うため、ここでは例外を握りつぶさない。
    終了条件は公式仕様の通り「リクエストしたcursorMark == レスポンスのnextCursorMark」。
    """
    headers = get_auth_header(store)
    items = []
    cursor_mark = "*"
    page = 1

    while True:
        params = {"hits": HITS_PER_PAGE, "cursorMark": cursor_mark, **extra_params}
        res = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)

        if res.status_code == 401:
            raise RuntimeError(
                f"[{store['name']}] 認証エラー（401）。ライセンスキーの期限切れ（3か月ごと）の可能性があります。"
                "楽天RMS管理画面でライセンスキーを再発行し、GitHub Secretsを更新してください。"
            )
        res.raise_for_status()
        data = res.json()

        results = data.get("results", [])
        print(f"    [{store['name']}] ページ{page}: {len(results)}件取得（累計 {data.get('numFound', '?')}件中）")
        items.extend(results)

        next_cursor = data.get("nextCursorMark")
        if not results or not next_cursor or next_cursor == cursor_mark:
            break
        cursor_mark = next_cursor
        page += 1
        time.sleep(PAGE_INTERVAL)

    return items


def fetch_store_rows(store: dict, fetched_at: str) -> list:
    """1店舗分の全商品を行データのリストに変換して返す。"""
    print(f"  [{store['name']}] 全商品を取得中...")
    raw_items = search_all(store, {})

    print(f"  [{store['name']}] 在庫切れ商品を取得中...")
    stockout_results = search_all(store, {"isItemStockout": "true"})
    stockout_numbers = {
        r.get("item", {}).get("manageNumber")
        for r in stockout_results
        if r.get("item", {}).get("manageNumber")
    }

    rows = [extract_row(store["name"], r, stockout_numbers, fetched_at) for r in raw_items]
    print(f"  [{store['name']}] 取得件数: {len(rows)}件（うち在庫切れ: {len(stockout_numbers)}件）")
    return rows


# ── 1商品分のレスポンスを行データに変換 ────────────
def extract_row(store_name: str, result: dict, stockout_numbers: set, fetched_at: str) -> list:
    """
    価格は variants.{variantId}.standardPrice の最安値を採用する。
    在庫は items.search が実際の数量を返さないため、「在庫あり/在庫切れ」の二値で持つ。
    """
    item = result.get("item", result)

    manage_number = item.get("manageNumber", "")
    title = item.get("title", "")

    variants = item.get("variants") or {}
    prices = []
    for variant in variants.values():
        v_price = variant.get("standardPrice")
        if v_price is not None:
            try:
                prices.append(float(v_price))
            except (TypeError, ValueError):
                pass
    price = min(prices) if prices else ""

    stock_status = "在庫切れ" if manage_number in stockout_numbers else "在庫あり"

    if not manage_number and not title:
        print(f"  [警告] [{store_name}] 想定外のレスポンス構造の商品があります。キー: {list(item.keys())}")

    return [store_name, manage_number, title, price, stock_status, fetched_at]


# ── スプレッドシート ──────────────────────────────
def get_worksheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(LISTING_SPREADSHEET_ID)

    try:
        return spreadsheet.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  「{SHEET_NAME}」タブが存在しないため新規作成します")
        return spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADER))


# ── メイン処理 ────────────────────────────────────
def main():
    print("=== 楽天出品データ同期 開始 ===")

    fetched_at = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    all_rows = []

    try:
        for store in STORES:
            all_rows.extend(fetch_store_rows(store, fetched_at))
    except Exception as e:
        print(f"取得失敗のため中断します（シートは前回のまま更新しません）: {e}")
        sys.exit(1)

    if not all_rows:
        print("取得件数が0件でした。API側の異常の可能性があるため、シートは更新せず終了します。")
        sys.exit(1)

    print(f"全店舗合計: {len(all_rows)}件。スプレッドシートへ書き込みます...")

    worksheet = get_worksheet()
    write_rows_in_batches(worksheet, [HEADER] + all_rows)
    update_status("楽天_出品データ")

    print("=== 楽天出品データ同期 完了 ===")


if __name__ == "__main__":
    main()
