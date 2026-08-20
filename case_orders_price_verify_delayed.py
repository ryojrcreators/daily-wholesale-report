"""
case_orders_price_adjust.py が書き込んだ価格を、少し時間を置いてから
もう一度APIで読み直し、書き込んだ値のまま残っているかを確認する。

case_orders_price_adjust.py 自身の即時検証（Calcで再計算して突き合わせる）は
「書き込んだ値そのものが正しいか」を確かめるものだったが、これは別の観点として
「書き込みが時間が経っても本当に反映され続けているか」を確認する
（反映APIの遅延・失敗、後続の別処理による上書きなどを拾うため）。

case_orders_price_adjust.py が書き出した price_adjust_applied_changes.json
（GitHub Actionsのartifactとして次のジョブに渡される）を読み、実行時点の
実際の価格と突き合わせる。異常があれば Chatwork で Ryo Higuchi 宛てに報告する。
このスクリプト自体は価格を変更しない（読み取りのみ）。
"""

import os
import json

from case_orders_auto_close import (
    get_spreadsheet,
    get_yahoo_access_token,
    yahoo_get_item,
    rakuten_get_current_prices,
    SHOP_RAKUTEN,
    SHOP_YAHOO,
    YAHOO_STORES,
)
from case_orders_price_adjust import (
    PRICE_TOLERANCE_YEN,
    CW_MENTION_RYO,
    post_chatwork,
    APPLIED_CHANGES_FILE,
)


def check_rakuten(sku: str, expected_price: int) -> list:
    """
    出品が見つからない場合は異常扱いしない（意図的にClose・削除された可能性があり、
    書き込んだ価格が反映されているかとは別の話のため）。2026-08-20、ユーザー指示により変更。
    """
    anomalies = []
    actual = rakuten_get_current_prices(sku)
    if not actual:
        return anomalies
    for store_name, price in actual.items():
        if price is None:
            continue
        if abs(price - expected_price) > PRICE_TOLERANCE_YEN:
            anomalies.append(
                f"[楽天] {sku}（{store_name}）: 書き込んだはずの ¥{expected_price:,} ではなく "
                f"現在 ¥{price:,}"
            )
    return anomalies


def check_yahoo(yahoo_token: str, sku: str, expected_price: int) -> list:
    """出品が見つからない場合は異常扱いしない（check_rakutenと同じ方針）。"""
    anomalies = []
    for store in YAHOO_STORES:
        try:
            item = yahoo_get_item(yahoo_token, store, sku)
        except Exception as e:
            anomalies.append(f"[Yahoo] {sku}（{store['name']}）: 取得エラー {e}")
            continue
        if item is None:
            continue
        try:
            actual_price = int(str(item.get("Price", "")).replace(",", ""))
        except ValueError:
            anomalies.append(f"[Yahoo] {sku}（{store['name']}）: 価格を読み取れません")
            continue
        if abs(actual_price - expected_price) > PRICE_TOLERANCE_YEN:
            anomalies.append(
                f"[Yahoo] {sku}（{store['name']}）: 書き込んだはずの ¥{expected_price:,} ではなく "
                f"現在 ¥{actual_price:,}"
            )
        break
    return anomalies


def main():
    print("=== 価格調整の時間差検証 開始 ===")

    if not os.path.exists(APPLIED_CHANGES_FILE):
        print(f"{APPLIED_CHANGES_FILE} が見つかりません。前段のジョブで変更が無かったか、"
              "artifactの受け渡しに失敗しています。終了。")
        return

    with open(APPLIED_CHANGES_FILE, encoding="utf-8") as f:
        changes = json.load(f)
    print(f"確認対象: {len(changes)}件")

    spreadsheet = get_spreadsheet()
    yahoo_token = get_yahoo_access_token(spreadsheet)

    anomalies = []
    for c in changes:
        print(f"  [{c['mall']}] {c['sku']} → ¥{c['price']:,} のはず")
        if c["mall"] == SHOP_RAKUTEN:
            anomalies += check_rakuten(c["sku"], c["price"])
        else:
            anomalies += check_yahoo(yahoo_token, c["sku"], c["price"])

    if anomalies:
        print(f"\n⚠️ 異常を検出しました（{len(anomalies)}件）:")
        for a in anomalies:
            print(f"  {a}")
        body = (
            f"{CW_MENTION_RYO}\n"
            f"[info][title]価格調整の時間差検証で異常を検出（{len(anomalies)}件）[/title]"
            + "\n".join(anomalies)
            + "\n\n書き込み直後の検証は正常でしたが、時間を置いて確認したところ値が"
              "ズレていました。反映の遅延・失敗や、他の処理による上書きが考えられます。"
              "内容の確認をお願いします。[/info]"
        )
        post_chatwork(body)
    else:
        print("\n異常は見つかりませんでした。")

    print("=== 時間差検証 完了 ===")


if __name__ == "__main__":
    main()
