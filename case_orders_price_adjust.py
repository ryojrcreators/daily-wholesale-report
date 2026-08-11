"""
社内システムのCase Orders（Change Price依頼）を読み取り、楽天・Yahooの販売価格を自動で更新する。

処理の流れ:
  1. Playwrightで app.jrcreators.com にログイン（case_orders_auto_close.py と同じ2段階）
  2. New かつ Case Group に Rakuten/Yahoo を含むケースのうち、Case Type が Change Price のものを対象にする
  3. 各ケースの Related Skus から、Shop が「楽天」「Yahoo(new)」の行を取り出す
  4. その行の Calc（社内の価格計算ツール /products/calculator）を開いて計算させ、
     Sell Price を新しい販売価格として読み取る
  5. 現在価格より高い場合だけ、楽天・Yahooの価格を更新する
  6. ケースに Reply を入れ、Case Groups から Rakuten/Yahoo を外す（単独なら In-Progress にする）
  7. 結果をスプレッドシートの「自動Close_ログ」タブに追記する

なぜ Min 列を使わず計算ツールを開くのか:
  ケース画面の Min（＝利益率18%での推奨価格）は、そのまま使えれば楽だが、
  ケースの仕入価格が反映されず古いままのことがある。
  計算ツールはケースの仕入価格で計算し直すため、常に正しい値が得られる。

なぜ計算式を自前で持たないのか:
  計算には 実重量と容積重量の比較、配送方法の選択、材料費、輸入税（食品・危険物フラグ）、
  社内の為替レートが絡む。外部で再現し続けるとズレて価格を誤るため、
  社内ツールにそのまま計算させる。
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

from case_orders_auto_close import (
    BASE_URL,
    SHOP_RAKUTEN,
    SHOP_YAHOO,
    CASE_GROUP_RAKUTEN_YAHOO,
    CASE_STATUS_IN_PROGRESS,
    login,
    fetch_target_cases,
    update_case,
    get_spreadsheet,
    get_yahoo_access_token,
    append_log,
)
from rakuten_price_adjust import (
    rakuten_update_price,
    yahoo_update_price,
    yahoo_check_sale_conflict,
)

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))

TARGET_CASE_TYPES = ("Change Price",)
REPLY_MESSAGE = "Rakuten/yahoo Raised"

CALCULATOR_PATH = "/products/calculator"


def fetch_price_rows(page, case_id: str) -> list:
    """
    Related Skus から、楽天・Yahooの行を「行番号つき」で取り出す。
    行番号は、あとで同じ行の Calc を押すために使う。
    """
    page.goto(f"{BASE_URL}/case-orders/view/{case_id}", wait_until="networkidle")
    page.wait_for_timeout(300)

    rows = page.evaluate(
        """() => {
            const table = [...document.querySelectorAll('table')].find(t => {
                const hs = [...t.querySelectorAll('th')].map(th => th.textContent.trim());
                return hs.includes('Sku') && hs.includes('Shop');
            });
            if (!table) return null;

            const headers = [...table.querySelectorAll('th')].map(th => th.textContent.trim());
            const idx = name => headers.indexOf(name);
            return [...table.querySelectorAll('tbody tr')].map((tr, i) => {
                const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                return {
                    rowIndex: i,
                    mall: tds[idx('Shop')] || '',
                    sku: tds[idx('Sku')] || '',
                    salesPrice: tds[idx('Sales Price')] || '',
                    min: tds[idx('Min')] || '',
                };
            }).filter(r => r.sku);
        }"""
    )

    if rows is None:
        print("  ！Related Skus のテーブルが見つかりません")
        return []

    targets = [r for r in rows if r["mall"] in (SHOP_RAKUTEN, SHOP_YAHOO)]
    print(f"  Related Skus: {len(rows)}件 → 対象 {len(targets)}件")
    for r in targets:
        print(f"    [{r['mall']}] {r['sku']} 現在価格={r['salesPrice']} / Min={r['min']}")
    return targets


def calc_sell_price(page, case_id: str, row_index: int):
    """
    指定行の Calc を押して計算ツールを開き、Sell Price を読み取る。

    計算ツールはケースの仕入価格などが入った状態で開くので、calculate を押すだけでよい。
    別ウィンドウで開く場合と同じタブで開く場合の両方に対応する。
    戻り値は (販売価格, 内訳の説明)。読み取れなければ (None, 理由)。
    """
    context = page.context
    before = set(context.pages)

    try:
        with context.expect_page(timeout=8000) as popup_info:
            page.evaluate(
                """(i) => {
                    const table = [...document.querySelectorAll('table')].find(t => {
                        const hs = [...t.querySelectorAll('th')].map(th => th.textContent.trim());
                        return hs.includes('Sku') && hs.includes('Shop');
                    });
                    const row = table.querySelectorAll('tbody tr')[i];
                    const link = [...row.querySelectorAll('a')].find(a => a.textContent.trim() === 'Calc');
                    link.click();
                }""",
                row_index,
            )
        calc_page = popup_info.value
    except Exception:
        # 同じタブで開いた場合
        calc_page = next((p for p in context.pages if CALCULATOR_PATH in p.url), None)
        if calc_page is None:
            return None, "計算ツールが開きませんでした"

    calc_page.wait_for_load_state("networkidle")
    calc_page.wait_for_timeout(300)

    if CALCULATOR_PATH not in calc_page.url:
        return None, f"計算ツール以外のページが開きました（{calc_page.url}）"

    inputs = calc_page.evaluate(
        """() => {
            const val = sel => document.querySelector(sel)?.value || '';
            return {
                purchase: val('#purchase-price, [name=purchase_price]'),
                weight: val('#weight-lb, [name=weight_lb]'),
                body: document.body.innerText.slice(0, 400),
            };
        }"""
    )
    print(f"    計算ツールの入力: 仕入={inputs.get('purchase')} / 重量={inputs.get('weight')}")

    try:
        calc_page.click('button:has-text("calculate"), input[value="calculate"]')
        calc_page.wait_for_timeout(1500)
    except Exception as e:
        calc_page.close()
        return None, f"calculateボタンを押せませんでした: {e}"

    result = calc_page.evaluate(
        """() => {
            // 「Sell Price」のラベルと同じ行にある入力欄の値を読む
            const cells = [...document.querySelectorAll('td, th, div, label')];
            const label = cells.find(c => c.textContent.trim().startsWith('Sell Price'));
            if (!label) return null;
            const row = label.closest('tr') || label.parentElement;
            const input = row ? row.querySelector('input') : null;
            const value = input ? input.value : null;

            const pick = name => {
                const el = cells.find(c => c.textContent.trim().startsWith(name));
                if (!el) return '';
                const r = el.closest('tr');
                if (!r) return '';
                const tds = [...r.querySelectorAll('td, input')];
                const last = tds[tds.length - 1];
                return last ? (last.value || last.textContent.trim()) : '';
            };
            return {
                sellPrice: value,
                exchangeRate: pick('Exchange Rate'),
                profit: pick('Profit %'),
                shippingFee: pick('Shipping Fee'),
            };
        }"""
    )
    calc_page.close()

    if not result or not result.get("sellPrice"):
        return None, "Sell Price を読み取れませんでした"

    try:
        price = int(str(result["sellPrice"]).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return None, f"Sell Price が数値ではありません: {result['sellPrice']}"

    detail = (f"為替={result.get('exchangeRate')} / 利益率={result.get('profit')}% "
              f"/ 送料={result.get('shippingFee')}")
    return price, detail


def parse_price(value: str) -> int:
    try:
        return int(str(value).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return 0


def main():
    print("=== Case Orders 価格調整 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：モール側もケース側も一切変更しません")

    spreadsheet = get_spreadsheet()
    yahoo_token = get_yahoo_access_token(spreadsheet)
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()

        try:
            login(page)
            cases = fetch_target_cases(page, TARGET_CASE_TYPES, "Change Price")
        except Exception as e:
            print(f"ケース一覧の取得に失敗しました: {e}")
            browser.close()
            sys.exit(1)

        if not cases:
            print("対象ケースなし。終了。")
            browser.close()
            return

        for case in cases[:MAX_PER_RUN]:
            case_id = case["id"]
            print(f"\n--- ケース {case_id}（{case['caseType']} / {case['product']}） ---")

            rows = fetch_price_rows(page, case_id)
            if not rows:
                print("  楽天・YahooのSKUがありません。Newのまま残します。")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", "対象SKUなし（Newのまま）"])
                continue

            all_ok = True
            changed_any = False

            for row in rows:
                mall, sku = row["mall"], row["sku"]
                current = parse_price(row["salesPrice"])

                new_price, detail = calc_sell_price(page, case_id, row["rowIndex"])
                if new_price is None:
                    print(f"    [{mall}] {sku}: 価格を計算できませんでした（{detail}）")
                    log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                     f"計算失敗: {detail}"])
                    all_ok = False
                    continue

                print(f"    [{mall}] {sku}: 現在 ¥{current:,} → 計算結果 ¥{new_price:,}（{detail}）")

                if current >= new_price:
                    # 値下げはしない。すでに計算結果以上で売れているなら触らない
                    print("      現在価格が計算結果以上のため、変更しません")
                    log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                     f"変更なし（現在¥{current:,} ≧ 計算¥{new_price:,}）"])
                    continue

                if DRY_RUN:
                    print(f"      【DRY RUN】¥{current:,} → ¥{new_price:,} に更新する対象")
                    log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                     f"【DRY RUN】¥{current:,}→¥{new_price:,}"])
                    changed_any = True
                    continue

                if mall == SHOP_RAKUTEN:
                    results = rakuten_update_price(sku, new_price)
                else:
                    # Yahooはセール中だと価格更新でセールを解除してしまうため、その場合は触らない
                    if yahoo_check_sale_conflict(yahoo_token, [sku]):
                        print("      Yahooがセール中のため、変更せず人の確認に回します")
                        log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                         "要確認（Yahooセール中）"])
                        all_ok = False
                        continue
                    results = yahoo_update_price(yahoo_token, [sku], new_price)

                for store_name, message, ok in results:
                    print(f"      {store_name}: {message}")
                    log_rows.append([now, case_id, case["caseType"], mall, store_name, sku, message])
                    if ok:
                        changed_any = True
                    else:
                        all_ok = False

                time.sleep(1)

            if not all_ok:
                print("  ⚠️ 失敗があったため、ケースは New のまま残します（次回再挑戦）。")
                continue

            if DRY_RUN:
                print("  【DRY RUN】本番ならここでケースを更新します。")
                continue

            try:
                action = update_case(page, case_id, REPLY_MESSAGE)
                print(f"  ✅ {action}／Reply「{REPLY_MESSAGE}」を投稿しました。")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", action])
            except Exception as e:
                print(f"  ⚠️ ケース更新に失敗しました（価格は反映済み）: {e}")
                log_rows.append([now, case_id, case["caseType"], "-", "-", "-", f"ケース更新失敗: {e}"])

        browser.close()

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print("=== Case Orders 価格調整 完了 ===")


if __name__ == "__main__":
    main()
