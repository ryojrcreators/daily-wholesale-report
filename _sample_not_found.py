"""一時：ASINなしシートのNOT FOUND商品名をサンプル表示する（読み取り専用）。"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINなし（要調査）"


def get_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def main():
    ws = get_sheet()
    rows = ws.get_all_values()[1:]

    names = [row[1].strip() for row in rows if len(row) > 1 and row[1].strip()]
    print(f"総件数: {len(names)}")
    print("\n--- 先頭から30件 ---")
    for n in names[:30]:
        print(f"  {n}")
    print("\n--- ランダム風に後方から30件 ---")
    for n in names[-30:]:
        print(f"  {n}")


if __name__ == "__main__":
    main()
