"""ケース161883で明示許可されたYahooの2商品だけを9,818円へ変更する臨時実行。"""
import os
import time

from case_orders_auto_close import (
    get_spreadsheet, get_yahoo_access_token, get_yahoo_stores,
    yahoo_get_item, YAHOO_BASE, append_log, JST,
)
from datetime import datetime
import rakuten_price_adjust as api


TARGET_PRICE = 9818
TARGETS = {"drplus": "my20230307msy", "americankitchen": "my20230307akc"}


def main():
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/ops/case-161883-yahoo-price"
            or os.environ.get("DRY_RUN", "true").lower() != "false"):
        raise RuntimeError("明示的な臨時本番実行以外では変更しません")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    stores = {store["seller_id"]: store for store in get_yahoo_stores()}
    if not set(TARGETS).issubset(stores):
        raise RuntimeError("対象2店舗の認証設定を確認できません")
    current_items = {}
    # 全対象を確認してから変更する。異なる価格やセール設定があれば停止する。
    for seller_id, code in TARGETS.items():
        item = yahoo_get_item(token, stores[seller_id], code)
        time.sleep(1)
        if item is None or item.get("Price") not in ("15690", str(TARGET_PRICE)):
            raise RuntimeError(f"{code}: 事前確認時の価格が想定と異なるため停止します")
        if item.get("SalePrice") and item["SalePrice"] not in ("0", item["Price"]):
            raise RuntimeError(f"{code}: セール設定があるため停止します")
        current_items[seller_id] = item

    for seller_id, code in TARGETS.items():
        store = stores[seller_id]
        before = current_items[seller_id]["Price"]
        if before != str(TARGET_PRICE):
            res = api.requests.post(
                f"{YAHOO_BASE}/updateItems",
                headers={"Authorization": f"Bearer {token}"},
                data={"seller_id": seller_id,
                      "item1": f"item_code={code}&price={TARGET_PRICE}&sale_price="},
                timeout=30,
            )
            if res.status_code >= 400 or "<Status>OK</Status>" not in res.text:
                raise RuntimeError(f"{code}: 価格更新失敗 HTTP {res.status_code}")
        time.sleep(1)
        if not api.yahoo_submit_item(token, store, code):
            raise RuntimeError(f"{code}: 反映API失敗")
        time.sleep(3)
        actual = yahoo_get_item(token, store, code)
        if actual is None or actual.get("Price") != str(TARGET_PRICE):
            raise RuntimeError(f"{code}: 更新後の価格検証失敗")
        print(f"CASE 161883: {code}: {before} -> {TARGET_PRICE}: 更新・反映API・API再取得 成功")
        append_log(spreadsheet, [[datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
                                 "161883", "Change Price", "Yahoo", store["name"], code,
                                 f"ユーザー指定による値下げ: ¥{before}→¥{TARGET_PRICE}（反映・再取得確認済み）"]])
    print("CASE 161883: Yahoo 2件の価格更新完了。楽天・ケース・その他商品は変更していません。")


if __name__ == "__main__":
    main()
