"""1回限りの読み取り専用確認用。現在の仕入不可件数を数える。"""
import os
import json
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"

creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
creds_dict = json.loads(creds_json)
scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)
ws = spreadsheet.worksheet(SHEET_NAME)

values = ws.get_all_values()
rows = values[1:]

shortage_total = sum(1 for row in rows if len(row) > 4 and row[4].strip() == "⚠️ 仕入不可")
shortage_done = sum(1 for row in rows if len(row) > 4 and row[4].strip() == "⚠️ 仕入不可" and len(row) > 7 and row[7].strip())
shortage_pending = shortage_total - shortage_done

print(f"総行数: {len(rows)}")
print(f"仕入不可（合計）: {shortage_total}")
print(f"  うち在庫対応済み（Close済み）: {shortage_done}")
print(f"  うち未対応: {shortage_pending}")
