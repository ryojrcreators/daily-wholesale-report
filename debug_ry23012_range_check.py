"""
一時調査用: ry23012300番台はペット用品（鳥の餌等）が集中しているブロックだが、
キーワードベースのスキャンでは既に複数回見落としが発生した。
番号帯そのもので全件抽出し、目視で確認する（Yahoo・楽天の両方）。
"""

import os
import json

import gspread
from google.oauth2.service_account import Credentials

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]


def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def scan(sheet_name, code_col_name):
    ss = get_spreadsheet()
    sheet = ss.worksheet(sheet_name)
    all_rows = sheet.get_all_values()
    header = all_rows[0]
    rows = all_rows[1:]

    def col(name):
        return header.index(name) if name in header else None

    c_shop = col("店舗名")
    c_code = col(code_col_name)
    c_name = col("商品名")

    hits = []
    for row in rows:
        if len(row) <= c_name:
            continue
        code = row[c_code]
        if code.startswith("ry23012") or code.startswith("ry2301230") or "ry23012" in code:
            hits.append((row[c_shop], code, row[c_name]))

    print(f"\n=== [{sheet_name}] ry23012系: {len(hits)}件 ===")
    for shop, code, name in sorted(hits, key=lambda x: x[1]):
        print(f"{shop}\t{code}\t{name}")


def main():
    scan("Yahoo_出品データ", "商品コード")
    scan("楽天_出品データ", "商品管理番号")


if __name__ == "__main__":
    main()
