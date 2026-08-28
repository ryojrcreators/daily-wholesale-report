"""
一時調査用: 「ASINあり」シート全行の購入倍率をスキャンし、極端な値（異常の疑いあり）
を洗い出す。Keepaは叩かず、シートに既に書き込まれている値だけを見る。
確認が終わったら削除する。
"""

import os
import json

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"


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
    ss = get_spreadsheet()
    sheet = ss.worksheet(SHEET_NAME)
    all_rows = sheet.get_all_values()
    header = all_rows[0]
    rows = all_rows[1:]

    def col(name):
        return header.index(name) if name in header else None

    c_item = col("商品管理番号")
    c_name = col("商品名")
    c_qty = col("楽天販売個数")
    c_pattern = col("抽出パターン")
    c_pkg = col("ASIN入数")
    c_ratio = col("購入倍率")
    c_manual = col("手修正倍率")

    print(f"総行数: {len(rows)}")
    print(f"列位置: qty={c_qty} pattern={c_pattern} pkg={c_pkg} ratio={c_ratio} manual={c_manual}")

    flagged_high = []
    flagged_low = []
    flagged_manual_diff = []
    total_checked = 0

    for i, row in enumerate(rows, start=2):
        def get(c):
            return row[c].strip() if c is not None and len(row) > c else ""

        ratio_raw = get(c_ratio)
        if not ratio_raw:
            continue
        try:
            ratio = float(ratio_raw)
        except ValueError:
            continue

        total_checked += 1
        item = get(c_item)
        name = get(c_name)
        pattern = get(c_pattern)
        qty = get(c_qty)
        pkg = get(c_pkg)
        manual = get(c_manual)

        info = f"行{i} {item} 「{name[:60]}」 qty={qty} pattern={pattern} pkg={pkg} ratio={ratio} manual={manual}"

        if ratio >= 3:
            flagged_high.append(info)
        elif ratio <= 0.1:
            flagged_low.append(info)

        if pattern in ("×N", "セット×N"):
            print(f"[XN] 行{i} item=[{item}] ratio={ratio} 「{name}」")

        if manual:
            try:
                manual_val = float(manual)
                if abs(manual_val - ratio) > 0.01:
                    flagged_manual_diff.append(info)
            except ValueError:
                pass

    print(f"\n購入倍率が記録済みの行: {total_checked}件")

    print(f"\n=== 倍率が高い（>=3）: {len(flagged_high)}件 ===")
    for line in flagged_high:
        print(line)

    print(f"\n=== 倍率が低い（<=0.1）: {len(flagged_low)}件 ===")
    for line in flagged_low:
        print(line)

    print(f"\n=== 手修正倍率と計算値が食い違っている: {len(flagged_manual_diff)}件 ===")
    for line in flagged_manual_diff:
        print(line)


if __name__ == "__main__":
    main()
