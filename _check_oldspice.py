"""1回限りの確認用。指定商品名を含む行を探し、E列・H列の状態を確認する。"""
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
for i, row in enumerate(values[1:], start=2):
    if len(row) > 1 and "Old Spice" in row[1] and "Fiji" in row[1]:
        print(f"行{i}: {row}")
