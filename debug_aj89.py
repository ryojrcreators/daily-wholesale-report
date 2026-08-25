"""一時デバッグ: aj0000089が実際はUnavailableなのに「仕入可能」判定されて再開されてしまった件を調べる。"""
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

    target_row = None
    for i, row in enumerate(all_values[1:], start=2):
        item_number = row[0] if len(row) > 0 else ""
        if item_number == "aj0000089":
            target_row = i
            row_data = row
            break

    if target_row is None:
        print("aj0000089が見つかりませんでした。")
        return

    print(f"行番号: {target_row}")
    for h, v in zip(header, row_data):
        print(f"  {h!r}: {v!r}")

    asin_col = header.index("ASIN") if "ASIN" in header else 2
    asin = row_data[asin_col] if len(row_data) > asin_col else ""
    print(f"\n対象ASIN: {asin}")

    if not asin:
        print("ASIN列が空です。")
        return

    res = requests.get(
        "https://api.keepa.com/product",
        params={"key": KEEPA_API_KEY, "domain": 1, "asin": asin, "stats": 1},
        timeout=30,
    )
    data = res.json()
    products = data.get("products") or []
    if not products:
        print("Keepaから商品データを取得できませんでした。")
        print(json.dumps(data, ensure_ascii=False)[:500])
        return

    p = products[0]
    stats = p.get("stats", {})
    current = stats.get("current", [])
    print("\n--- Keepa生データ（現在） ---")
    print(f"  title: {p.get('title')!r}")
    print(f"  availabilityAmazon: {p.get('availabilityAmazon')!r}")
    print(f"  current[0](AMAZON)={current[0] if len(current)>0 else None}")
    print(f"  current[1](NEW)={current[1] if len(current)>1 else None}")
    print(f"  current[11](COUNT_NEW)={current[11] if len(current)>11 else None}")
    print(f"  current[7](FBM?)={current[7] if len(current)>7 else None}")
    print(f"  offers件数: {len(p.get('offers') or [])}")


if __name__ == "__main__":
    main()
