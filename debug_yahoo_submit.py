"""一時調査用: 11241011akc の現在状態と submitItem のレスポンス全文を確認（このあと反映を再依頼する）。"""
import requests
from case_orders_auto_close import YAHOO_BASE, get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, yahoo_get_item

ss = get_spreadsheet()
token = get_yahoo_access_token(ss)
store = get_yahoo_stores()[0]
code = "11241011akc"
it = yahoo_get_item(token, store, code)
print("現在:", {k: it.get(k) for k in ["Display", "EditingFlag", "UpdateTime", "Quantity", "BrandCode", "Jan", "LeadTimeInStock", "PointImmediate"]})
res = requests.post(f"{YAHOO_BASE}/submitItem", headers={"Authorization": f"Bearer {token}"},
                    data={"seller_id": store["seller_id"], "item_code": code}, timeout=60)
print("submitItem status:", res.status_code)
print(res.text[:1500])
