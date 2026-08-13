"""
緊急復旧スクリプト。

case_orders_price_adjust.py のバグにより、ケース155212の処理中に
本来別々の価格を計算すべき8件のYahoo商品すべてに、同じ誤った価格(¥16,186)を
書き込んでしまった。このスクリプトはその8件を元の価格に戻す。

バグの内容: 1ケース内で最初に成功した楽天行のCalc結果を、そのケースの
Yahoo対象すべてに使い回していた（商品ごとに別々に計算すべきだった）。

対象と復元先の価格は、実行ログに残っていた「現在 ¥xxx → 計算結果 ¥16,186」の
xxx（誤った書き込みが起きる直前の価格）をそのまま使う。
"""

import os

from case_orders_auto_close import get_yahoo_access_token, get_spreadsheet
from rakuten_price_adjust import yahoo_update_price

# (商品コード, 戻す価格)
REVERT_TARGETS = [
    ("111900001akc", 11330),
    ("111900001msy", 11330),
    ("bb-051999msy", 4460),
    ("bb-051999akc", 4460),
    ("my20230534msy", 10780),
    ("my20230534akc", 10780),
    ("bb-051999-2msy", 7982),
    ("bb-051999-2akc", 7982),
]


def main():
    os.environ["DRY_RUN"] = "false"  # rakuten_price_adjust.DRY_RUN が確実に本番動作するように
    import rakuten_price_adjust
    rakuten_price_adjust.DRY_RUN = False

    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)

    print("=== Yahoo価格 緊急復旧 開始 ===")
    all_ok = True

    for sku, original_price in REVERT_TARGETS:
        print(f"\n{sku} → ¥{original_price:,} に復元")
        results = yahoo_update_price(token, [sku], original_price)
        for store_name, message, ok in results:
            print(f"  {store_name}: {message}")
            if not ok:
                all_ok = False

    print("\n=== 復旧完了 ===" if all_ok else "\n=== 一部失敗。ログを確認してください ===")


if __name__ == "__main__":
    main()
