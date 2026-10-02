"""一時調査用（読み取りのみ）: 指定コードのDisplay/EditingFlagを集計する。"""
import time
from collections import Counter
from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, yahoo_get_item

codes = [l.strip() for l in open("sees_yahoo_check_codes.txt", encoding="utf-8") if l.strip()]
ss = get_spreadsheet()
token, t0 = get_yahoo_access_token(ss), time.time()
stores = get_yahoo_stores()
cnt, rows = Counter(), []
for c in codes:
    if time.time() - t0 > 300:
        token, t0 = get_yahoo_access_token(ss), time.time()
    for st in stores:
        it = yahoo_get_item(token, st, c)
        time.sleep(1.0)
        if it is None:
            continue
        key = (it.get("Display"), it.get("EditingFlag"))
        cnt[key] += 1
        rows.append((c, it.get("Display"), it.get("EditingFlag"), it.get("UpdateTime")))
print("集計 (Display, EditingFlag):", dict(cnt))
for r in rows:
    if (r[1], r[2]) != ("1", "0"):
        print("要確認", r)
