"""（一時）分析用に対象月のキャンセルCSV2種類を取得してアーティファクトとして保存する。

cancel_report.py と同じ取得ロジック（ログイン→/sales/downloadを直接叩く）を使う。
シートには一切触れない・読み取りのみ。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from calendar import monthrange
import cancel_report as cr
from playwright.sync_api import sync_playwright

year, month = cr.target_month()
start = f"{year}-{month:02d}-01"
end = f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"
print(f"対象月: {year}/{month}（{start}〜{end}）")

with sync_playwright() as p:
    browser, context = cr.login(p)
    print("ログイン完了")
    all_rows = cr.download_csv(context, cr.build_query(start, end, False), "全体のキャンセル")
    shop_rows = cr.download_csv(context, cr.build_query(start, end, True), "店舗都合のキャンセル")
    browser.close()

import csv
for name, rows in (("cancel_all.csv", all_rows), ("cancel_shop.csv", shop_rows)):
    with open(name, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"保存: {name}（{len(rows)}行）")
