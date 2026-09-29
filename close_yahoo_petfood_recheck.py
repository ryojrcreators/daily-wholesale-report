"""
一時実行用: Yahooで見落としが判明したGreenies各種おやつ・鳥の餌（205件）をCloseする。
DRY_RUN=true（既定）では実際には変更しない。
"""

from case_orders_auto_close import DRY_RUN, get_spreadsheet, get_yahoo_access_token, yahoo_close

with open("yahoo_petfood_codes.txt", encoding="utf-8") as f:
    ITEM_CODES = [line.strip() for line in f if line.strip()]


def main():
    print(f"=== Yahoo ペットフード見落とし対応Close開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_CODES)}件 ===")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    results = yahoo_close(token, ITEM_CODES)
    for shop, msg, ok in results:
        mark = "OK" if ok else "NG"
        print(f"  [{mark}] {shop}: {msg}")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
