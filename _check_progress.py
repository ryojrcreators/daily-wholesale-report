"""一時：ASINなしシートの再チェック進捗と結果内訳を確認する（読み取り専用）。"""

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

    processed = 0
    not_found = 0
    high = 0
    low = 0
    samples_high = []

    for row in rows:
        asin = row[3].strip() if len(row) > 3 else ""
        conf = row[4].strip() if len(row) > 4 else ""
        if not asin:
            continue
        processed += 1
        if asin == "NOT FOUND":
            not_found += 1
        elif conf == "HIGH":
            high += 1
            if len(samples_high) < 10:
                samples_high.append((row[1][:40], asin))
        elif conf == "LOW":
            low += 1

    print(f"処理済み: {processed} / {len(rows)}")
    print(f"NOT FOUND: {not_found}")
    print(f"HIGH: {high}")
    print(f"LOW: {low}")
    print("\n--- HIGHサンプル ---")
    for name, asin in samples_high:
        print(f"  {name} -> {asin}")


if __name__ == "__main__":
    main()
