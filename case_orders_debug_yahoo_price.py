"""
使い捨てデバッグスクリプト。指定したYahoo商品コードについて、getItemの生レスポンスを
2店舗ともそのまま出力する（読み取りのみ、価格は変更しない）。

ケース155234で、内部システムのRelated Skus画面に表示されたSales Price（17080円/13830円）と、
case_orders_price_adjust.py がYahoo APIから取得した現在価格（29490円）が食い違っていたため、
どちらが実際のYahoo APIレスポンスと一致するかを確認する。
"""

from case_orders_auto_close import (
    get_spreadsheet,
    get_yahoo_access_token,
    yahoo_get_item,
    YAHOO_STORES,
)

CODES = ["23005569msy", "23005569akc"]


def main():
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)

    for code in CODES:
        print(f"\n==== {code} ====")
        for store in YAHOO_STORES:
            try:
                item = yahoo_get_item(token, store, code)
            except Exception as e:
                print(f"  [{store['name']}] エラー: {e}")
                continue
            if item is None:
                print(f"  [{store['name']}] 出品なし")
                continue
            print(f"  [{store['name']}] Price={item.get('Price')} Quantity={item.get('Quantity')} "
                  f"ItemCode={item.get('ItemCode')} Name={item.get('Name')}")


if __name__ == "__main__":
    main()
