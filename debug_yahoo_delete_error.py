"""一時調査用: Yahoo deleteItemが400で失敗した理由をフルで確認する。"""

import requests

from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, YAHOO_BASE

spreadsheet = get_spreadsheet()
token = get_yahoo_access_token(spreadsheet)

for store in get_yahoo_stores():
    res = requests.post(
        f"{YAHOO_BASE}/deleteItem",
        headers={"Authorization": f"Bearer {token}"},
        data={"seller_id": store["seller_id"], "item_code": "10000247akc"},
        timeout=30,
    )
    print(f"=== {store['name']} status={res.status_code} ===")
    print(res.text)
    print()
