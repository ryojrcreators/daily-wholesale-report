import os
from rakuten_coupon_api import auth_headers, search_all

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_2"]
SHOP_NAME = os.environ["RAKUTEN_SHOP_NAME_2"]
TARGET_COUPON_NAME = f"{SHOP_NAME}で使える5,0の付く日クーポン"

headers = auth_headers(SERVICE_SECRET, LICENSE_KEY)
coupons = search_all(headers)
print(f"total: {len(coupons)}")

groups = {}
for c in coupons:
    if c.get("couponName") != TARGET_COUPON_NAME:
        continue
    start = c.get("couponStartDate", "")
    day = None
    try:
        day = int(start[8:10])
    except Exception:
        pass
    groups.setdefault(day, []).append(c)

for day in sorted(groups, key=lambda x: (x is None, x)):
    items = sorted(groups[day], key=lambda c: c.get("couponStartDate", ""))
    print(f"\nday={day}: {len(items)}件")
    for c in items[-3:]:
        print(f"  {c.get('couponCode')}  {c.get('couponStartDate')} ~ {c.get('couponEndDate')}")
