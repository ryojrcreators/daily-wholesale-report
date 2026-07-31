"""1回限りの再送信用。今回のFounder5,0の付く日クーポン発行結果を、新フォーマットで再送する。"""
import os
import requests

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "60101971"
CW_MENTION = "[To:2158846]Yoko Matsusakaさん"
CW_TITLE = "楽天クーポン更新（Founder 5,0の付く日）"
CW_INTRO = "下記の通り更新しました。"

RESULTS = [
    {"day": 5, "status": "already"},
    {"day": 10, "code": "NJEE-ALIN-YWWN-B2A8", "url": "https://coupon.rakuten.co.jp/getCoupon?getkey=QUxJTi1OSkVFLVlXV04tQjJBOA--&rt="},
    {"day": 15, "code": "LHDT-CDCV-UGAI-BOWW", "url": "https://coupon.rakuten.co.jp/getCoupon?getkey=Q0RDVi1MSERULVVHQUktQk9XVw--&rt="},
    {"day": 20, "code": "AZ5I-RYO1-JGQ9-VWFY", "url": "https://coupon.rakuten.co.jp/getCoupon?getkey=UllPMS1BWjVJLUpHUTktVldGWQ--&rt="},
    {"day": 25, "code": "FLGJ-MRTR-5E5I-R4JJ", "url": "https://coupon.rakuten.co.jp/getCoupon?getkey=TVJUUi1GTEdKLTVFNUktUjRKSg--&rt="},
    {"day": 30, "code": "LQRX-0EIC-H8Y4-YIAE", "url": "https://coupon.rakuten.co.jp/getCoupon?getkey=MEVJQy1MUVJYLUg4WTQtWUlBRQ--&rt="},
]

lines = [CW_MENTION, f"[info][title]{CW_TITLE}[/title]", CW_INTRO, ""]
for r in RESULTS:
    if r.get("status") == "already":
        lines.append(f"■ {r['day']}日分  既に更新済みでした")
        lines.append("")
    else:
        lines.append(f"■ {r['day']}日分")
        lines.append(f"クーポンコード: {r['code']}")
        lines.append(f"取得URL: {r['url']}")
        lines.append("")
lines.append("[/info]")
body = "\n".join(lines)

res = requests.post(
    f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/tasks",
    headers={"X-ChatWorkToken": CW_TOKEN},
    data={"body": body, "to_ids": "2158846"},
    timeout=30,
)
print(f"status={res.status_code}")
print(res.text[:300])
