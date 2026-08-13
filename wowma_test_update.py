"""
Wowma 価格更新API（updateItemPrice）の実機確認用の使い捨てスクリプト。
テスト対象は ry23010062（現在価格 5000円）に、同じ5000円を書き込む
（実質変化なしの安全なテスト）。

case_orders_wowma.py の wowma_update_price / wowma_get_item をそのまま使う。
"""

from case_orders_wowma import wowma_get_item, wowma_update_price


def main():
    item_code = "ry23010062"
    price = 5000

    before = wowma_get_item(item_code)
    print(f"更新前 itemPrice: {before.get('itemPrice') if before else None}")

    ok, message = wowma_update_price(item_code, price, dry_run=False)
    print(f"更新結果: ok={ok} message={message}")

    after = wowma_get_item(item_code)
    print(f"更新後 itemPrice: {after.get('itemPrice') if after else None}")


if __name__ == "__main__":
    main()
