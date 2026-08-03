"""1回限りの移行スクリプト。
「ASINなし（要調査）」タブのうち、ASIN列が「NOT FOUND」でない行を
「ASINあり」タブの形式（商品管理番号(商品URL) / 商品名 / ASIN / 通常購入販売価格 / 在庫チェック / 価格チェック / 適正価格）
に変換して末尾に追記し、「ASINなし（要調査）」タブは「NOT FOUND」の行だけを残す。

DRY_RUN=true（既定）では件数の確認だけ行い、シートは一切変更しない。
"""
import os
import json
import time
import gspread
from google.oauth2.service_account import Credentials

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
FOUND_SHEET_NAME = "ASINあり"
NOTFOUND_SHEET_NAME = "ASINなし（要調査）"

creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
creds = Credentials.from_service_account_info(
    json.loads(creds_json),
    scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

notfound_ws = spreadsheet.worksheet(NOTFOUND_SHEET_NAME)
found_ws = spreadsheet.worksheet(FOUND_SHEET_NAME)

notfound_values = notfound_ws.get_all_values()
notfound_header = notfound_values[0]
notfound_rows = notfound_values[1:]
print(f"「{NOTFOUND_SHEET_NAME}」総行数（ヘッダー除く）: {len(notfound_rows)}")

# 列インデックス（peekで確認済み）: 0=商品管理番号 1=商品名 2=価格 3=ASIN 4=信頼度
to_move = []
to_keep = []
for row in notfound_rows:
    asin = row[3].strip() if len(row) > 3 else ""
    if asin.upper() == "NOT FOUND":
        to_keep.append(row)
    else:
        item_number = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else ""
        price = row[2] if len(row) > 2 else ""
        to_move.append([item_number, name, asin, price, "", "", ""])

print(f"移動対象（ASINあり）: {len(to_move)}行")
print(f"残す対象（NOT FOUND）: {len(to_keep)}行")

if DRY_RUN:
    print("【DRY RUN】サンプル（移動対象の先頭3行）:")
    for r in to_move[:3]:
        print(f"  {r}")
    print("実際の書き込みは行いません。")
    raise SystemExit(0)


def append_in_batches(worksheet, rows, batch_rows=2000):
    start = 0
    while start < len(rows):
        chunk = rows[start:start + batch_rows]
        for attempt in range(5):
            try:
                worksheet.append_rows(chunk, value_input_option="USER_ENTERED")
                break
            except gspread.exceptions.APIError as e:
                if attempt == 4:
                    raise
                wait = 5 * (attempt + 1)
                print(f"  追記リトライ ({attempt + 1}/5) {wait}秒待機: {e}")
                time.sleep(wait)
        start += len(chunk)
        print(f"  {min(start, len(rows))}/{len(rows)} 行を追記しました")


def write_rows_in_batches(worksheet, data, batch_rows=5000, retries=5):
    n_rows = len(data)
    n_cols = max((len(r) for r in data), default=1)
    data = [row + [""] * (n_cols - len(row)) for row in data]
    worksheet.resize(rows=max(n_rows, 1), cols=max(n_cols, 1))
    start = 0
    while start < n_rows:
        chunk = data[start:start + batch_rows]
        rng = f"A{start + 1}"
        for attempt in range(retries):
            try:
                worksheet.update(rng, chunk)
                break
            except gspread.exceptions.APIError as e:
                if attempt == retries - 1:
                    raise
                wait = 5 * (attempt + 1)
                print(f"  書き込みリトライ ({attempt + 1}/{retries}) {wait}秒待機: {e}")
                time.sleep(wait)
        start += len(chunk)


print("「ASINあり」タブに追記しています...")
append_in_batches(found_ws, to_move)

print(f"「{NOTFOUND_SHEET_NAME}」タブをNOT FOUND分だけに書き換えています...")
write_rows_in_batches(notfound_ws, [notfound_header] + to_keep)

print("=== 完了 ===")
