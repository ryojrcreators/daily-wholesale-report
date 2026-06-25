"""
楽天赤字・仕入不可チェックスクリプト
- Google Sheetsからデータ読み込み
- Keepa APIで価格・在庫チェック（100件バッチ×2回 = 200件/実行）
- 結果を在庫チェック列・価格チェック列に書き込み
"""

import os
import time
import json
import requests
import gspread
from google.oauth2.service_account import Credentials

# ── 設定 ──────────────────────────────────────────
SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"

KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]
# 利益率・手数料率は事業上の機微情報のため Secret から読み込む（公開コードに数値を出さない）
PROFIT_RATE = float(os.environ["PROFIT_RATE"])
COMMISSION_RATE = float(os.environ["COMMISSION_RATE"])

BATCH_SIZE = 100     # Keepaバッチ最大件数
BATCHES_PER_RUN = 5  # 1回の実行で何バッチ処理するか（500件）

SHEET_WRITE_INTERVAL = 1.2  # Sheets書き込み1件ごとの待機（秒）。Sheets APIの書き込み回数制限対策
SHEET_WRITE_RETRIES = 5     # 429エラー時のリトライ回数

# 列インデックス（0始まり）
COL_ITEM_ID = 0       # 商品管理番号
COL_NAME = 1          # 商品名
COL_ASIN = 2          # ASIN
COL_PRICE_JPY = 3     # 楽天販売価格
COL_STOCK_CHECK = 4   # 在庫チェック（書き込み先）
COL_PRICE_CHECK = 5   # 価格チェック（書き込み先）
COL_PROPER_PRICE = 6  # 適正価格（書き込み先）

# ── Google Sheets 認証 ────────────────────────────
def get_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)


    return spreadsheet.worksheet(SHEET_NAME)

# ── 為替レート取得 ────────────────────────────────
def get_exchange_rate() -> float:
    try:
        res = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=JPY",
            timeout=10
        )
        res.raise_for_status()
        rate = res.json()["rates"]["JPY"]
        print(f"為替レート: 1 USD = {rate} JPY")
        return rate
    except Exception as e:
        print(f"為替レート取得失敗、デフォルト150を使用: {e}")
        return 150.0

# ── 重量別送料テーブル ────────────────────────────
SHIPPING_TABLE = {
    0.5: 4.50, 1.0: 5.05, 1.5: 5.55, 2.0: 6.10,
    2.5: 6.60, 3.0: 7.15, 3.5: 7.65, 4.0: 8.20,
    4.5: 8.70, 5.0: 9.25, 5.5: 9.75, 6.0: 10.30,
    7.0: 11.35, 8.0: 12.40, 9.0: 13.50, 10.0: 14.55,
    11.0: 15.60, 12.0: 16.65, 13.0: 17.70, 14.0: 18.75,
    15.0: 19.80, 16.0: 20.85, 17.0: 21.90, 18.0: 22.95,
    19.0: 24.00, 20.0: 25.05, 25.0: 30.55, 30.0: 36.10,
    35.0: 41.60, 40.0: 47.15, 45.0: 52.65, 50.0: 58.20,
    55.0: 63.70, 60.0: 69.25, 66.0: 76.05,
}

def write_with_retry(sheet, updates: list):
    """Sheets書き込み。429（書き込みクォータ超過）時は待機してリトライする。"""
    for attempt in range(SHEET_WRITE_RETRIES):
        try:
            sheet.batch_update(updates)
            return
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < SHEET_WRITE_RETRIES - 1:
                wait = 20 * (attempt + 1)
                print(f"  Sheets書き込みクォータ超過。{wait}秒待機してリトライ...")
                time.sleep(wait)
            else:
                raise

def get_shipping_cost(weight_lbs: float) -> float:
    for threshold in sorted(SHIPPING_TABLE.keys()):
        if weight_lbs <= threshold:
            return SHIPPING_TABLE[threshold]
    return SHIPPING_TABLE[66.0]

# ── Keepa トークン残量確認 ────────────────────────
def get_keepa_tokens_remaining() -> int:
    """Keepa APIのトークン残量を返す。取得失敗時は-1を返す。"""
    url = f"https://api.keepa.com/token?key={KEEPA_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        return res.json().get("tokensLeft", -1)
    except Exception as e:
        print(f"Keepaトークン残量取得失敗: {e}")
        return -1

# ── Keepa バッチ取得 ──────────────────────────────
def fetch_keepa_batch(asins: list):
    """
    Keepaバッチ取得。成功時は {asin: product} の辞書を返す。
    API自体が失敗（429・通信エラー等）した場合は None を返す。
    （None と空辞書を区別することで、API失敗を「仕入不可」と誤書き込みしない）
    """
    asin_str = ",".join(asins)
    url = (
        f"https://api.keepa.com/product"
        f"?key={KEEPA_API_KEY}"
        f"&domain=1"
        f"&asin={asin_str}"
        f"&stats=1"
    )
    try:
        res = requests.get(url, timeout=60)
        res.raise_for_status()
        data = res.json()
        products = data.get("products", [])
        return {p["asin"]: p for p in products}
    except Exception as e:
        print(f"Keepa APIエラー: {e}")
        return None  # API失敗。呼び出し側でバッチをスキップさせる

