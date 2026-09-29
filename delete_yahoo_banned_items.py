"""
一時実行用: 9/30ガイドライン対応でYahoo側は在庫0（setStock）までしか行っていなかったが、
Yahooには楽天のhideItem/Wowmaのsaleoモ status相当の「安全な非公開フラグ」が無いため、
完全削除（deleteItem）に切り替える。

対象は、これまでのCloseで在庫0にした全259件（米・米加工品・ペットフード・おやつ・
ヘアケア・牛エキス系・Greenies・鳥の餌 一式）。
DRY_RUN=true（既定）では実際には変更しない。
"""

import time

from case_orders_auto_close import DRY_RUN, get_spreadsheet, get_yahoo_access_token, get_yahoo_stores
from guideline_ban_delete import yahoo_delete_item

with open("yahoo_delete_codes.txt", encoding="utf-8") as f:
    ITEM_CODES = [line.strip() for line in f if line.strip()]

# 前回の実行で、259件×2店舗のループを1つのアクセストークンのまま回し続けた結果、
# 処理の後半でトークンが失効して大量に失敗した（2026-09-29）。
# 5分ごとにトークンを取り直すことで、長時間ループでも失効しないようにする。
TOKEN_REFRESH_INTERVAL_SEC = 300


def main():
    print(f"=== Yahoo 完全削除開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_CODES)}件 ===")
    spreadsheet = get_spreadsheet()
    stores = get_yahoo_stores()

    state = {"token": get_yahoo_access_token(spreadsheet), "fetched_at": time.time()}

    def get_fresh_token():
        if time.time() - state["fetched_at"] > TOKEN_REFRESH_INTERVAL_SEC:
            print("  （トークン更新）")
            state["token"] = get_yahoo_access_token(spreadsheet)
            state["fetched_at"] = time.time()
        return state["token"]

    ok_count = 0
    ng_count = 0
    for code in ITEM_CODES:
        for store in stores:
            token = get_fresh_token()
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
