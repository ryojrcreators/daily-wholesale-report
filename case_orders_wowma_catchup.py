"""
Wowma価格の「後追い」処理。

case_orders_price_adjust.py は楽天・YahooをGitHub Actionsで自動実行しているが、
Wowmaは固定IPからしか呼べないためこのPC上での手動実行でしか処理できない
（WOWMA_ENABLED、詳細は case_orders_price_adjust.py の docstring 参照）。

そのため、GitHub Actionsが先にケースを処理してしまうと、
  - Reply「Rakuten/yahoo Raised」が投稿され
  - Case Groupsが Rakuten/Yahoo のみだった場合は Status が In-Progress になる
  - 他のグループ（Amazon等）も付いていた場合は、Rakuten/Yahooタグだけが外れ、
    Status は New のまま残る
ため、通常の一覧（case_status_id=1&case_group_id[0]=4）からはそのケースが
消えてしまい、Wowma分だけ取りこぼされる（2026-08-13、実際にケース155234・155228で発生：
タグが外れて New のままだが Rakuten/Yahoo グループには該当しなくなった）。

このスクリプトは Case Group での絞り込みをせず、New / In-Progress の
Change Price ケースをすべて対象に、まだWowmaの記録（自動Close_ログにmall=Wowmaの行）が
無いケースだけを拾って、Wowma価格だけを追いで更新する。
ケースのステータス・Replyは一切変更しない（楽天・Yahoo側は既に処理済み、または
まだ手つかずでも次回の通常実行に任せるため）。
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

from case_orders_auto_close import (
    BASE_URL,
    SHOP_RAKUTEN,
    login,
    get_spreadsheet,
    append_log,
    LOG_SHEET_NAME,
)
from case_orders_price_adjust import (
    fetch_price_rows,
    calc_sell_price,
    get_case_purchase_price_usd,
    parse_price,
    PRICE_TOLERANCE_YEN,
    post_chatwork,
    CW_MENTION_RYO,
)
from case_orders_wowma import SHOP_WOWMA, wowma_get_item, wowma_update_price

JST = timezone(timedelta(hours=9))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Case Groupでの絞り込みはしない（Rakuten/Yahooタグが外れているケースも拾うため）。
# case_status_id: 1=New, 2=In-Progress
CHANGE_PRICE_LIST_URLS = [
    f"{BASE_URL}/case-orders?case_status_id=1",
    f"{BASE_URL}/case-orders?case_status_id=2",
]


def fetch_change_price_cases(page) -> list:
    seen_ids = set()
    result = []
    for url in CHANGE_PRICE_LIST_URLS:
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
            if r["caseType"] == "Change Price" and r["id"] and r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                result.append(r)
    return result


def already_wowma_handled(log_ws_values: list, case_id: str) -> bool:
    # LOG_HEADER = [実行日時, ケースID, Case Type, モール, 店舗, 商品コード, 結果]
    for row in log_ws_values:
        if len(row) >= 4 and row[1] == case_id and row[3] == SHOP_WOWMA:
            return True
    return False


def main():
    print("=== Wowma価格 後追い処理 開始 ===")
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
    anomalies = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()

        try:
            login(page)
            cases = fetch_change_price_cases(page)
        except Exception as e:
            print(f"ケース一覧の取得に失敗しました: {e}")
            browser.close()
            sys.exit(1)

        targets = [c for c in cases if not already_wowma_handled(log_values, c["id"])]
        print(f"Change Price ケース（New/In-Progress）: {len(cases)}件 → Wowma未処理: {len(targets)}件")

        for case in targets:
            case_id = case["id"]
            print(f"\n--- ケース {case_id}（{case['product']}） ---")

            rows = fetch_price_rows(page, case_id)
            rakuten_rows = [r for r in rows if r["mall"] == SHOP_RAKUTEN]
            if not rakuten_rows:
                print("  楽天SKUがありません。スキップ。")
                continue

            expected_purchase_usd = get_case_purchase_price_usd(page)
            if expected_purchase_usd is not None:
                print(f"  Descriptionの仕入価格: ${expected_purchase_usd:.2f}")

            for row in rakuten_rows:
                rakuten_sku = row["sku"]
                new_price, detail = calc_sell_price(page, case_id, row["rowIndex"],
                                                     expected_purchase_usd=expected_purchase_usd)
                if new_price is None:
                    print(f"    [楽天] {rakuten_sku}: 価格を計算できませんでした（{detail}）")
                    log_rows.append([now, case_id, "Change Price", SHOP_WOWMA, "-", rakuten_sku,
                                     f"計算失敗: {detail}"])
                    continue

                try:
                    item = wowma_get_item(rakuten_sku)
                except Exception as e:
                    print(f"    [Wowma] {rakuten_sku}: 取得エラー: {e}")
                    log_rows.append([now, case_id, "Change Price", SHOP_WOWMA, "-", rakuten_sku,
                                     f"取得エラー: {e}"])
                    continue

                if item is None:
                    print(f"    [Wowma] {rakuten_sku}: 出品なし（対象外）")
                    log_rows.append([now, case_id, "Change Price", SHOP_WOWMA, "-", rakuten_sku,
                                     "Wowmaに出品なし"])
                    continue

                current = parse_price(item.get("itemPrice", "0"))
                print(f"    [Wowma] {rakuten_sku}: 現在 ¥{current:,} → 計算結果 ¥{new_price:,}（{detail}）")

                if current and current >= new_price:
                    print("      現在価格が計算結果以上のため、変更しません")
                    log_rows.append([now, case_id, "Change Price", SHOP_WOWMA, "-", rakuten_sku,
                                     f"変更なし（現在¥{current:,} ≧ 計算¥{new_price:,}）"])
                    continue

                if DRY_RUN:
                    print(f"      【DRY RUN】¥{new_price:,} に更新する対象")
                    log_rows.append([now, case_id, "Change Price", SHOP_WOWMA, "-", rakuten_sku,
                                     f"【DRY RUN】→¥{new_price:,}"])
                    continue

                ok, message = wowma_update_price(rakuten_sku, new_price, dry_run=False)
                print(f"      {message}")
                log_rows.append([now, case_id, "Change Price", SHOP_WOWMA, "-", rakuten_sku, message])
                time.sleep(1)

                if ok:
                    try:
                        confirm = wowma_get_item(rakuten_sku)
                    except Exception:
                        confirm = None
                    confirm_price = parse_price(confirm.get("itemPrice", "0")) if confirm else None
                    if confirm_price is None or abs(confirm_price - new_price) > PRICE_TOLERANCE_YEN:
                        anomalies.append(
                            f"[Wowma] {rakuten_sku}（後追い処理）: 書き込んだ値 ¥{new_price:,} だが実際は "
                            f"{'取得失敗' if confirm_price is None else f'¥{confirm_price:,}'}"
                        )

        browser.close()

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    if anomalies:
        print(f"\n⚠️ 検証で異常を検出しました（{len(anomalies)}件）:")
        for a in anomalies:
            print(f"  {a}")
        body = (
            f"{CW_MENTION_RYO}\n"
            f"[info][title]Wowma後追い処理の検証で異常を検出（{len(anomalies)}件）[/title]"
            + "\n".join(anomalies)
            + "\n\n価格は自動では戻していません。内容の確認をお願いします。[/info]"
        )
        post_chatwork(body)
    else:
        print("\n検証で異常は見つかりませんでした。")

    print("=== Wowma価格 後追い処理 完了 ===")


if __name__ == "__main__":
    main()
