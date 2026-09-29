"""
一時調査用: 米カテゴリのキーワードに「ライスプロテイン」等を追加して楽天も再確認する
（元のスキャンにはこのキーワードが無く、NutriBiotic/Source Naturals等を見落としていた
可能性があるため）。
"""

import os
import re
import json

import gspread
from google.oauth2.service_account import Credentials

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
SHEET_NAME = "楽天_出品データ"

RICE_PATTERNS = [
    r"白米", r"玄米", r"もち米", r"精米", r"無洗米", r"ジャポニカ米", r"カルローズ",
    r"こしひかり", r"コシヒカリ",
    r"\b(white|brown|jasmine|basmati|sushi|long\s*grain|short\s*grain|calrose)\s*rice\b",
    r"ライスクリスプ", r"Rice Crisps", r"ライスシロップ", r"Rice Syrup",
    r"ライスプロテイン", r"Rice Protein",
]
pattern_re = re.compile("|".join(RICE_PATTERNS), re.IGNORECASE)


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

    print(f"総行数: {len(rows)}")
    hits = []
    for row in rows:
        if len(row) <= c_name:
            continue
        name = row[c_name]
        if pattern_re.search(name):
            hits.append((row[c_shop], row[c_item], name))

    print(f"\n=== 該当: {len(hits)}件 ===")
    for shop, item, name in hits:
        print(f"{shop}\t{item}\t{name}")


if __name__ == "__main__":
    main()
