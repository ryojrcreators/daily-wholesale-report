"""
一時調査用: 2026-09-30から楽天が新たに出品禁止にするカテゴリ
（外国産米／ペットフード等／牛エキス入り食品）に該当しそうな出品を、
「楽天_出品データ」タブ（日次スナップショット）の商品名からキーワードで洗い出す。
実際の削除（hideItem）はまだ行わない。確認が終わったら削除する。
"""

import os
import re
import json

import gspread
from google.oauth2.service_account import Credentials

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
SHEET_NAME = "楽天_出品データ"

# 1回目のスキャンで「米」「犬」「猫」「Pet」などの1文字・短語だけで拾うと、
# 「米国」「北米」（国名）やペット用おもちゃ・給餌器・グルーミング用品（禁止対象外）まで
# 大量に誤検出したため、禁止対象（米そのもの／ペットのフード・おやつ・サプリ・ヘアケア用品）
# を具体的に指す複合語・正規表現に絞り込んでいる。
RICE_PATTERNS = [
    r"白米", r"玄米", r"もち米", r"精米", r"無洗米", r"ジャポニカ米", r"カルローズ",
    r"こしひかり", r"コシヒカリ",
    r"(?i)\b(white|brown|jasmine|basmati|sushi|long\s*grain|short\s*grain|calrose)\s*rice\b",
]
PET_FOOD_PATTERNS = [
    r"ペットフード", r"ドッグフード", r"キャットフード",
    r"犬用おやつ", r"猫用おやつ", r"犬のおやつ", r"猫のおやつ", r"ペット用おやつ",
    r"(?i)\bdog\s*food\b", r"(?i)\bcat\s*food\b", r"(?i)\bdog\s*treat", r"(?i)\bcat\s*treat",
    r"ジャーキー.{0,10}(犬|猫|ペット)", r"(犬|猫|ペット).{0,10}ジャーキー",
]
PET_SUPP_PATTERNS = [
    r"ペット用サプリ", r"犬用サプリ", r"猫用サプリ", r"(犬|猫|ペット).{0,5}サプリ",
    r"(?i)\bpet\s*supplement", r"(?i)\bdog\s*supplement", r"(?i)\bcat\s*supplement",
]
PET_HAIRCARE_PATTERNS = [
    r"ペット用シャンプー", r"犬用シャンプー", r"猫用シャンプー", r"(犬|猫|ペット).{0,5}シャンプー",
    r"(犬|猫|ペット).{0,5}コンディショナー",
    r"(?i)\bpet\s*shampoo", r"(?i)\bdog\s*shampoo", r"(?i)\bcat\s*shampoo",
]
BEEF_EXTRACT_PATTERNS = [r"牛エキス", r"ビーフエキス", r"(?i)beef\s*extract"]


def any_match(patterns, text):
    return any(re.search(p, text) for p in patterns)


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
    print(f"ヘッダー: {header}")
    print(f"総行数: {len(rows)}")

    def col(name):
        return header.index(name) if name in header else None

    c_shop = col("店舗名")
    c_item = col("商品管理番号")
    c_name = col("商品名")

    rice, pet_food, pet_supp, pet_hair, beef = [], [], [], [], []

    for row in rows:
        if len(row) <= c_name:
            continue
        shop = row[c_shop]
        item = row[c_item]
        name = row[c_name]

        if any_match(RICE_PATTERNS, name):
            rice.append((shop, item, name))
        if any_match(PET_FOOD_PATTERNS, name):
            pet_food.append((shop, item, name))
        if any_match(PET_SUPP_PATTERNS, name):
            pet_supp.append((shop, item, name))
        if any_match(PET_HAIRCARE_PATTERNS, name):
            pet_hair.append((shop, item, name))
        if any_match(BEEF_EXTRACT_PATTERNS, name):
            beef.append((shop, item, name))

    for label, items in [
        ("米（穀物そのもの）", rice),
        ("ペットフード／おやつ", pet_food),
        ("ペット用サプリ", pet_supp),
        ("ペット用ヘアケア（シャンプー等）", pet_hair),
        ("牛エキス（商品名に出ることは稀。ほぼ0件想定）", beef),
    ]:
        print(f"\n=== {label}: {len(items)}件 ===")
        for shop, item, name in items:
            print(f"{shop}\t{item}\t{name}")


if __name__ == "__main__":
    main()
