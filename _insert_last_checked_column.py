"""
一時スクリプト：「ASINあり」シートのH列の左に新しい列を1つ挿入し、
ヘッダー「最終チェック日時」を設定する（既存のH列「在庫対応済み」はI列へ、
I列「価格調整対応済み」はJ列へ、それぞれ1つ右にずれる）。

実行後は不要なので削除する。
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"


def get_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def main():
    ws = get_sheet()
    header = ws.row_values(1)
    print(f"挿入前のヘッダー: {header}")

    if len(header) >= 8 and header[7].strip() == "最終チェック日時":
        print("既にH列が「最終チェック日時」になっています。何もしません。")
        return

    ws.spreadsheet.batch_update({
        "requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": 7,  # 0始まり：H列の位置
                    "endIndex": 8,
                },
                "inheritFromBefore": False,
            }
        }]
    })
    ws.update_cell(1, 8, "最終チェック日時")

    header_after = ws.row_values(1)
    print(f"挿入後のヘッダー: {header_after}")
    print("完了：H列に「最終チェック日時」を追加しました。")


if __name__ == "__main__":
    main()
