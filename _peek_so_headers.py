"""1回限りの読み取り専用確認用。SOタブの列名（と先頭1行）を確認する。書き込みは一切しない。"""
import os
import json
import gspread
from google.oauth2.service_account import Credentials

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
PO_SO_SPREADSHEET_ID = os.environ.get("PO_SO_SPREADSHEET_ID") or SPREADSHEET_ID

credentials_info = json.loads(GOOGLE_CREDENTIALS)
scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
gc = gspread.authorize(credentials)

spreadsheet = gc.open_by_key(PO_SO_SPREADSHEET_ID)
worksheet = spreadsheet.worksheet("SO")
values = worksheet.get_all_values()

print(f"総行数: {len(values)}")
if values:
    header = values[0]
    print(f"列数: {len(header)}")
    for i, col in enumerate(header):
        print(f"  [{i}] {col}")
    if len(values) > 1:
        print("\n--- サンプル1行目 ---")
        for i, val in enumerate(values[1]):
            label = header[i] if i < len(header) else f"col{i}"
            print(f"  {label}: {val}")
