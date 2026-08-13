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
    YAHOO_SUFFIXES,
    YAHOO_STORES,
    yahoo_get_item,
    SHOP_RAKUTEN,
    SHOP_YAHOO,
    CASE_GROUP_RAKUTEN_YAHOO,
    CASE_STATUS_IN_PROGRESS,
    strip_yahoo_suffix,
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


def read_sell_price(calc_page):
    """計算ツールの結果から Sell Price と内訳を読み取る"""
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
    if not result or not result.get("sellPrice"):
        return None, "Sell Price を読み取れませんでした"
    try:
        price = int(str(result["sellPrice"]).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return None, f"Sell Price が数値ではありません: {result['sellPrice']}"

    detail = (f"為替={result.get('exchangeRate')} / 利益率={result.get('profit')}% "
              f"/ 送料={result.get('shippingFee')}")
    return price, detail


def calc_for_shop(calc_page, keyword: str):
    """
    計算ツールの Shop を切り替えて計算し直す。
    楽天とYahooでは手数料が違うため、モールごとに計算しないと価格を誤る。
    """
    switched = calc_page.evaluate(
        """(keyword) => {
            const selects = [...document.querySelectorAll('select')];
            const shop = selects.find(s => [...s.options].some(o => o.textContent.includes('楽天')));
            if (!shop) return null;
            const option = [...shop.options].find(o => o.textContent.includes(keyword));
            if (!option) return { options: [...shop.options].map(o => o.textContent.trim()) };
            shop.value = option.value;
            shop.dispatchEvent(new Event('change', { bubbles: true }));
            return { selected: option.textContent.trim() };
        }""",
        keyword,
    )
    if switched is None:
        return None, "Shopの選択欄が見つかりません"
    if "selected" not in switched:
        return None, f"「{keyword}」を含む選択肢がありません（候補: {switched.get('options')}）"

    calc_page.click('button:has-text("calculate"), input[value="calculate"]')
    calc_page.wait_for_timeout(1500)
    return read_sell_price(calc_page)


def calc_sell_price(page, case_id: str, row_index: int, shop_keyword: str = None):
    """
    指定行の Calc を押して計算ツールを開き、Sell Price を読み取る。

    計算ツールはケースの仕入価格などが入った状態で開くので、calculate を押すだけでよい。
    shop_keyword を渡すと、Shop をその文字を含む選択肢に切り替えてから計算する
    （Related Skus にYahoo行が無い商品でも、Yahoo価格を出せるようにするため）。
    戻り値は (販売価格, 内訳の説明)。読み取れなければ (None, 理由)。
    """
    context = page.context
    before = set(context.pages)

    # Calc は window.open で固定名の窓を使っている可能性があり、その場合2回目以降は
    # 「新しいページが開いた」イベントが発火せず expect_page がタイムアウトする。
    # 事前に既存の計算ツール窓を閉じておくことで、毎回確実に新規ページとして検出させる。
    for stray in list(context.pages):
        if stray is not page and CALCULATOR_PATH in stray.url:
            try:
                stray.close()
            except Exception:
                pass

    calc_page = None
    last_error = None
    for attempt in range(2):
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
            break
        except Exception as e:
            last_error = e
            # 同じタブで開いた場合
            calc_page = next((p for p in context.pages if p is not page and CALCULATOR_PATH in p.url), None)
            if calc_page is not None:
                break
            page.wait_for_timeout(500)

    if calc_page is None:
        return None, f"計算ツールが開きませんでした（{attempt + 1}回試行）: {last_error}"

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

    if shop_keyword:
        price, detail = calc_for_shop(calc_page, shop_keyword)
        calc_page.close()
        return price, detail

    try:
        calc_page.click('button:has-text("calculate"), input[value="calculate"]')
        calc_page.wait_for_timeout(1500)
    except Exception as e:
        calc_page.close()
        return None, f"calculateボタンを押せませんでした: {e}"

    price, detail = read_sell_price(calc_page)
    calc_page.close()
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

            rakuten_rows = [r for r in rows if r["mall"] == SHOP_RAKUTEN]
            yahoo_rows = [r for r in rows if r["mall"] == SHOP_YAHOO]

            def apply_price(mall, sku, current, new_price, detail, store_hint=""):
                """計算結果を1つのSKUに反映する。戻り値は (成功したか, 変更したか)"""
                nonlocal log_rows
                print(f"    [{mall}] {sku}{store_hint}: 現在 ¥{current:,} → 計算結果 ¥{new_price:,}（{detail}）")

                if current and current >= new_price:
                    # 値下げはしない。すでに計算結果以上で売れているなら触らない
                    print("      現在価格が計算結果以上のため、変更しません")
                    log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                     f"変更なし（現在¥{current:,} ≧ 計算¥{new_price:,}）"])
                    return True, False

                if DRY_RUN:
                    print(f"      【DRY RUN】¥{new_price:,} に更新する対象")
                    log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                     f"【DRY RUN】→¥{new_price:,}"])
                    return True, True

                if mall == SHOP_RAKUTEN:
                    results = rakuten_update_price(sku, new_price)
                else:
                    # Yahooはセール中だと価格更新でセールを解除してしまうため、その場合は触らない
                    if yahoo_check_sale_conflict(yahoo_token, [sku]):
                        print("      Yahooがセール中のため、変更せず人の確認に回します")
                        log_rows.append([now, case_id, case["caseType"], mall, "-", sku,
                                         "要確認（Yahooセール中）"])
                        return False, False
                    results = yahoo_update_price(yahoo_token, [sku], new_price)

                ok_all, changed = True, False
                for store_name, message, ok in results:
                    print(f"      {store_name}: {message}")
                    log_rows.append([now, case_id, case["caseType"], mall, store_name, sku, message])
                    if ok:
                        changed = True
                    else:
                        ok_all = False
                time.sleep(1)
                return ok_all, changed

            # 1つのケースに複数の商品（＝複数の楽天SKU）が束ねられていることがあるため、
            # 楽天SKUごとに「その商品専用のYahoo価格」を計算する。
            # 過去バグ: ケース内で最初に計算できたYahoo価格を全SKUに使い回してしまい、
            # 無関係な商品にまで同じ誤った価格を書き込んだ（2026-08-13、8件を復旧）。
            # 二度と起きないよう、Yahoo価格は必ず「対象の楽天SKU」とセットで扱う。
            handled_yahoo_codes = set()

            for row in rakuten_rows:
                rakuten_sku = row["sku"]
                current = parse_price(row["salesPrice"])
                new_price, detail = calc_sell_price(page, case_id, row["rowIndex"])
                if new_price is None:
                    print(f"    [楽天] {rakuten_sku}: 価格を計算できませんでした（{detail}）")
                    log_rows.append([now, case_id, case["caseType"], SHOP_RAKUTEN, "-", rakuten_sku,
                                     f"計算失敗: {detail}"])
                    all_ok = False
                    continue

                ok, changed = apply_price(SHOP_RAKUTEN, rakuten_sku, current, new_price, detail)
                all_ok = all_ok and ok
                changed_any = changed_any or changed

                # 同じ行でShopをYahooに切り替えて、この商品専用のYahoo価格を求める
                yahoo_price, yahoo_detail = calc_sell_price(
                    page, case_id, row["rowIndex"], shop_keyword="Yahoo")

                # この楽天SKUに対応するYahooコードだけを対象にする（他の商品とは絶対に混ぜない）。
                # startswith だと "bb-051999-2akc" が "bb-051999" にも一致してしまうため
                # （実際にこれが原因で無関係な商品に価格を書いてしまった）、接尾辞を正確に
                # 取り除いた完全一致でのみ紐付ける。
                candidates = [row["sku"] + suffix for suffix in YAHOO_SUFFIXES]
                for yr in yahoo_rows:
                    if (strip_yahoo_suffix(yr["sku"]).lower() == rakuten_sku.lower()
                            and yr["sku"] not in candidates):
                        candidates.append(yr["sku"])

                group_targets = {}
                for code in candidates:
                    if code in handled_yahoo_codes:
                        # 他の楽天SKUで既に確定済み（想定外の衝突）。二重処理を避けるためスキップ
                        print(f"    ⚠️ {code} は他の商品で既に処理済みのためスキップします")
                        continue
                    for store in YAHOO_STORES:
                        try:
                            item = yahoo_get_item(yahoo_token, store, code)
                        except Exception as e:
                            print(f"    [Yahoo] {code} の確認に失敗: {e}")
                            all_ok = False
                            continue
                        if item is not None:
                            group_targets[code] = parse_price(item.get("Price", "0"))
                            handled_yahoo_codes.add(code)
                            break

                if not group_targets:
                    continue

                print(f"    [{rakuten_sku}] 対応するYahoo商品: {list(group_targets)}")

                if yahoo_price is None:
                    print(f"    Yahoo価格を計算できませんでした（{yahoo_detail}）。"
                          f"この商品のYahooは変更しません")
                    for code in group_targets:
                        log_rows.append([now, case_id, case["caseType"], SHOP_YAHOO, "-", code,
                                         f"計算失敗（楽天{rakuten_sku}側）: {yahoo_detail}"])
                    all_ok = False
                    continue

                for code, current_y in group_targets.items():
                    ok, changed = apply_price(SHOP_YAHOO, code, current_y, yahoo_price, yahoo_detail)
                    all_ok = all_ok and ok
                    changed_any = changed_any or changed

            # Related Skus にあるのに、どの楽天SKUの接尾辞候補にも一致しなかったYahoo行
            # （楽天側の出品が既に無い等）。行自体のCalcで個別に計算する。
            orphan_rows = [r for r in yahoo_rows if r["sku"] not in handled_yahoo_codes]
            for row in orphan_rows:
                current = parse_price(row["salesPrice"])
                new_price, detail = calc_sell_price(page, case_id, row["rowIndex"])
                if new_price is None:
                    print(f"    [Yahoo] {row['sku']}: 価格を計算できませんでした（{detail}）")
                    log_rows.append([now, case_id, case["caseType"], SHOP_YAHOO, "-", row["sku"],
                                     f"計算失敗: {detail}"])
                    all_ok = False
                    continue
                ok, changed = apply_price(SHOP_YAHOO, row["sku"], current, new_price, detail)
                all_ok = all_ok and ok
                changed_any = changed_any or changed

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
