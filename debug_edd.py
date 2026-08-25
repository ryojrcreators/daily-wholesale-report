"""一時デバッグ: Keepa Product APIのレスポンスにEDD（お届け予定日/leadtime）関連の
フィールドが含まれるか、aj0000410（B08ZH... 等）の生レスポンスを全部見て確認する。"""
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
        if item_number == "aj0000410":
            target_row = i
            row_data = row
            break

    if target_row is None:
        print("aj0000410が見つかりませんでした。")
        return

    asin_col = header.index("ASIN") if "ASIN" in header else 2
    asin = row_data[asin_col] if len(row_data) > asin_col else ""
    print(f"行番号: {target_row} / ASIN: {asin}")
    print(f"商品名: {row_data[1] if len(row_data) > 1 else ''}")

    res = requests.get(
        "https://api.keepa.com/product",
        params={"key": KEEPA_API_KEY, "domain": 1, "asin": asin, "stats": 1, "offers": 20},
        timeout=30,
    )
    data = res.json()
    products = data.get("products") or []
    if not products:
        print("Keepaから商品データを取得できませんでした。")
        print(json.dumps(data, ensure_ascii=False)[:1000])
        return

    p = products[0]

    # deliveryやleadtime、availabilityっぽいキーを全部拾う
    print("\n--- delivery/leadtime/availability関連キー ---")
    for k, v in p.items():
        lk = k.lower()
        if any(word in lk for word in ["delivery", "lead", "avail", "ship", "eta", "eddm"]):
            print(f"  {k}: {v!r}")

    print("\n--- offers（3rdパーティ出品）に配送関連情報があるか ---")
    offers = p.get("offers") or []
    print(f"offers件数: {len(offers)}")
    if offers:
        first = offers[0]
        print(f"最初のofferの全キー: {list(first.keys())}")
        print(json.dumps(first, ensure_ascii=False, default=str)[:1500])

    print("\n--- 全トップレベルキー一覧 ---")
    print(sorted(p.keys()))


if __name__ == "__main__":
    main()
