"""
一時調査用: 「ASINあり」シートを読むだけで、仕入不可Close対象の件数を数える。
楽天・YahooのAPIは一切呼ばない（rakuten_shortage_auto_close.pyの対象抽出条件だけを
再現する軽量版）。確認が終わったら削除する。
"""

import os
import json

import gspread
from google.oauth2.service_account import Credentials

from rakuten_shortage_auto_close import (
    SHORTAGE_SHEET_NAME,
    COL_ITEM_NUMBER,
    COL_NAME,
    COL_STOCK_CHECK,
    COL_DONE,
    SHORTAGE_LABEL,
    has_excluded_maker,
)

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]


def get_spreadsheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def main():
    spreadsheet = get_spreadsheet()
    ws = spreadsheet.worksheet(SHORTAGE_SHEET_NAME)
    rows = ws.get_all_values()[1:]

    shortage_total = 0
    already_done = 0
    excluded_maker = 0
    close_targets = 0

    for row in rows:
        def get(c):
            return row[c].strip() if len(row) > c else ""

        stock = get(COL_STOCK_CHECK)
        if stock != SHORTAGE_LABEL:
            continue
        shortage_total += 1

        done = get(COL_DONE)
        if done:
            already_done += 1
            continue

        name = get(COL_NAME)
        if has_excluded_maker(name):
            excluded_maker += 1
            continue

        close_targets += 1

    print(f"総行数: {len(rows)}")
    print(f"仕入不可（E列）: {shortage_total}件")
    print(f"  うちすでに対応済み（I列に値あり）: {already_done}件")
    print(f"  うち除外メーカー: {excluded_maker}件")
    print(f"  → 今回Close対象になる件数: {close_targets}件")


if __name__ == "__main__":
    main()
