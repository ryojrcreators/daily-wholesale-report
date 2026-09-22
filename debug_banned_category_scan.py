"""
一時調査用: 2026-09-30から楽天が新たに出品禁止にするカテゴリ
（外国産米／ペットフード等／牛エキス入り食品）に該当しそうな出品を、
「楽天_出品データ」タブ（日次スナップショット）の商品名からキーワードで洗い出す。
実際の削除（hideItem）はまだ行わない。確認が終わったら削除する。
"""

import os
import json

import gspread
from google.oauth2.service_account import Credentials

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
SHEET_NAME = "楽天_出品データ"

# 商品名だけでの機械判定は誤爆リスクがあるため、広めに拾って人が最終確認する前提のキーワード
RICE_KEYWORDS = ["米", "ライス", "Rice", "rice", "こしひかり", "コシヒカリ"]
PET_KEYWORDS = [
    "ペット", "犬用", "猫用", "ドッグフード", "キャットフード", "Dog Food", "Cat Food",
    "犬", "猫", "Dog", "Cat", "Pet",
]
BEEF_EXTRACT_KEYWORDS = ["牛エキス", "ビーフエキス", "Beef Extract", "beef extract"]


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

    rice, pet, beef = [], [], []

    for row in rows:
        if len(row) <= c_name:
            continue
        shop = row[c_shop]
        item = row[c_item]
        name = row[c_name]
        lowered = name.lower()

        if any(kw.lower() in lowered for kw in RICE_KEYWORDS):
            rice.append((shop, item, name))
        if any(kw.lower() in lowered for kw in PET_KEYWORDS):
            pet.append((shop, item, name))
        if any(kw.lower() in lowered for kw in BEEF_EXTRACT_KEYWORDS):
            beef.append((shop, item, name))

    print(f"\n=== 米関連キーワード一致: {len(rice)}件 ===")
    for shop, item, name in rice:
        print(f"{shop}\t{item}\t{name}")

    print(f"\n=== ペット関連キーワード一致: {len(pet)}件 ===")
    for shop, item, name in pet:
        print(f"{shop}\t{item}\t{name}")

    print(f"\n=== 牛エキスキーワード一致: {len(beef)}件（商品名に成分表記が出ることは稀。ほぼ0件想定） ===")
    for shop, item, name in beef:
        print(f"{shop}\t{item}\t{name}")


if __name__ == "__main__":
    main()
