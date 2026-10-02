"""一時調査用（読み取りのみ）: See's Candiesの商品について、楽天2店舗・Yahoo2店舗の現在の公開状態を一覧にする。"""

import json
import os
import re
import time

import gspread
import requests
from google.oauth2.service_account import Credentials

from case_orders_auto_close import (
    RMS_BASE, get_rakuten_stores, get_yahoo_access_token, get_yahoo_stores,
    rakuten_auth_headers, yahoo_get_item,
)

SEES = re.compile(r"See.?s\s*Candies|シーズ\s*キャンディ|シーズキャンディ", re.IGNORECASE)


def sheet_rows(ss, name, code_col):
    rows = ss.worksheet(name).get_all_values()
    h = rows[0]
    i_shop, i_code, i_name = h.index("店舗名"), h.index(code_col), h.index("商品名")
    return [(r[i_shop], r[i_code], r[i_name]) for r in rows[1:] if len(r) > i_name and SEES.search(r[i_name])]


def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    ss = gspread.authorize(creds).open_by_key(os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"])

    # ── 楽天 ──
    r_hits = sheet_rows(ss, "楽天_出品データ", "商品管理番号")
    codes = sorted({c for _, c, _ in r_hits})
    names = {c: n for _, c, n in r_hits}
    print(f"[楽天] スナップショット上のSee's: {len(r_hits)}行 / ユニーク商品 {len(codes)}件")
    stores = get_rakuten_stores()
    print("店舗名:", [s["name"] for s in stores])
    print("\n商品管理番号\t" + "\t".join(s["name"] for s in stores) + "\t商品名")
    for c in codes:
        st = []
        for s in stores:
            res = requests.get(f"{RMS_BASE}/{c}", headers=rakuten_auth_headers(s), timeout=30)
            time.sleep(1.0)
            if res.status_code == 404:
                st.append("なし")
            elif res.status_code >= 400:
                st.append(f"ERR{res.status_code}")
            else:
                st.append("非公開" if res.json().get("hideItem") is True else "公開中")
        print(f"{c}\t" + "\t".join(st) + f"\t{names[c][:60]}")

    # ── Yahoo ──
    y_hits = sheet_rows(ss, "Yahoo_出品データ", "商品コード")
    print(f"\n[Yahoo] スナップショット上のSee's: {len(y_hits)}行")
    token = get_yahoo_access_token(ss)
    ystores = {s["name"]: s for s in get_yahoo_stores()}
    print("店舗名:", list(ystores))
    print("\n店舗\t商品コード\t現在の在庫\t商品名")
    t0 = time.time()
    for shop, code, name in sorted(y_hits, key=lambda x: (x[1], x[0])):
        if time.time() - t0 > 300:
            token, t0 = get_yahoo_access_token(ss), time.time()
        store = ystores.get(shop)
        if store is None:
            print(f"{shop}\t{code}\t(店舗名不一致)\t{name[:50]}")
            continue
        item = yahoo_get_item(token, store, code)
        time.sleep(1.0)
        print(f"{shop}\t{code}\t{(item or {}).get('Quantity', 'なし')}\t{name[:50]}")


if __name__ == "__main__":
    main()
