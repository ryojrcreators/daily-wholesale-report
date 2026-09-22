"""
一時実行用: 2026-09-30から楽天が新たに出品禁止にするカテゴリ
（外国産米・米加工品／ペットフード・おやつ／ペット用ヘアケア／牛エキス系）に該当すると
確認できた商品を、Yahoo!ショッピング側（American Kitchen・Meta Store）でも
在庫0（実質クローズ）にする。

対象コードは debug_yahoo_wowma_banned_scan.py によるスキャン結果を人が確認して
確定させたもの（楽天と同一商品17種類 + Yahooのみで見つかった同カテゴリ新規3種類）。

DRY_RUN=true（既定）では実際には変更しない。
"""

from case_orders_auto_close import DRY_RUN, get_spreadsheet, get_yahoo_access_token, yahoo_close

ITEM_CODES = [
    "my20231547akc", "my20231547msy",
    "my20231547-2akc", "my20231547-2msy",
    "my20231547-4akc", "my20231547-4msy",
    "my20231547-8akc", "my20231547-8msy",
    "now1960-akc", "now1960msy",
    "now1960-2akc", "now1960-2msy",
    "now1960-3akc", "now1960-3msy",
    "nissin4-akc", "nissin4-msy",
    "nissin8-akc", "nissin8-msy",
    "je0000982akc", "je0000982msy",
    "14007001-ak", "14007001msy",
    "aj0001016akc", "aj0001016msy",
    "my20230867akc", "my20230867msy",
    "my20230867-3akc", "my20230867-3msy",
    "ma01082402akc", "ma01082402msy",
    "ma66593-akc", "ma66593-msy",
    "Mins-001akc", "Mins-001msy",
    "23007669akc", "23007669msy",
    "my031904270akc", "my031904270msy",
    "my20230438akc", "my20230438msy",
    "ry23014187akc", "ry23014187msy",
    "ry23014184akc", "ry23014184msy",
    "ry23014176akc", "ry23014176msy",
    "160515-055msy",
    # Yahooのみで見つかった新規（同カテゴリ、含める指示済み）
    "jm0000358akc", "jm0000358msy",
    "aj0000008akc", "aj0000008msy",
    "my20231997akc", "my20231997msy",
]


def main():
    print(f"=== Yahoo 出品禁止カテゴリ対応Close開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_CODES)}件 ===")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    results = yahoo_close(token, ITEM_CODES)
    for shop, msg, ok in results:
        mark = "OK" if ok else "NG"
        print(f"  [{mark}] {shop}: {msg}")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
