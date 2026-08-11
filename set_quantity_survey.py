"""
「ASINあり」シートの商品名から、セット商品（○個セット等）の表記パターンを洗い出す（読み取りのみ）。

背景:
  rakuten_price_check.py は Keepa が返す「1個ぶん」の価格・重量でしか原価を計算しておらず、
  セット商品でも単品と同じ適正価格になってしまう（例: スピードスティックの単品と3個セットが
  どちらも ¥6,432）。セット数を掛けて計算するよう直したいが、セット数をどこから取るかを
  決めるために、まず実データの表記を調べる。

出力:
  1. 抽出できたセット数ごとの件数
  2. 同じASINで複数行あるもの（＝単品とセットが併存している疑いが濃い組）
  3. 「セット」等の語を含むのに数値を取り出せなかった行（パターンの取りこぼし）

このスクリプトはシートを読むだけで、何も書き換えない。
"""

import os
import json
import re
from collections import Counter, defaultdict

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"

COL_ITEM_ID = 0
COL_NAME = 1
COL_ASIN = 2
COL_PRICE_JPY = 3

# 数量を表しそうな単位。「3個セット」「2本セット」「4パック」など。
UNIT_PATTERN = r"個|本|袋|箱|缶|パック|pack|Pack|PACK|セット|set|Set|SET|入|枚|包|瓶"

# 半角・全角の数字に対応する
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 「セット」等の語を含むかどうか（取りこぼし検出用）
SET_WORD = re.compile(r"セット|[Ss][Ee][Tt]|パック|[Pp]ack|まとめ買い|×|x\d|\d本|\d個")


def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def extract_quantity(name: str):
    """
    商品名からセット数を推測する。見つからなければ None。
    どのパターンで当たったかも返す（表記の傾向を見るため）。
    """
    text = name.translate(FULLWIDTH_DIGITS)

    patterns = [
        # 3個セット / 2本セット / 4パック / 6缶セット
        (rf"(\d+)\s*(?:{UNIT_PATTERN})\s*(?:セット|SET|set|Set)", "N個セット"),
        # セット3個 / セット×3
        (r"(?:セット|SET|set)\s*[×xX]\s*(\d+)", "セット×N"),
        # ×3 / x3 （末尾によくある）
        (r"[×xX]\s*(\d+)\s*(?:個|本|袋|箱|缶|パック)?\s*$", "×N"),
        # 3個入り / 12本入
        (rf"(\d+)\s*(?:{UNIT_PATTERN})\s*入", "N個入"),
        # 3パック / 2個 （単位だけ。誤検出しやすいので最後に回す）
        (rf"(\d+)\s*(?:個|本|パック|缶|袋)(?![ぁ-んァ-ヶ一-龠])", "N個"),
    ]

    for pattern, label in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                qty = int(m.group(1))
            except ValueError:
                continue
            # 明らかに数量ではないもの（容量表記など）を弾く
            if 2 <= qty <= 50:
                return qty, label, m.group(0)
    return None, None, None


def main():
    print("=== セット数の表記調査 開始 ===")
    rows = get_sheet().get_all_values()[1:]
    print(f"総行数: {len(rows)}\n")

    qty_counter = Counter()
    label_counter = Counter()
    samples = defaultdict(list)
    missed = []
    by_asin = defaultdict(list)

    for row in rows:
        if len(row) <= COL_ASIN:
            continue
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        asin = row[COL_ASIN].strip()
        item_id = row[COL_ITEM_ID].strip()
        price = row[COL_PRICE_JPY] if len(row) > COL_PRICE_JPY else ""

        qty, label, matched = extract_quantity(name)
        if qty:
            qty_counter[qty] += 1
            label_counter[label] += 1
            if len(samples[qty]) < 3:
                samples[qty].append(f"{item_id} 「{matched}」 {name[:45]}")
        elif SET_WORD.search(name) and len(missed) < 30:
            missed.append(f"{item_id} {name[:60]}")

        if asin:
            by_asin[asin].append((item_id, qty or 1, price, name[:40]))

    print("── 1. 抽出できたセット数 ──")
    total = sum(qty_counter.values())
    print(f"セット商品と判定: {total}件 / {len(rows)}件\n")
    for qty, count in sorted(qty_counter.items()):
        print(f"  {qty:>3}個セット: {count:>5}件")
        for s in samples[qty]:
            print(f"        {s}")

    print("\n── 2. 当たったパターンの内訳 ──")
    for label, count in label_counter.most_common():
        print(f"  {count:>5}件  {label}")

    print("\n── 3. 同じASINで複数行あるもの（単品とセットの併存） ──")
    dupes = {a: v for a, v in by_asin.items() if len(v) > 1}
    print(f"  該当ASIN: {len(dupes)}件\n")
    for asin, items in list(dupes.items())[:15]:
        print(f"  {asin}")
        for item_id, qty, price, name in items:
            print(f"    {item_id:<20} 推定{qty}個  ¥{price:<8} {name}")

    print("\n── 4. セット系の語を含むのに数量を取れなかった行 ──")
    print("     （取りこぼしのパターン。ここが多ければ抽出条件の追加が必要）")
    for m in missed:
        print(f"    {m}")

    print("\n=== 調査完了 ===")


if __name__ == "__main__":
    main()
