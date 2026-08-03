"""1回限りの読み取り専用確認用。ASINなし（要調査）タブの列とサンプル行を見る。"""
import os
import json
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINなし（要調査）"

creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
creds_dict = json.loads(creds_json)
scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

print("シート一覧:", [ws.title for ws in spreadsheet.worksheets()])

ws = spreadsheet.worksheet(SHEET_NAME)
values = ws.get_all_values()
print(f"総行数: {len(values)}")
header = values[0]
print(f"ヘッダー: {header}")

for row in values[1:6]:
    print(row)

# ASIN列らしきものを探して、値が入っている行数を数える
for i, col in enumerate(header):
    if "asin" in col.lower() or "ASIN" in col:
        filled = sum(1 for row in values[1:] if len(row) > i and row[i].strip())
        print(f"列[{i}]『{col}』に値がある行数: {filled} / {len(values)-1}")

from collections import Counter
conf_counts = Counter(row[4].strip() if len(row) > 4 else "" for row in values[1:])
print("信頼度の内訳:", conf_counts)

asin_counts = Counter(row[3].strip().upper() if len(row) > 3 else "" for row in values[1:] if not row[3].strip() or row[3].strip().upper() in ("NOT FOUND", "N/A", "NONE", "-"))
print("ASIN列に「NOT FOUND」系が入っている値の内訳:", asin_counts)
