import os
from rakuten_coupon_api import auth_headers, search_all

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]

TARGET_COUPON_NAMES = [
    "リポソーム型ビタミンC（3個）の写真付きレビュー投稿+200円OFF",
    "Dr.Plusリポソーム型ビタミンC（3個）のレビュー投稿で600円OFF",
    "リポソーム型ビタミンC（2個）の写真付きレビュー投稿+200円OFF",
    "Dr.Plusリポソーム型ビタミンC（2個）のレビュー投稿で300円OFF",
    "リポソーム型ビタミンC（1個）の写真付きレビュー投稿+100円OFF",
    "Dr.Plusリポソーム型ビタミンC（1個）のレビュー投稿で300円OFF",
    "アルロース（5個セット）の写真付きレビュー投稿で+200円OFF",
    "アルロース（5個セット）のレビュー投稿で1000円OFF",
    "アルロース（3個セット）の写真付きレビュー投稿で+200円OFF",
    "アルロース（3個セット）のレビュー投稿で500円OFF",
    "アルロース（1個）の写真付きレビュー投稿で+100円OFF",
    "アルロース（1個）のレビュー投稿で300円OFF",
]

headers = auth_headers(SERVICE_SECRET, LICENSE_KEY)
coupons = search_all(headers)
print(f"total: {len(coupons)}")

for name in TARGET_COUPON_NAMES:
    matches = [c for c in coupons if c.get("couponName") == name]
    if not matches:
        print(f"{name}: 見つかりません")
        continue
    latest = max(matches, key=lambda c: c.get("couponStartDate", ""))
    print(f"{name}: {latest.get('couponCode')}  {latest.get('couponStartDate')} ~ {latest.get('couponEndDate')}  ({len(matches)}件)")
