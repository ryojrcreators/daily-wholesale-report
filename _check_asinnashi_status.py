"""一時：ASINなし（要調査）シートの信頼度内訳を確認する（読み取り専用）。"""

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

    total = len(rows)
    no_asin_candidate = 0
    high = 0
    low = 0
    other = 0

    for row in rows:
        asin = row[3].strip() if len(row) > 3 else ""
        conf = row[4].strip() if len(row) > 4 else ""
        if not asin:
            no_asin_candidate += 1
        elif conf == "HIGH":
            high += 1
        elif conf == "LOW":
            low += 1
        else:
            other += 1

    print(f"総行数: {total}")
    print(f"ASIN候補なし（未処理）: {no_asin_candidate}")
    print(f"信頼度HIGH: {high}")
    print(f"信頼度LOW: {low}")
    print(f"その他: {other}")


if __name__ == "__main__":
    main()
