"""
一時実行用: Greenies・鳥の餌系で楽天側に見落としがないか、items.searchスナップショットで
見つかった125件の現在のhideItem状態を確認し、非公開になっていないものだけClose(hideItem=true)する。

スナップショット自体には既に非公開の商品も混ざっている（items.searchはhideItem=trueの商品も
返すため）ので、rakuten_hide()を使うと「すでに倉庫」はそのまま素通りし、まだ公開中のものだけ
実際に変更される。
"""

from case_orders_auto_close import DRY_RUN, rakuten_hide

with open("rakuten_petfood_recheck_items.txt", encoding="utf-8") as f:
    ITEM_NUMBERS = [line.strip() for line in f if line.strip()]


def main():
    print(f"=== 楽天 ペットフード見落とし対応Close開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_NUMBERS)}件 ===")
    newly_closed = []
    for item in ITEM_NUMBERS:
        results = rakuten_hide(item)
        for shop, msg, ok in results:
            mark = "OK" if ok else "NG"
            print(f"  [{mark}] {item} / {shop}: {msg}")
            if ok and "倉庫に入れ" in msg:
                newly_closed.append((item, shop, msg))
    print(f"\n=== 完了。新たに非公開にした/対象になった: {len(newly_closed)}件 ===")
    for item, shop, msg in newly_closed:
        print(f"  {item} / {shop}: {msg}")


if __name__ == "__main__":
    main()
