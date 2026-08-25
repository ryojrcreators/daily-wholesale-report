"""
一時デバッグ用: 「Original Gourmet ロリポップ ワイルドチェリー 30個入り」の
購入倍率が30になっている原因を確認する。該当行のASIN・ASIN入数・Amazon商品名を
シートから読み、さらにKeepaの生レスポンス（packageQuantity/numberOfItems）も確認する。
"""
import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"
KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]


def get_spreadsheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def main():
    sheet = get_spreadsheet().worksheet(SHEET_NAME)
    header = sheet.row_values(1)
    all_values = sheet.get_all_values()

    target_idx = None
    for i, row in enumerate(all_values[1:], start=2):
        name = row[1] if len(row) > 1 else ""
        if "ロリポップ" in name and "ワイルドチェリー" in name:
            target_idx = i
            row_data = row
            break

    if target_idx is None:
        print("該当行が見つかりませんでした（商品名に「ロリポップ」「ワイルドチェリー」を含む行なし）")
        return

    print(f"行番号: {target_idx}")
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
    print("\n--- Keepa生データ ---")
    print(f"  title: {p.get('title')!r}")
    print(f"  packageQuantity: {p.get('packageQuantity')!r}")
    print(f"  numberOfItems: {p.get('numberOfItems')!r}")
    print(f"  packageHeight/Width/Length/Weight: "
          f"{p.get('packageHeight')}/{p.get('packageWidth')}/{p.get('packageLength')}/{p.get('packageWeight')}")


if __name__ == "__main__":
    main()
