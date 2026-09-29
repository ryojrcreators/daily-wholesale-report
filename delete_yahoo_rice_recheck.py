"""
一時実行用: 米カテゴリの広いキーワードでYahooを再スキャンした結果見つかった45件
（NutriBiotic/Source Naturals/NOWのライスプロテイン、Trader Joe'sパスタ等。
既に削除済みのものも含むがdeleteItemは冪等なのでまとめて実行する）を削除する。
"""

import time

from case_orders_auto_close import DRY_RUN, get_spreadsheet, get_yahoo_access_token, get_yahoo_stores
from guideline_ban_delete import yahoo_delete_item

with open("yahoo_rice_recheck_codes.txt", encoding="utf-8") as f:
    ITEM_CODES = [line.strip() for line in f if line.strip()]


def main():
    print(f"=== Yahoo 米カテゴリ見落とし追加削除開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_CODES)}件 ===")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    stores = get_yahoo_stores()

    ok_count = 0
    ng_count = 0
    for code in ITEM_CODES:
        for store in stores:
            ok, note = yahoo_delete_item(token, store, code)
            if note == "すでに存在しません":
                continue
            mark = "OK" if ok else "NG"
            print(f"  [{mark}] {code} / {store['name']}: {note}")
            if ok:
                ok_count += 1
            else:
                ng_count += 1
            time.sleep(1.0)
    print(f"=== 完了: OK {ok_count}件 / NG {ng_count}件 ===")


if __name__ == "__main__":
    main()
