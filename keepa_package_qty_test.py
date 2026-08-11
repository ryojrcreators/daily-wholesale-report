"""
Keepaが「1購入あたり何個入りか」を判別できる項目を返すか確認する（読み取りのみ）。

背景:
  rakuten_price_check.py はセット商品でも1個ぶんの価格で原価計算しており、
  単品と3個セットの適正価格が同じになってしまう。
  ただし商品名の「N個セット」で一律に掛け算すると、Amazon側が最初からN個入りの
  箱を売っている商品（ティックタック12個セット、Skittles 36個セットなど）で
  原価が数十倍になり、今より悪化する。

  そこで「楽天が売る個数 ÷ ASIN 1購入あたりの入数 = 必要な購入回数」で計算したい。
  Keepaが入数（packageQuantity / numberOfItems）を返すかをここで確認する。

対象は商品管理番号で指定し、シートからASINを引く。
"""

import os
import json

import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"
KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]

COL_ITEM_ID = 0
COL_NAME = 1
COL_ASIN = 2
COL_PRICE_JPY = 3

# 判別したい代表例。単品とセットが併存するもの、Amazon側が箱売りのものを混ぜてある。
TARGET_ITEM_IDS = [
    "1000-1192",      # スピードスティック 単品
    "1000-1192-3",    # 同 3個セット（ASINは同じ → 3回購入のはず）
    "1000-1192-6",    # 同 6個セット
    "10001547",       # 【12個セット】ティックタック（Amazon側が12個入りの想定）
    "10000405",       # 【36個セット】Skittles（同上）
    "0210chuc",       # チュッパチャプス 40個
    "my095801171",    # 同ASINの60個入り
    "10000368",       # 【2本セット】オールドスパイス
    "10000367",       # 同 単品
]


def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def main():
    print("=== Keepa 入数項目の確認 開始 ===\n")

    rows = get_sheet().get_all_values()[1:]
    wanted = {i.lower() for i in TARGET_ITEM_IDS}
    targets = []
    for row in rows:
        if len(row) <= COL_ASIN:
            continue
        item_id = row[COL_ITEM_ID].strip()
        if item_id.lower() in wanted and row[COL_ASIN].strip():
            targets.append({
                "item_id": item_id,
                "name": row[COL_NAME],
                "asin": row[COL_ASIN].strip(),
                "price": row[COL_PRICE_JPY] if len(row) > COL_PRICE_JPY else "",
            })

    print(f"シートから見つかった対象: {len(targets)}件 / 指定{len(TARGET_ITEM_IDS)}件")
    missing = wanted - {t["item_id"].lower() for t in targets}
    if missing:
        print(f"見つからなかった商品管理番号: {sorted(missing)}")
    if not targets:
        return

    asins = sorted({t["asin"] for t in targets})
    print(f"問い合わせるASIN: {len(asins)}件\n")

    url = (
        f"https://api.keepa.com/product?key={KEEPA_API_KEY}"
        f"&domain=1&asin={','.join(asins)}&stats=1"
    )
    res = requests.get(url, timeout=60)
    res.raise_for_status()
    products = {p["asin"]: p for p in res.json().get("products", [])}
    print(f"Keepaから取得: {len(products)}件\n")

    for t in targets:
        p = products.get(t["asin"])
        print(f"■ {t['item_id']}  ¥{t['price']}")
        print(f"   楽天商品名: {t['name'][:60]}")
        if not p:
            print("   → Keepaにデータなし\n")
            continue

        print(f"   Amazon商品名: {(p.get('title') or '')[:70]}")
        # 入数に関係しそうな項目をまとめて出す（どれが使えるかを見極めるため）
        for key in ("packageQuantity", "numberOfItems", "unitCount", "itemForm",
                    "size", "variationCount", "packageWeight"):
            if key in p:
                print(f"   {key}: {p.get(key)}")

        stats = p.get("stats", {}) or {}
        current = stats.get("current", []) or []
        amazon = current[0] if len(current) > 0 else None
        new = current[1] if len(current) > 1 else None
        print(f"   Amazon本体価格: {amazon} / 新品最安: {new}（セント、-1=なし）")
        print()

    print("=== 確認完了 ===")


if __name__ == "__main__":
    main()