# ── 在庫・価格判定 ────────────────────────────────
def judge(product: dict, rakuten_price_jpy: int, exchange_rate: float) -> tuple:
    stats = product.get("stats", {})
    current = stats.get("current", [])

    def safe_get(lst, idx, default=None):
        try:
            val = lst[idx]
            return None if val == -1 else val
        except IndexError:
            return default

    # Keepa stats.current のインデックス（重要）
    #   [0]  AMAZON     : Amazon本体価格（セント、-1=なし）
    #   [1]  NEW        : 新品最安値（3rdパーティ含む、セント、-1=なし）
    #   [11] COUNT_NEW  : 新品出品数（-1=なし）
    amazon_price = safe_get(current, 0)
    new_price    = safe_get(current, 1)
    new_count    = safe_get(current, 11)

    # 仕入れ元の価格と在庫ステータスを決定
    if amazon_price is not None:
        source_price_cents = amazon_price
        stock_status = "✅ 正常"
    elif new_price is not None or (new_count is not None and new_count > 0):
        if new_price is None:
            return "🟢 3rdパーティ", "-", "-"
        source_price_cents = new_price
        stock_status = "🟢 3rdパーティ"
    else:
        return "⚠️ 仕入不可", "-", "-"

    source_price_usd = source_price_cents / 100.0

    weight_g = product.get("data", {}).get("packageWeight", None)
    if weight_g and weight_g > 0:
        weight_lbs = weight_g / 453.592
    else:
        weight_lbs = 1.0

    shipping_usd = get_shipping_cost(weight_lbs)
    cost_jpy = (source_price_usd + shipping_usd) * exchange_rate
    breakeven = rakuten_price_jpy * (1 - PROFIT_RATE - COMMISSION_RATE)

    if cost_jpy <= breakeven:
        return stock_status, "✅ 正常", "-"
    else:
        # 適正価格 = 仕入コスト ÷ (1 - 利益率 - 手数料率)
        proper_price = int(cost_jpy / (1 - PROFIT_RATE - COMMISSION_RATE))
        return stock_status, "🔴 赤字", f"¥{proper_price:,}"

# ── メイン処理 ────────────────────────────────────
def main():
    print("=== 楽天赤字チェック開始 ===")

    sheet = get_sheet()
    exchange_rate = get_exchange_rate()

    all_rows = sheet.get_all_values()
    rows = all_rows[1:]

    print(f"総行数: {len(rows)}")

    unchecked = [
        (i + 1, row)
        for i, row in enumerate(rows)
        if len(row) > COL_STOCK_CHECK and row[COL_STOCK_CHECK].strip() == ""
        and len(row) > COL_ASIN and row[COL_ASIN].strip() != ""
    ]

    print(f"未チェック件数: {len(unchecked)}")

    if not unchecked:
        print("未チェック商品なし。終了。")
        return

    target = unchecked[:BATCH_SIZE * BATCHES_PER_RUN]
    print(f"今回処理: {len(target)}件")

    for batch_start in range(0, len(target), BATCH_SIZE):
        # バッチ投入前にトークン残量を確認し、処理件数を動的に調整
        tokens = get_keepa_tokens_remaining()
        print(f"Keepaトークン残量: {tokens}")
        if tokens <= 0:
            print("⚠️ トークンがありません。終了します。")
            break

        # トークンが足りない場合はその分だけ処理（端数も無駄にしない）
        actual_batch_size = min(BATCH_SIZE, tokens)
        batch = target[batch_start:batch_start + actual_batch_size]
        asins = [row[COL_ASIN] for _, row in batch]

        if actual_batch_size < BATCH_SIZE:
            print(f"トークン不足のため今回は{actual_batch_size}件に絞って処理します。")

        print(f"Keepaバッチ取得: {len(asins)}件...")
        keepa_data = fetch_keepa_batch(asins)

        # API自体が失敗（None）した場合は書き込まずスキップ（誤って仕入不可にしない）
        if keepa_data is None:
            print("⚠️ Keepa APIエラーのためこのバッチはスキップします（空欄のまま＝次回再チェック）。")
            break

        print(f"取得成功: {len(keepa_data)}件")

        for sheet_row_idx, row in batch:
            asin = row[COL_ASIN]
            try:
                rakuten_price = int(str(row[COL_PRICE_JPY]).replace(",", ""))
            except ValueError:
                rakuten_price = 0

            product = keepa_data.get(asin)

            if product is None:
                # バッチ取得は成功したがこのASINだけデータなし＝廃盤/無効ASIN
                stock_result = "⚠️ 仕入不可"
                price_result = "-"
                proper_price_result = "-"
            else:
                stock_result, price_result, proper_price_result = judge(product, rakuten_price, exchange_rate)

            # 1件ずつ即書き込み（途中停止しても結果を無駄にしない）
            stock_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_STOCK_CHECK + 1)
            price_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_PRICE_CHECK + 1)
            proper_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_PROPER_PRICE + 1)
            write_with_retry(sheet, [
                {"range": stock_cell, "values": [[stock_result]]},
                {"range": price_cell, "values": [[price_result]]},
                {"range": proper_cell, "values": [[proper_price_result]]},
            ])

            print(f"  {asin}: {stock_result} / {price_result} / {proper_price_result}")

            time.sleep(SHEET_WRITE_INTERVAL)

        print(f"バッチ書き込み完了")

        if batch_start + BATCH_SIZE < len(target):
            print("次のバッチまで30秒待機...")
            time.sleep(30)

    # フォントをArialに設定（在庫・価格チェック列）
    sheet.format("E2:G10000", {"textFormat": {"fontFamily": "Arial"}})

    print("=== 楽天赤字チェック完了 ===")

if __name__ == "__main__":
    main()
