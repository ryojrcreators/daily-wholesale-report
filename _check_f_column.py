"""一時：ASINなしシートのF列（Amazon商品名）が正しく書き込まれているか確認する（読み取り専用）。"""

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
    all_rows = ws.get_all_values()
    header = all_rows[0]
    rows = all_rows[1:]

    print(f"ヘッダー: {header}")

    processed = 0
    with_name = 0
    samples = []

    for row in rows:
        asin = row[3].strip() if len(row) > 3 else ""
        if not asin:
            continue
        processed += 1
        name = row[5].strip() if len(row) > 5 else ""
        if name:
            with_name += 1
            if len(samples) < 8:
                samples.append((row[1][:35], asin, name[:50]))

    print(f"処理済み: {processed}")
    print(f"Amazon商品名あり: {with_name}")
    print("\n--- サンプル ---")
    for orig, asin, name in samples:
        print(f"  {orig} -> {asin} -> {name}")


if __name__ == "__main__":
    main()
