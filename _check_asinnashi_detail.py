"""一時：ASINなしシートのD列(ASIN候補)の内訳をさらに詳しく確認する（読み取り専用）。"""

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

    not_found = 0
    has_candidate_low = 0
    samples = []

    for row in rows:
        asin = row[3].strip() if len(row) > 3 else ""
        if asin == "NOT FOUND":
            not_found += 1
        elif asin:
            has_candidate_low += 1
            if len(samples) < 15:
                name = row[1].strip() if len(row) > 1 else ""
                samples.append((name, asin))

    print(f"NOT FOUND: {not_found}")
    print(f"ASIN候補あり（信頼度LOW）: {has_candidate_low}")
    print("\n--- サンプル（商品名 → ASIN候補）---")
    for name, asin in samples:
        print(f"  {name[:50]} → {asin}")


if __name__ == "__main__":
    main()
