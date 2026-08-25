"""一時デバッグ: ロリポップ2商品の現在のシート値とKeepa生データを再確認する。"""
import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"
KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]

TARGETS = ["ワイルドチェリー", "チュッパチャプス"]


def get_spreadsheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def main():
    sheet = get_spreadsheet().worksheet(SHEET_NAME)
    header = sheet.row_values(1)
    all_values = sheet.get_all_values()

    for keyword in TARGETS:
        print(f"\n===== キーワード: {keyword} =====")
        found = False
        for i, row in enumerate(all_values[1:], start=2):
            name = row[1] if len(row) > 1 else ""
            if keyword in name:
                found = True
                print(f"行番号: {i}")
                for h, v in zip(header, row):
                    print(f"  {h!r}: {v!r}")
                asin_col = header.index("ASIN") if "ASIN" in header else 2
                asin = row[asin_col] if len(row) > asin_col else ""
                if asin:
                    res = requests.get(
                        "https://api.keepa.com/product",
                        params={"key": KEEPA_API_KEY, "domain": 1, "asin": asin, "stats": 1},
                        timeout=30,
                    )
                    products = res.json().get("products") or []
                    if products:
                        p = products[0]
                        print(f"  [Keepa] packageQuantity={p.get('packageQuantity')!r} "
                              f"numberOfItems={p.get('numberOfItems')!r}")
        if not found:
            print("該当行なし")


if __name__ == "__main__":
    main()
