"""
一時調査用: 「楽天_出品データ」タブの商品名から「ビーフ」「Beef」を含む商品を洗い出す。
牛エキス入り食品（2026-09-30出品禁止）に該当するか人が判断する材料。
"""

import os
import re
import json

import gspread
from google.oauth2.service_account import Credentials

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
SHEET_NAME = "楽天_出品データ"


def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def main():
    ss = get_spreadsheet()
    sheet = ss.worksheet(SHEET_NAME)
    all_rows = sheet.get_all_values()
    header = all_rows[0]
    rows = all_rows[1:]

    def col(name):
        return header.index(name) if name in header else None

    c_shop = col("店舗名")
    c_item = col("商品管理番号")
    c_name = col("商品名")

    pattern = re.compile(r"ビーフ|(?i)\bbeef\b")

    hits = []
    for row in rows:
        if len(row) <= c_name:
            continue
        name = row[c_name]
        if pattern.search(name):
            hits.append((row[c_shop], row[c_item], name))

    print(f"総行数: {len(rows)}")
    print(f"\n=== ビーフ/Beef 一致: {len(hits)}件 ===")
    for shop, item, name in hits:
        print(f"{shop}\t{item}\t{name}")


if __name__ == "__main__":
    main()
