"""
一時調査用: Yahoo側のペット用品スキャンにZuPreem/Kaytee/Vitakraftなどのブランド名
キーワードが抜けていたことが判明したため（楽天・Wowma側には入っていた）、
同じ拡張済みキーワードで再スキャンし、見落としを洗い出す。
"""

import os
import re
import json

import gspread
from google.oauth2.service_account import Credentials

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
YAHOO_SHEET_NAME = "Yahoo_出品データ"

PATTERNS = [
    r"Greenies", r"グリニーズ",
    r"Milk-Bone", r"ミルクボーン",
    r"Sunseed", r"サンシード",
    r"Degu", r"デグー",
    r"鳥の餌", r"鳥用.{0,5}(餌|フード|飼料)", r"バードフード", r"Bird Food",
    r"インコ.{0,5}(餌|フード)", r"小鳥.{0,5}(餌|フード)", r"文鳥.{0,5}(餌|フード)",
    r"Vitakraft", r"Kaytee", r"ケイティー", r"ZuPreem", r"ズプリーム",
    r"Parrot Food", r"オウムフード", r"オウム.{0,3}フード",
    r"マーモセット.{0,5}フード", r"フェレット.{0,5}(フード|ダイエット)",
]
NOISE = re.compile(r"フード付き|フードフィーダー|餌箱|えさやり器|ディスペンサー|回し車")
pattern_re = re.compile("|".join(PATTERNS), re.IGNORECASE)


def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def main():
    ss = get_spreadsheet()
    sheet = ss.worksheet(YAHOO_SHEET_NAME)
    all_rows = sheet.get_all_values()
    header = all_rows[0]
    rows = all_rows[1:]

    def col(name):
        return header.index(name) if name in header else None

    c_shop = col("店舗名")
    c_code = col("商品コード")
    c_name = col("商品名")

    print(f"[Yahoo] 総行数: {len(rows)}")
    hits = []
    for row in rows:
        if len(row) <= c_name:
            continue
        name = row[c_name]
        if pattern_re.search(name) and not NOISE.search(name):
            hits.append((row[c_shop], row[c_code], name))

    print(f"\n=== [Yahoo] 該当: {len(hits)}件 ===")
    for shop, code, name in hits:
        print(f"{shop}\t{code}\t{name}")


if __name__ == "__main__":
    main()
