"""
一時スクリプト：「ASINなし（要調査）」シートでD列が「NOT FOUND」の行を
D列・E列とも空欄に戻し、rakuten_asin_finder.pyの再チェック対象に戻す。

改善後の検索ロジック（埋め込み済み英語名の優先利用）で再チャレンジさせるため。
実行後は不要なので削除する。
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINなし（要調査）"


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
    rows = ws.get_all_values()[1:]

    targets = []
    for i, row in enumerate(rows, start=2):
        asin = row[3].strip() if len(row) > 3 else ""
        if asin == "NOT FOUND":
            targets.append(i)

    print(f"リセット対象（NOT FOUND）: {len(targets)}件")

    if not targets:
        print("対象なし。終了。")
        return

    updates = []
    for row_num in targets:
        updates.append({"range": f"D{row_num}:E{row_num}", "values": [["", ""]]})

    # 1回のbatch_updateで大量セルをまとめて更新（Sheets APIの呼び出し回数制限対策）
    CHUNK = 500
    for i in range(0, len(updates), CHUNK):
        ws.batch_update(updates[i:i + CHUNK])
        print(f"  {min(i + CHUNK, len(updates))}/{len(updates)}件 完了")

    print("完了：D列・E列をリセットしました。")


if __name__ == "__main__":
    main()
