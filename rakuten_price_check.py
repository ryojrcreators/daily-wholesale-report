"""
楽天赤字・仕入不可チェックスクリプト
- Google Sheetsからデータ読み込み
- Keepa APIで価格・在庫チェック（100件バッチ×5回 = 500件/実行）
- 結果を在庫チェック列・価格チェック列に書き込み

チェック済みかどうかに関わらず、ASINが入っている行を上から順に巡回し続ける
（末尾まで行ったら先頭に戻ってまたチェックする）。これにより、一度チェックした
商品も定期的に再チェックされ、廃盤→再入荷のような変化を検知できる。
巡回位置（カーソル）は「チェック進捗」タブのA1セルに保存し、次回実行時に
そこから再開する。
"""

import os
import time
import json
from datetime import datetime, timezone, timedelta
import requests
import gspread
from google.oauth2.service_account import Credentials

from set_quantity import ensure_annotation_columns, annotation_updates, compute_pkg_and_ratio

JST = timezone(timedelta(hours=9))

# ── 設定 ──────────────────────────────────────────
SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"

KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]
# 利益率・手数料率は事業上の機微情報のため Secret から読み込む（公開コードに数値を出さない）
PROFIT_RATE = float(os.environ["PROFIT_RATE"])
COMMISSION_RATE = float(os.environ["COMMISSION_RATE"])

BATCH_SIZE = 100      # Keepaバッチ最大件数
BATCHES_PER_RUN = 36  # 1回の実行で何バッチ処理するか（3,600件。Keepaプラン増強後、全件を約2日で一巡する想定）

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
COL_LAST_CHECKED = 7  # 最終チェック日時（書き込み先）

CURSOR_SHEET_NAME = "チェック進捗"


# ── Google Sheets 認証 ────────────────────────────
def get_spreadsheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def get_sheet():
    return get_spreadsheet().worksheet(SHEET_NAME)


# ── 楽天の現在販売価格（D列の最新化用） ────────────
# rakuten_price_check.py自体はKeepa APIしか呼ばないため、楽天RMS APIをライブでは叩かない
# （商品ごとにGETすると3,600件/実行で実行時間が大幅に悪化する）。代わりに
# rakuten_listing_sync.pyが毎日1回取得している「楽天_出品データ」タブのスナップショットを
# 再利用する。GOOGLE_CREDENTIALS/RAKUTEN_LISTING_SPREADSHEET_IDは、このタブを読み書きする
# 他のスクリプト（rakuten_listing_sync.py, case_orders_auto_close.py）と同じペアの環境変数。
LISTING_SHEET_NAME = "楽天_出品データ"


def load_current_prices() -> dict | None:
    """
    {商品管理番号: 最安値} を返す。取得できなければNone（呼び出し側はD列更新を諦め、
    在庫・価格チェック自体は従来通り続行する）。
    """
    try:
        creds = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_CREDENTIALS"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]).worksheet(LISTING_SHEET_NAME)
        rows = ws.get_all_values()
    except Exception as e:
        print(f"「{LISTING_SHEET_NAME}」タブの取得に失敗したため、D列の更新はスキップします: {e}")
        return None

    if len(rows) <= 1:
        return None

    header = rows[0]
    try:
        idx_item = header.index("商品管理番号")
        idx_price = header.index("販売価格")
    except ValueError:
        print(f"「{LISTING_SHEET_NAME}」タブの列構成が想定と異なるため、D列の更新はスキップします。")
        return None

    prices: dict = {}
    for row in rows[1:]:
        if len(row) <= max(idx_item, idx_price):
            continue
        item_number = row[idx_item].strip()
        price_str = row[idx_price].strip()
        if not item_number or not price_str:
            continue
        try:
            price = float(price_str)
        except ValueError:
            continue
        if item_number not in prices or price < prices[item_number]:
            prices[item_number] = price

    return prices


