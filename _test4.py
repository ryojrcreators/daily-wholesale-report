"""（一時）指定した期間のキャンセルデータ2種類をCSVで取得し、Excelマクロと同じロジックで集計する。

取得するもの:
  A) 全体のキャンセル     … has_cs_request なし
  B) 店舗都合のキャンセル … has_cs_request=1

集計ロジックは「Cancel集計マクロ.xlsm」の AggregateSheet と同じ:
  件数      = 店舗ごとの order_number のユニーク数（明細行数ではない）
  Price合計 = Σ(qty × price) を小数点以下切り上げ

期間は環境変数 START_DATE / END_DATE で指定（未指定なら2026年7月）。
"""
import csv
import io
import math
import os
from collections import defaultdict
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright

DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"

START_DATE = os.environ.get("START_DATE", "2026-07-01")
END_DATE = os.environ.get("END_DATE", "2026-07-31")

# 楽天2店舗＋Yahoo2店舗のSKU店舗ID / 注文ステータス=キャンセル
BASE_QUERY = (
    f"start_date={START_DATE}"
    f"&end_date={END_DATE}"
    "&SoHeads%5Bsku_shop_id%5D%5B%5D=1"
    "&SoHeads%5Bsku_shop_id%5D%5B%5D=15"
    "&SoHeads%5Bso_status_id%5D%5B%5D=7"
    "&has_credit=0"
    "&SoHeads%5Bresend%5D=0"
)

TARGETS = [
    ("all", "全体のキャンセル", BASE_QUERY),
    ("shop", "店舗都合のキャンセル", BASE_QUERY + "&has_cs_request=1"),
]

# マクロと同じ並び順
PREFERRED = ["アメリカーナ", "Founder", "American Kitchen", "Meta Store"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def login(p):
    """Basic認証 → フォームログインを済ませたブラウザコンテキストを返す"""
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1800, "height": 900},
        device_scale_factor=2,
        user_agent=UA,
    )
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    return browser, context


def decode_csv(content):
    """このシステムのCSVはShift-JIS(CP932)。念のためUTF-8にもフォールバックする。"""
    for enc in ("cp932", "utf-8-sig"):
        try:
            return content.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return content.decode("cp932", errors="replace"), "cp932(一部置換)"


def download(context, query, label, save_as):
    url = f"https://{DOMAIN}/sales/download?{query}"
    cookies = {c["name"]: c["value"] for c in context.cookies()}
    res = requests.get(
        url,
        cookies=cookies,
        headers={"User-Agent": UA},
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
        timeout=300,
    )
    print(f"[{label}] status={res.status_code} bytes={len(res.content)}")
    if res.status_code != 200:
        raise SystemExit(f"[{label}] ダウンロードに失敗しました")

    text, enc = decode_csv(res.content)
    if "<html" in text[:500].lower():
        raise SystemExit(f"[{label}] CSVではなくHTMLが返っています")

    rows = list(csv.DictReader(io.StringIO(text)))
    print(f"[{label}] 文字コード={enc} 明細行数={len(rows)}")

    # UTF-8で保存し直す（Excelで開いても化けないようBOM付き）
    with open(save_as, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
        w.writeheader()
        w.writerows(rows)
    print(f"[{label}] 保存: {save_as}")
    return rows


def aggregate(rows):
    """マクロの AggregateSheet と同じ集計"""
    orders = defaultdict(set)
    totals = defaultdict(float)
    for r in rows:
        shop = (r.get("shop_name") or "").strip()
        if not shop:
            continue
        orders[shop].add(r.get("order_number", ""))
        try:
            qty = float((r.get("qty") or "0").replace(",", ""))
            price = float((r.get("price") or "0").replace(",", ""))
        except ValueError:
            qty, price = 0.0, 0.0
        totals[shop] += qty * price

    keys = [k for k in PREFERRED if k in orders] + \
           [k for k in sorted(orders) if k not in PREFERRED]
    return [(k, len(orders[k]), math.ceil(totals[k])) for k in keys]


def main():
    print(f"=== 対象期間: {START_DATE} 〜 {END_DATE} ===")
    results = {}
    with sync_playwright() as p:
        browser, context = login(p)
        print("ログイン完了")
        for key, label, query in TARGETS:
            rows = download(context, query, label, f"cancel_{key}.csv")
            results[key] = (label, aggregate(rows), len(rows))
        browser.close()

    print("\n=== 集計結果（マクロと同じロジック）===")
    for key, label, _ in TARGETS:
        lbl, agg, line_count = results[key]
        print(f"\n[{lbl}]  明細 {line_count}行")
        print(f"  {'店舗名':<20}{'件数':>6}{'Price合計':>14}")
        for shop, count, total in agg:
            print(f"  {shop:<20}{count:>6}{total:>14,}")
        print(f"  {'合計':<20}{sum(a[1] for a in agg):>6}{sum(a[2] for a in agg):>14,}")


if __name__ == "__main__":
    main()
