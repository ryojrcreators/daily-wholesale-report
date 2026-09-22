"""
一時調査用: 楽天で確定した「9/30出品禁止カテゴリ」該当商品（27種類）が、
Yahoo・Wowmaにも出品されているか、商品名の特徴的なキーワードで探す。
Yahooは「Yahoo_出品データ」タブ（日次スナップショット）を見る。
Wowmaはスナップショットが無いため、searchItemInfosをその場でページングして全件見る。
実際の削除・在庫0化はまだ行わない。
"""

import os
import re
import json

import gspread
from google.oauth2.service_account import Credentials

from case_orders_wowma import wowma_search_items

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
YAHOO_SHEET_NAME = "Yahoo_出品データ"

# 楽天で確定した27商品の、ブランド名・特徴的な語の組み合わせ
PATTERNS = [
    r"Keystone Pantry", r"ブラウンライスシロップ",
    r"Jovial", r"カッペリーニ", r"玄米ショートパスタ",
    r"Minsley",
    r"玄米茶", r"Genmaicha",
    r"ライスクリスプ", r"Rice Crisps",
    r"発芽玄米プロテイン", r"Sprouted.{0,3}Brown Rice Protein",
    r"Carna4",
    r"Pet Head",
    r"チョウメイン.{0,10}テリヤキビーフ", r"Teriyaki Beef",
    r"ビーフボーン.{0,3}ブロスパウダー", r"Bone Broth.{0,3}Beef",
    r"トップラーメン.{0,10}ビーフ", r"スターフライ.{0,10}ビーフ",
    r"Peak Refuel",
    r"テリヤキライス.{0,10}ビーフ",
    r"炒めごはん.{0,10}スパイシービーフ",
    r"Heinz.{0,10}グレービー", r"Beef Gravy",
    r"牛肉風味.{0,10}代用肉", r"Vegetarian Meat Substitute",
]
pattern_re = re.compile("|".join(PATTERNS), re.IGNORECASE)


def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def scan_yahoo():
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
        if pattern_re.search(name):
            hits.append((row[c_shop], row[c_code], name))

    print(f"\n=== [Yahoo] 該当: {len(hits)}件 ===")
    for shop, code, name in hits:
        print(f"{shop}\t{code}\t{name}")


def scan_wowma():
    start = 1
    total_hits = []
    scanned = 0
    while True:
        items, max_count = wowma_search_items(start, 500)
        if start == 1:
            print(f"[Wowma] 総商品数: {max_count}")
        if not items:
            break
        for item in items:
            name = item.get("itemName", "")
            if pattern_re.search(name):
                total_hits.append((item.get("itemCode", ""), name))
        scanned += len(items)
        if scanned >= max_count:
            break
        start += 500

    print(f"\n=== [Wowma] 該当: {len(total_hits)}件（走査{scanned}件） ===")
    for code, name in total_hits:
        print(f"{code}\t{name}")


def main():
    scan_yahoo()
    scan_wowma()


if __name__ == "__main__":
    main()
