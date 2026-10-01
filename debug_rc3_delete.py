"""一時調査用: rc0000003akc/msy がdeleteItemで消えない理由をレスポンス全文で確認し、消せれば消す。"""

import requests

from case_orders_auto_close import (
    get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, YAHOO_BASE, yahoo_get_item,
)

spreadsheet = get_spreadsheet()
token = get_yahoo_access_token(spreadsheet)

for code in ["rc0000003akc", "rc0000003msy"]:
    for store in get_yahoo_stores():
        item = yahoo_get_item(token, store, code)
        print(f"--- getItem {code} @ {store['name']}: {'存在' if item else '存在しない'}")
        if not item:
            continue
        print("   Quantity:", item.get("Quantity"), "/ Name:", (item.get("Name") or "")[:40])
        res = requests.post(
            f"{YAHOO_BASE}/deleteItem",
            headers={"Authorization": f"Bearer {token}"},
            data={"seller_id": store["seller_id"], "item_code": code},
            timeout=30,
        )
        print(f"   deleteItem status={res.status_code}")
        print("   ", res.text[:600].replace("\n", " "))
