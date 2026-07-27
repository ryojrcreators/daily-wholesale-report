"""
楽天の商品管理番号を基準に、Yahooの商品コードにどんな接尾辞が使われているかを調べる（読み取りのみ）。

背景:
  社内システムの Related Skus には売れたことのあるSKUしか登録されておらず、
  Yahoo側のコード（例: 13000504-ak）が載っていないことがある。
  そのため「楽天コード + 接尾辞」でYahooコードを推測する必要があるが、
  接尾辞のパターンを推測で決めると取りこぼす。実データから網羅的に洗い出す。

出力:
  1. 接尾辞ごとの件数ランキング
  2. 前方一致の危険度（ある楽天コードが別の楽天コードの先頭と一致してしまう件数）
  3. サンプル表示

このスクリプトはスプレッドシートを読むだけで、何も書き換えない。
"""

import os
import json
import re
from collections import Counter, defaultdict

import gspread
from google.oauth2.service_account import Credentials

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

RAKUTEN_SHEET = "楽天_出品データ"
YAHOO_SHEET = "Yahoo_出品データ"

# 各シートの列（0始まり）。どちらも A=店舗名 B=商品コード C=商品名
COL_SHOP = 0
COL_CODE = 1
COL_NAME = 2


def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def read_codes(spreadsheet, sheet_name: str):
    """[(商品コード, 店舗名, 商品名)] を返す"""
    ws = spreadsheet.worksheet(sheet_name)
    rows = ws.get_all_values()[1:]  # ヘッダー除く
    out = []
    for row in rows:
        if len(row) <= COL_CODE:
            continue
        code = row[COL_CODE].strip()
        if not code:
            continue
        out.append((code, row[COL_SHOP].strip(), row[COL_NAME].strip() if len(row) > COL_NAME else ""))
    return out


def main():
    print("=== SKU接尾辞 調査 開始 ===")
    spreadsheet = get_spreadsheet()

    rakuten = read_codes(spreadsheet, RAKUTEN_SHEET)
    yahoo = read_codes(spreadsheet, YAHOO_SHEET)
    print(f"楽天: {len(rakuten)}件 / Yahoo: {len(yahoo)}件\n")

    rakuten_codes = {code for code, _, _ in rakuten}

    # ── 1. 楽天コードで前方一致するYahooコードを探し、接尾辞を集計 ──
    # 長い楽天コードから順に照合し、最も長く一致したものを採用する
    # （短いコードが別商品の先頭にたまたま一致するのを避けるため）
    by_length = sorted(rakuten_codes, key=len, reverse=True)

    suffix_counter = Counter()
    suffix_examples = defaultdict(list)
    matched = 0
    unmatched_samples = []

    # 高速化のため、先頭数文字でインデックスを作る
    prefix_index = defaultdict(list)
    for code in by_length:
        prefix_index[code[:4].lower()].append(code)

    for ycode, yshop, yname in yahoo:
        key = ycode[:4].lower()
        best = None
        for rcode in prefix_index.get(key, []):
            if ycode.lower().startswith(rcode.lower()):
                if best is None or len(rcode) > len(best):
                    best = rcode
        if best is None:
            if len(unmatched_samples) < 20:
                unmatched_samples.append((ycode, yshop, yname[:40]))
            continue

        matched += 1
        suffix = ycode[len(best):]
        suffix_counter[suffix] += 1
        if len(suffix_examples[suffix]) < 3:
            suffix_examples[suffix].append(f"{best} → {ycode}（{yshop}）")

    print("── 1. Yahooコードの接尾辞ランキング ──")
    print(f"楽天コードで前方一致したYahoo商品: {matched}件 / {len(yahoo)}件\n")
    for suffix, count in suffix_counter.most_common(40):
        label = "（接尾辞なし＝完全一致）" if suffix == "" else f"「{suffix}」"
        print(f"  {count:>6}件  {label}")
        for ex in suffix_examples[suffix]:
            print(f"           例: {ex}")

    others = len(suffix_counter) - 40
    if others > 0:
        print(f"\n  ...ほか{others}種類の接尾辞あり（少数）")

    # ── 2. 前方一致の危険度 ──
    # 楽天コードAが、別の楽天コードBの先頭と一致してしまう＝誤マッチの温床
    print("\n── 2. 前方一致の危険度（楽天コード同士） ──")
    risky = []
    for code in by_length:
        for other in prefix_index.get(code[:4].lower(), []):
            if other != code and other.lower().startswith(code.lower()):
                risky.append((code, other))
                break
    print(f"  他の楽天コードの先頭になっているコード: {len(risky)}件 / {len(rakuten_codes)}件")
    for a, b in risky[:15]:
        print(f"    {a} は {b} の先頭と一致")

    # ── 3. どの楽天コードにも一致しなかったYahoo商品のサンプル ──
    print("\n── 3. 楽天コードに紐づかなかったYahoo商品のサンプル ──")
    for ycode, yshop, yname in unmatched_samples:
        print(f"    {ycode}（{yshop}）{yname}")

    # ── 4. 店舗ごとの接尾辞の傾向 ──
    print("\n── 4. 店舗ごとの接尾辞の傾向 ──")
    by_shop = defaultdict(Counter)
    for ycode, yshop, _ in yahoo:
        key = ycode[:4].lower()
        best = None
        for rcode in prefix_index.get(key, []):
            if ycode.lower().startswith(rcode.lower()):
                if best is None or len(rcode) > len(best):
                    best = rcode
        if best is not None:
            by_shop[yshop][ycode[len(best):]] += 1
    for shop, counter in by_shop.items():
        print(f"  [{shop}]")
        for suffix, count in counter.most_common(10):
            label = "（なし）" if suffix == "" else f"「{suffix}」"
            print(f"    {count:>6}件  {label}")

    print("\n=== 調査完了 ===")


if __name__ == "__main__":
    main()
