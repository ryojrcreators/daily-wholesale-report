"""1回限りの読み取り専用確認用。ASINありシートの列と仕入不可行のサンプルを見る。"""
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
print(f"総行数: {len(values)}")
header = values[0]
print(f"ヘッダー: {header}")

shortage_rows = [row for row in values[1:] if len(row) > 4 and "仕入不可" in row[4]]
print(f"仕入不可行数: {len(shortage_rows)}")
for row in shortage_rows[:5]:
    print(row)
