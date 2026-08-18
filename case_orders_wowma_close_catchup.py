"""
Wowma在庫の「後追い」処理（Closeケース版）。

case_orders_price_adjust.py 用に作った case_orders_wowma_catchup.py と同じ理由・同じ設計。
case_orders_auto_close.py は楽天・YahooをGitHub Actionsで毎時自動実行しているが、
Wowmaは固定IPからしか呼べないためこのPC上での手動実行でしか処理できない
（WOWMA_ENABLED、詳細は case_orders_auto_close.py の docstring 参照）。

GitHub Actionsの自動実行が先にケースを処理してしまうと、
  - Reply「Rakuten/Yahoo Closed」が投稿され
  - Case Groupsが Rakuten/Yahoo のみだった場合は Status が In-Progress になる
  - 他のグループも付いていた場合は、Rakuten/Yahooタグだけが外れ、Status は New のまま残る
ため、通常の一覧（case_group_id[0]=4 で絞った一覧）からはそのケースが消えてしまい、
Wowma分だけ取りこぼされる。

このスクリプトは Case Group での絞り込みをせず、New / In-Progress の
Close (Temporary) / Close (Permanent) ケースをすべて対象に、まだWowmaの記録
（自動Close_ログにmall=Wowmaの行、DRY RUNの記録は除く）が無いケースだけを拾って、
Wowma在庫だけを追いで0にする。ケースのステータス・Replyは一切変更しない。
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

from case_orders_auto_close import (
    BASE_URL,
    SHOP_RAKUTEN,
    SHOP_YAHOO,
    TARGET_CASE_TYPES,
    strip_yahoo_suffix,
    login,
    fetch_case_skus,
    get_spreadsheet,
    append_log,
    LOG_SHEET_NAME,
)
from case_orders_wowma import SHOP_WOWMA, wowma_get_item, wowma_update_stock

JST = timezone(timedelta(hours=9))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Case Groupでの絞り込みはしない（Rakuten/Yahooタグが外れているケースも拾うため）。
# case_status_id: 1=New, 2=In-Progress
CLOSE_LIST_URLS = [
    f"{BASE_URL}/case-orders?case_status_id=1",
    f"{BASE_URL}/case-orders?case_status_id=2",
]


def fetch_close_cases(page) -> list:
    seen_ids = set()
    result = []
    for url in CLOSE_LIST_URLS:
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(500)

        info = page.evaluate(
            """() => {
                const target = [...document.querySelectorAll('table')].find(
                    t => [...t.querySelectorAll('th')].some(th => th.textContent.trim() === 'Case Type')
                );
                if (!target) return null;
                const headers = [...target.querySelectorAll('th')].map(th => th.textContent.trim());
                const idxId = headers.indexOf('Id');
                const idxType = headers.indexOf('Case Type');
                const idxProduct = headers.indexOf('Product');
                return [...target.querySelectorAll('tbody tr')].map(tr => {
                    const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                    return {
                        id: (tds[idxId] || '').replace(/,/g, ''),
                        caseType: tds[idxType] || '',
                        product: tds[idxProduct] || '',
                    };
                });
            }"""
        )
        if not info:
            print(f"！ケース一覧のテーブルが見つかりませんでした（{url}）。")
            continue
        for r in info:
            if r["caseType"] in TARGET_CASE_TYPES and r["id"] and r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                result.append(r)
    return result


def already_wowma_handled(log_ws_values: list, case_id: str) -> bool:
    # LOG_HEADER = [実行日時, ケースID, Case Type, モール, 店舗, 商品コード, 結果]
    for row in log_ws_values:
        if (len(row) >= 7 and row[1] == case_id and row[3] == SHOP_WOWMA
                and "DRY RUN" not in row[6]):
            return True
    return False


def main():
    print("=== Wowma在庫 後追い処理（Close） 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：Wowmaへの書き込みは行いません")

    spreadsheet = get_spreadsheet()
    try:
        log_ws = spreadsheet.worksheet(LOG_SHEET_NAME)
        log_values = log_ws.get_all_values()
    except Exception:
        log_values = []

    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        try:
            login(page)
            cases = fetch_close_cases(page)
        except Exception as e:
            print(f"ケース一覧の取得に失敗しました: {e}")
            browser.close()
            sys.exit(1)

        targets = [c for c in cases if not already_wowma_handled(log_values, c["id"])]
        print(f"Closeケース（New/In-Progress）: {len(cases)}件 → Wowma未処理: {len(targets)}件")

        for case in targets:
            case_id = case["id"]
            print(f"\n--- ケース {case_id}（{case['product']}） ---")

            try:
                skus = fetch_case_skus(page, case_id)
            except Exception as e:
                print(f"  Related Skus の取得に失敗: {e}")
                continue

            rakuten_skus = [s["sku"] for s in skus if s["mall"] == SHOP_RAKUTEN]
            yahoo_skus = [s["sku"] for s in skus if s["mall"] == SHOP_YAHOO]
            derived_bases = []
            for ysku in yahoo_skus:
                base = strip_yahoo_suffix(ysku)
                if base and base not in rakuten_skus and base not in derived_bases:
                    derived_bases.append(base)

            candidates = list(dict.fromkeys(rakuten_skus + derived_bases))
            if not candidates:
                print("  楽天SKUがありません。スキップ。")
                continue

            for sku in candidates:
                try:
                    item = wowma_get_item(sku)
                except Exception as e:
                    print(f"  [Wowma] {sku}: 取得エラー: {e}")
                    log_rows.append([now, case_id, "Close", SHOP_WOWMA, "-", sku, f"取得エラー: {e}"])
                    continue
                if item is None:
                    print(f"  [Wowma] {sku}: 出品なし（対象外）")
                    log_rows.append([now, case_id, "Close", SHOP_WOWMA, "-", sku, "Wowmaに出品なし"])
                    continue
                if item.get("stockCount") == "0":
                    print(f"  [Wowma] {sku}: すでに在庫0")
                    log_rows.append([now, case_id, "Close", SHOP_WOWMA, "-", sku, "すでに在庫0"])
                    continue
                ok, message = wowma_update_stock(sku, 0, dry_run=DRY_RUN)
                print(f"  [Wowma] {sku}: {message}")
                log_rows.append([now, case_id, "Close", SHOP_WOWMA, "-", sku, message])

        browser.close()

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print("=== Wowma在庫 後追い処理（Close） 完了 ===")


if __name__ == "__main__":
    main()
