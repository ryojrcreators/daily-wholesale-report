"""一時デバッグ: スピードスティック単品/3個セットの適正価格計算がおかしい件を調べる。"""
import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"
KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]


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

    asins_seen = set()
    for i, row in enumerate(all_values[1:], start=2):
        name = row[1] if len(row) > 1 else ""
        if "スピードスティック" in name and "フレッシュ" in name:
            print(f"\n行番号: {i}")
            for h, v in zip(header, row):
                print(f"  {h!r}: {v!r}")
            asin_col = header.index("ASIN") if "ASIN" in header else 2
            asin = row[asin_col] if len(row) > asin_col else ""
            if asin:
                asins_seen.add(asin)

    print(f"\n対象ASIN一覧: {sorted(asins_seen)}")
    for asin in sorted(asins_seen):
        res = requests.get(
            "https://api.keepa.com/product",
            params={"key": KEEPA_API_KEY, "domain": 1, "asin": asin, "stats": 1},
            timeout=30,
        )
        products = res.json().get("products") or []
        if products:
            p = products[0]
            stats = p.get("stats", {})
            current = stats.get("current", [])
            print(f"\nASIN {asin}: title={p.get('title')!r}")
            print(f"  packageQuantity={p.get('packageQuantity')!r} numberOfItems={p.get('numberOfItems')!r}")
            print(f"  packageWeight(g)={p.get('packageWeight')!r}")
            print(f"  current[0](AMAZON)={current[0] if len(current)>0 else None} "
                  f"current[1](NEW)={current[1] if len(current)>1 else None}")


if __name__ == "__main__":
    main()
