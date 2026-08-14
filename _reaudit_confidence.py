"""
一時スクリプト：「ASINなし（要調査）」シートで既に処理済みの行を、
厳しくした信頼度判定ロジック（一致率50%以上・一致3語以上）で再チェックする。

商品名に英語の正式名が埋め込まれているケースのみ再検証可能（extract_english_keywordsが
Noneを返す＝当時DeepL翻訳経由だった行は、翻訳結果が保存されていないため対象外）。

E列（信頼度）がHIGH→LOWに変わる行だけシートを更新する（LOW→HIGHへの格上げはしない。
新基準の方が厳しいので、その逆方向は理論上起こらないはずだが念のため）。
実行後は不要なので削除する。
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

from rakuten_asin_finder import extract_english_keywords, judge_confidence

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

    checked = 0
    skipped_no_english = 0
    downgraded = 0
    updates = []
    downgrade_samples = []

    for i, row in enumerate(rows, start=2):
        asin = row[3].strip() if len(row) > 3 else ""
        confidence = row[4].strip() if len(row) > 4 else ""
        keepa_title = row[5].strip() if len(row) > 5 else ""
        product_name = row[1].strip() if len(row) > 1 else ""

        if not asin or asin == "NOT FOUND" or not keepa_title:
            continue

        search_term = extract_english_keywords(product_name)
        if not search_term:
            skipped_no_english += 1
            continue

        checked += 1
        new_confidence = judge_confidence(search_term, keepa_title)

        if confidence == "HIGH" and new_confidence == "LOW":
            downgraded += 1
            updates.append({"range": f"E{i}", "values": [["LOW"]]})
            if len(downgrade_samples) < 15:
                downgrade_samples.append((product_name[:35], asin, keepa_title[:45]))

    print(f"再検証対象（英語名あり）: {checked}件")
    print(f"再検証不可（DeepL翻訳経由・スキップ）: {skipped_no_english}件")
    print(f"HIGH→LOWに格下げ: {downgraded}件")

    print("\n--- 格下げサンプル ---")
    for name, asin, title in downgrade_samples:
        print(f"  {name} -> {asin} -> {title}")

    if updates:
        CHUNK = 500
        for i in range(0, len(updates), CHUNK):
            ws.batch_update(updates[i:i + CHUNK])
            print(f"  シート更新: {min(i + CHUNK, len(updates))}/{len(updates)}件")

    print("完了。")


if __name__ == "__main__":
    main()