# ── 巡回カーソル（前回どこまでチェックしたか） ──────
def get_cursor(spreadsheet) -> int:
    """対象プール内でのインデックス（0始まり）を返す。無ければ0。"""
    try:
        ws = spreadsheet.worksheet(CURSOR_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        return 0
    value = ws.acell("A1").value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def save_cursor(spreadsheet, cursor: int):
    try:
        ws = spreadsheet.worksheet(CURSOR_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=CURSOR_SHEET_NAME, rows=2, cols=1)
        ws.update_cell(1, 1, "0")
    ws.update_cell(1, 1, str(cursor))

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
def judge(product: dict, rakuten_price_jpy: int, exchange_rate: float, ratio: float = 1) -> tuple:
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
    # 楽天1個の販売に対して実際に必要なASIN購入量（購入倍率）を反映する。
    # 例: 「3個セット」出品でASINが6個入りパックの場合、ratio=0.5 →
    # 楽天1個あたりの仕入コストはASIN価格の半分で済む。逆に単品ずつ切り出して
    # 売っている出品では ratio<1、まとめ買いが必要な出品では ratio>1 になる。
    cost_jpy = (source_price_usd + shipping_usd) * exchange_rate * ratio
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

    spreadsheet = get_spreadsheet()
    sheet = spreadsheet.worksheet(SHEET_NAME)
    exchange_rate = get_exchange_rate()

    current_prices = load_current_prices()
    if current_prices is None:
        print("楽天の現在価格スナップショットを取得できなかったため、D列は更新せず従来のシート値のみ使用します。")
    else:
        print(f"楽天の現在価格スナップショット: {len(current_prices)}件読み込みました。")

    all_rows = sheet.get_all_values()
    rows = all_rows[1:]

    print(f"総行数: {len(rows)}")

    header = all_rows[0]
    if len(header) <= COL_LAST_CHECKED or header[COL_LAST_CHECKED].strip() != "最終チェック日時":
        if sheet.col_count <= COL_LAST_CHECKED:
            sheet.add_cols(COL_LAST_CHECKED + 1 - sheet.col_count)
        sheet.update_cell(1, COL_LAST_CHECKED + 1, "最終チェック日時")
        print("H列にヘッダー「最終チェック日時」を設定しました。")

    # セット数・ASIN入数の書き出し先。Keepaはどのみち全行ぶん叩いているので、
    # ついでに書き出しておけば追加のトークン消費なしで情報が揃う。
    # 適正価格・赤字判定の計算にもこの倍率を使う（2026-08-28〜）。
    annotation_pos = ensure_annotation_columns(sheet, all_rows[0])
    col_manual_ratio = annotation_pos["手修正倍率"]

    # チェック済みかどうかに関わらず、ASINが入っている行すべてを対象プールにする
    pool = [
        (i + 1, row)
        for i, row in enumerate(rows)
        if len(row) > COL_ASIN and row[COL_ASIN].strip() != ""
    ]

    # 動作確認用: 指定した商品管理番号だけに絞り込む（カンマ区切り）
    only_items_raw = os.environ.get("ONLY_ITEM_NUMBERS", "").strip()
    if only_items_raw:
        only_items = {s.strip() for s in only_items_raw.split(",") if s.strip()}
        pool = [
            (i, row) for i, row in pool
            if len(row) > COL_ITEM_ID and row[COL_ITEM_ID].strip() in only_items
        ]
        print(f"ONLY_ITEM_NUMBERS指定により{len(pool)}件に絞り込みました。")

    total = len(pool)
    print(f"巡回対象プール: {total}件")

    if total == 0:
        print("ASINが入っている行がありません。終了。")
        return

    cursor = get_cursor(spreadsheet) % total
    print(f"前回のカーソル位置: {cursor}")

    want = BATCH_SIZE * BATCHES_PER_RUN
    count = min(want, total)
    target = [pool[(cursor + i) % total] for i in range(count)]
    print(f"今回処理: {len(target)}件（末尾まで行ったら先頭に戻って巡回します）")

    processed_count = 0

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
            item_number = row[COL_ITEM_ID].strip() if len(row) > COL_ITEM_ID else ""
            try:
                sheet_price = int(str(row[COL_PRICE_JPY]).replace(",", ""))
            except ValueError:
                sheet_price = 0

            # D列（楽天販売価格）が最新スナップショットとズレていれば、今回の判定にも
            # 反映した上でD列自体も書き直す（古い価格のまま損益分岐点を計算し続けない）
            fresh_price = current_prices.get(item_number) if current_prices else None
            price_updates = []
            if fresh_price is not None and int(fresh_price) != sheet_price:
                rakuten_price = int(fresh_price)
                price_jpy_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_PRICE_JPY + 1)
                price_updates.append({"range": price_jpy_cell, "values": [[rakuten_price]]})
            else:
                rakuten_price = sheet_price

            product = keepa_data.get(asin)

            item_name = row[COL_NAME] if len(row) > COL_NAME else ""

            if product is None:
                # バッチ取得は成功したがこのASINだけデータなし＝廃盤/無効ASIN
                stock_result = "⚠️ 仕入不可"
                price_result = "-"
                proper_price_result = "-"
            else:
                # 手修正倍率（人がシートに直接入力した値）があればそちらを優先する
                manual_ratio_raw = row[col_manual_ratio].strip() if len(row) > col_manual_ratio else ""
                if manual_ratio_raw:
                    try:
                        ratio = float(manual_ratio_raw)
                    except ValueError:
                        _, ratio = compute_pkg_and_ratio(item_name, product)
                else:
                    _, ratio = compute_pkg_and_ratio(item_name, product)
                stock_result, price_result, proper_price_result = judge(product, rakuten_price, exchange_rate, ratio)

            # 1件ずつ即書き込み（途中停止しても結果を無駄にしない）
            stock_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_STOCK_CHECK + 1)
            price_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_PRICE_CHECK + 1)
            proper_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_PROPER_PRICE + 1)
            checked_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_LAST_CHECKED + 1)
            checked_at = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
            # 判定結果と同じリクエストで、セット数・ASIN入数の情報も書く
            # （書き込み回数が増えないので、Sheetsのクォータには影響しない）
            write_with_retry(sheet, price_updates + [
                {"range": stock_cell, "values": [[stock_result]]},
                {"range": price_cell, "values": [[price_result]]},
                {"range": proper_cell, "values": [[proper_price_result]]},
                {"range": checked_cell, "values": [[checked_at]]},
            ] + annotation_updates(annotation_pos, sheet_row_idx + 1, item_name, product))

            print(f"  {asin}: {stock_result} / {price_result} / {proper_price_result}"
                  + (f"（D列 ¥{sheet_price:,}→¥{rakuten_price:,} に更新）" if price_updates else ""))

            processed_count += 1
            time.sleep(SHEET_WRITE_INTERVAL)

        print(f"バッチ書き込み完了")

        if batch_start + BATCH_SIZE < len(target):
            print("次のバッチまで30秒待機...")
            time.sleep(30)

    if only_items_raw:
        print("ONLY_ITEM_NUMBERS指定時はカーソル位置を更新しません（通常の巡回に影響させないため）。")
    else:
        new_cursor = (cursor + processed_count) % total
        save_cursor(spreadsheet, new_cursor)
        print(f"カーソル位置を更新: {cursor} → {new_cursor}（{processed_count}件処理）")

    # フォントをArialに設定（在庫・価格チェック列）
    sheet.format("E2:G10000", {"textFormat": {"fontFamily": "Arial"}})

    print("=== 楽天赤字チェック完了 ===")

if __name__ == "__main__":
    main()
