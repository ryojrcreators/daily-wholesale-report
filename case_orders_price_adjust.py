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

価格変更後の検証について（2026-08-13の事故を受けて追加）:
  過去に「ケース内で最初に計算できたYahoo価格を、無関係な他の商品にも使い回してしまい、
  複数商品を同じ誤った価格で書き換えた」事故が実際に起きた。書き込み自体は成功として
  記録されるため、ログを見るだけでは異常に気づけない。そのため書き込み直後に以下を行う。
    1. 同じ行のCalcで再計算し、書き込んだ値と一致するか確認する
    2. 同じケース内で、別の商品（別の楽天SKU）に同じ価格が付いていないか確認する
  数十円のズレは為替レートの丸め等で起こり得るため許容し、それを超える差だけ異常とする。
  異常があれば Chatwork で Ryo Higuchi 宛てに報告する（このスクリプトは自動修正しない）。
  「実際にモールへ反映されたか」の時間差確認（書き込み直後ではなく少し後で再確認）は
  case_orders_price_verify_delayed.py が別ジョブとして行う。

Wowmaについて（2026-08-13追加）:
  Wowma!（現au PAYマーケット）のAPIは登録した固定IPからしか呼べないため、
  GitHub Actions（IPが毎回変わる）からは接続できない。そのためWOWMA_API_KEY/
  WOWMA_SHOP_ID を環境変数に設定したときだけ有効になり（GitHub Secretsには
  登録しない）、通常の自動実行では何もせず、固定IPを許可登録したPC上で
  手動実行したときだけWowmaの価格も更新される。
  Wowmaは楽天と同じ商品コードで出品されており、社内の計算ツールにもWowma専用の
  Shop設定は無いため、楽天と同じ計算価格をそのまま使う。

仕入価格（Purchase Price）について（2026-08-13追加）:
  計算ツールを Calc リンクから開くと、Purchase Price 欄には商品側に登録済みの
  古い仕入価格がそのまま入っており、Change Priceケースを起こす原因になった
  「新しい仕入価格」（ケースの Description に「Purchase price: $134.00 + tax」
  のように書かれている）が反映されていないことがある（ケース155222で実際に発生、
  $96.95のまま計算されて必要な値上げが「変更不要」と誤判定された）。
  そのため、ケースのDescriptionから仕入価格を読み取り、計算ツールを開くたびに
  Purchase Price欄をその値へ書き換えてから計算させる。Descriptionから読み取れない
  場合は、計算ツールの値をそのまま使う（変更前の挙動に fallback）。
"""

import os
import re
import sys
import time
import json
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

from case_orders_auto_close import (
    BASE_URL,
    YAHOO_SUFFIXES,
    YAHOO_STORES,
    yahoo_get_item,
    rakuten_get_current_prices,
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

# Wowmaは固定IPからしか呼べない（Wow!manager側のIP許可リスト制限）ため、
# GitHub Actions（Secrets未設定）では自然にスキップされ、
# WOWMA_API_KEY/WOWMA_SHOP_ID を設定したこのPC上で手動実行したときだけ動く。
WOWMA_ENABLED = bool(os.environ.get("WOWMA_API_KEY")) and bool(os.environ.get("WOWMA_SHOP_ID"))
if WOWMA_ENABLED:
    from case_orders_wowma import SHOP_WOWMA, wowma_get_item, wowma_update_price

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))

TARGET_CASE_TYPES = ("Change Price",)
REPLY_MESSAGE = "Rakuten/yahoo Raised"

CALCULATOR_PATH = "/products/calculator"

# 検証で許容するズレ（円）。為替レートの丸め等による数十円程度の差は異常とみなさない
PRICE_TOLERANCE_YEN = 50

CW_TOKEN = os.environ.get("CW_TOKEN", "")
CW_MENTION_RYO = "[To:2618849]Ryo Higuchiさん"
CW_ROOM_ID = "83994351"

# 次のジョブ（case_orders_price_verify_delayed.py）に渡す、今回書き込んだ内容
APPLIED_CHANGES_FILE = "price_adjust_applied_changes.json"


def post_chatwork(body: str):
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のため通知しません。")
        return
    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data={"body": body},
            timeout=30,
        )
        print(f"  Chatwork通知: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
    except Exception as e:
        print(f"  Chatwork通知に失敗しました: {e}")


def get_case_purchase_price_usd(page):
    """
    ケース画面のDescription欄から「Purchase price: $134.00 + tax」のような
    記載を読み取り、USD建ての仕入価格をfloatで返す。読み取れなければ None。
    呼び出し時点でpageは既にそのケースのview画面を開いている前提。
    """
    text = page.evaluate(
        """() => {
            const h4 = [...document.querySelectorAll('h4')].find(h => h.textContent.trim() === 'Description');
            if (!h4) return null;
            const p = h4.nextElementSibling;
            return p ? p.textContent.trim() : null;
        }"""
    )
    if not text:
        return None
    m = re.search(r"Purchase\s+price:?\s*\$?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


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
                const hasCalc = !![...tr.querySelectorAll('a')].find(a => a.textContent.trim() === 'Calc');
                return {
                    rowIndex: i,
                    mall: tds[idx('Shop')] || '',
                    sku: tds[idx('Sku')] || '',
                    salesPrice: tds[idx('Sales Price')] || '',
                    min: tds[idx('Min')] || '',
                    inactive: tds[idx('Inactive')] || '',
                    hasCalc,
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
        notes = []
        if not r["hasCalc"]:
            notes.append("Calcなし＝セット商品等のため計算対象外")
        if r["inactive"].strip().lower() == "x":
            notes.append("Inactive＝出品されていないため対象外")
        note = f"（{' / '.join(notes)}）" if notes else ""
        print(f"    [{r['mall']}] {r['sku']} 現在価格={r['salesPrice']} / Min={r['min']}{note}")

    # Calcが無い行（セット商品など）は計算できないので最初から除外する。
    # 元々は計算ツールを開いて Cannot read properties of undefined (reading 'click')
    # で失敗させ、それをケース全体の失敗として扱っていたが、この行がある商品だけ
    # 除外すれば残りの単品行は正常に処理できるため、ここで弾いておく。
    #
    # Inactive に X が付いている行は出品されていない商品なので、価格を変えても
    # 意味が無い（かつ楽天/YahooのAPIで見つからずエラーになるだけ）ため除外する。
    excluded = [r for r in targets if not r["hasCalc"] or r["inactive"].strip().lower() == "x"]
    if excluded:
        print(f"  対象外のため除外: {[r['sku'] for r in excluded]}")
    targets = [r for r in targets if r["hasCalc"] and r["inactive"].strip().lower() != "x"]
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


def calc_sell_price(page, case_id: str, row_index: int, shop_keyword: str = None,
                     expected_purchase_usd: float = None):
    """
    指定行の Calc を押して計算ツールを開き、Sell Price を読み取る。

    計算ツールはケースの仕入価格などが入った状態で開くので、通常は calculate を
    押すだけでよい。ただしその仕入価格は商品側に登録済みの古い値のことがあり、
    Change Priceケースの Description に書かれた新しい仕入価格を反映していない
    場合がある。expected_purchase_usd を渡すと、計算ツールの Purchase Price 欄と
    比較し、ズレていれば計算前に上書きする（詳細はモジュールdocstring参照）。
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
                purchase: val('[name=price]'),
                weight: val('[name=weight]'),
                body: document.body.innerText.slice(0, 400),
            };
        }"""
    )
    print(f"    計算ツールの入力: 仕入={inputs.get('purchase')} / 重量={inputs.get('weight')}")

    if expected_purchase_usd is not None:
        try:
            current_purchase = float(str(inputs.get("purchase") or "0").replace(",", ""))
        except ValueError:
            current_purchase = None
        if current_purchase is None or abs(current_purchase - expected_purchase_usd) > 0.01:
            print(f"    ⚠️ 計算ツールの仕入価格(${inputs.get('purchase')})がケースのDescription"
                  f"記載額(${expected_purchase_usd:.2f})と異なるため書き換えます")
            calc_page.evaluate(
                """(v) => {
                    const el = document.querySelector('[name=price]');
                    if (el) {
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                expected_purchase_usd,
            )

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


def recheck_rakuten_price(page, case_id: str, row_index: int, sku: str, applied_price: int, anomalies: list,
                           expected_purchase_usd: float = None, shop_keyword: str = None):
    """
    楽天SKUを再計算＋API再取得して、書き込んだ値と一致するか確認する。
    shop_keyword は、行自体が楽天ではない（Yahoo行から逆引きした楽天出品など）場合に
    "楽天" を渡す。渡さないと、その行のCalcを開いたときの初期Shop（＝その行本来のモール）
    のままになり、楽天ではない価格と比較して誤検知する（2026-08-18、ケース155501で実際に発生）。
    """
    recomputed, _ = calc_sell_price(page, case_id, row_index, shop_keyword=shop_keyword,
                                     expected_purchase_usd=expected_purchase_usd)
    if recomputed is not None and abs(recomputed - applied_price) > PRICE_TOLERANCE_YEN:
        anomalies.append(
            f"[楽天] {sku}: 書き込んだ値 ¥{applied_price:,} だが再計算すると ¥{recomputed:,}"
        )

    actual = rakuten_get_current_prices(sku)
    for store_name, price in actual.items():
        if price is not None and abs(price - applied_price) > PRICE_TOLERANCE_YEN:
            anomalies.append(
                f"[楽天] {sku}（{store_name}）: 書き込んだ値 ¥{applied_price:,} だが実際は ¥{price:,}"
            )


def recheck_yahoo_price(page, case_id: str, row_index: int, yahoo_token: str, applied: dict, anomalies: list,
                         expected_purchase_usd: float = None):
    """
    Yahooグループ（同じ楽天SKUに紐づく複数コード）を再計算＋API再取得して確認する。
    applied は {商品コード: 書き込んだ価格}
    """
    recomputed, _ = calc_sell_price(page, case_id, row_index, shop_keyword="Yahoo",
                                     expected_purchase_usd=expected_purchase_usd)

    for code, applied_price in applied.items():
        if recomputed is not None and abs(recomputed - applied_price) > PRICE_TOLERANCE_YEN:
            anomalies.append(
                f"[Yahoo] {code}: 書き込んだ値 ¥{applied_price:,} だが再計算すると ¥{recomputed:,}"
            )

        for store in YAHOO_STORES:
            try:
                item = yahoo_get_item(yahoo_token, store, code)
            except Exception:
                continue
            if item is None:
                continue
            try:
                actual_price = int(str(item.get("Price", "")).replace(",", ""))
            except ValueError:
                continue
            if abs(actual_price - applied_price) > PRICE_TOLERANCE_YEN:
                anomalies.append(
                    f"[Yahoo] {code}（{store['name']}）: 書き込んだ値 ¥{applied_price:,} "
                    f"だが実際は ¥{actual_price:,}"
                )
            break


def main():
    print("=== Case Orders 価格調整 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：モール側もケース側も一切変更しません")

    spreadsheet = get_spreadsheet()
    yahoo_token = get_yahoo_access_token(spreadsheet)
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []
    anomalies = []          # 検証で見つかった異常の説明文リスト（Chatwork報告用）
    applied_changes = []    # {case_id, mall, sku, price}。時間差検証ジョブに渡す

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

            expected_purchase_usd = get_case_purchase_price_usd(page)
            if expected_purchase_usd is not None:
                print(f"  Descriptionの仕入価格: ${expected_purchase_usd:.2f}"
                      "（計算ツールの値と異なれば書き換えて計算します）")
            else:
                print("  ⚠️ DescriptionからPurchase priceを読み取れませんでした。"
                      "計算ツールの値をそのまま使います。")

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
                elif WOWMA_ENABLED and mall == SHOP_WOWMA:
                    ok, message = wowma_update_price(sku, new_price, dry_run=False)
                    results = [("Wowma", message, ok)]
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
                new_price, detail = calc_sell_price(page, case_id, row["rowIndex"],
                                                     expected_purchase_usd=expected_purchase_usd)
                if new_price is None:
                    print(f"    [楽天] {rakuten_sku}: 価格を計算できませんでした（{detail}）")
                    log_rows.append([now, case_id, case["caseType"], SHOP_RAKUTEN, "-", rakuten_sku,
                                     f"計算失敗: {detail}"])
                    all_ok = False
                    continue

                ok, changed = apply_price(SHOP_RAKUTEN, rakuten_sku, current, new_price, detail)
                all_ok = all_ok and ok
                changed_any = changed_any or changed
                if changed and not DRY_RUN:
                    applied_changes.append({"case_id": case_id, "mall": SHOP_RAKUTEN,
                                            "sku": rakuten_sku, "price": new_price})
                    # 書き込み直後にもう一度Calcで計算し直し、実際の値もAPIから読み直して
                    # 書き込んだ値と一致するか確認する（2026-08-13の事故を受けて追加）
                    recheck_rakuten_price(page, case_id, row["rowIndex"], rakuten_sku, new_price, anomalies,
                                           expected_purchase_usd=expected_purchase_usd)

                # Wowmaは楽天と同じ商品コード・同じ計算価格を使う（社内ツールに専用のShop設定は無い）。
                # 出品が無ければ静かにスキップする。
                if WOWMA_ENABLED:
                    try:
                        wowma_item = wowma_get_item(rakuten_sku)
                    except Exception as e:
                        print(f"    [Wowma] {rakuten_sku}: 取得エラー: {e}")
                        wowma_item = None
                        all_ok = False
                    if wowma_item is not None:
                        current_w = parse_price(wowma_item.get("itemPrice", "0"))
                        ok, w_changed = apply_price(SHOP_WOWMA, rakuten_sku, current_w, new_price, detail)
                        all_ok = all_ok and ok
                        changed_any = changed_any or w_changed
                        if w_changed and not DRY_RUN:
                            applied_changes.append({"case_id": case_id, "mall": SHOP_WOWMA,
                                                    "sku": rakuten_sku, "price": new_price})
                            try:
                                confirm = wowma_get_item(rakuten_sku)
                            except Exception:
                                confirm = None
                            confirm_price = parse_price(confirm.get("itemPrice", "0")) if confirm else None
                            if confirm_price is None or abs(confirm_price - new_price) > PRICE_TOLERANCE_YEN:
                                anomalies.append(
                                    f"[Wowma] {rakuten_sku}: 書き込んだ値 ¥{new_price:,} だが実際は "
                                    f"{'取得失敗' if confirm_price is None else f'¥{confirm_price:,}'}"
                                )

                # 同じ行でShopをYahooに切り替えて、この商品専用のYahoo価格を求める
                yahoo_price, yahoo_detail = calc_sell_price(
                    page, case_id, row["rowIndex"], shop_keyword="Yahoo",
                    expected_purchase_usd=expected_purchase_usd)

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

                yahoo_changed = {}
                for code, current_y in group_targets.items():
                    ok, changed = apply_price(SHOP_YAHOO, code, current_y, yahoo_price, yahoo_detail)
                    all_ok = all_ok and ok
                    changed_any = changed_any or changed
                    if changed and not DRY_RUN:
                        applied_changes.append({"case_id": case_id, "mall": SHOP_YAHOO,
                                                "sku": code, "price": yahoo_price})
                        yahoo_changed[code] = yahoo_price

                if yahoo_changed and not DRY_RUN:
                    # 書き込み直後にもう一度Calcで計算し直し、実際の値もAPIから読み直して
                    # 書き込んだ値と一致するか確認する
                    recheck_yahoo_price(page, case_id, row["rowIndex"], yahoo_token, yahoo_changed, anomalies,
                                         expected_purchase_usd=expected_purchase_usd)

            # Related Skus にあるのに、どの楽天SKUの接尾辞候補にも一致しなかったYahoo行
            # （楽天側の行がRelated Skusに載っていない等）。行自体のCalcで個別に計算する。
            # shop_keyword="Yahoo" を明示して、この行のShopがYahooになるようにする
            # （Calc初期表示のShopは行の実際のモールと一致しないことがあるため）。
            orphan_rows = [r for r in yahoo_rows if r["sku"] not in handled_yahoo_codes]

            # 逆引き：Related Skusに楽天行が無いYahoo行でも、実際には楽天にも出品が
            # 存在することがある（2026-08-18、ケース155501で実際に発生：Yahoo行
            # 13016320-akしか載っていなかったが、接尾辞を外した13016320で楽天出品が実在した）。
            # case_orders_auto_close.py の derived_bases と同じ考え方をこちらにも入れる。
            rakuten_existing_skus = set(r["sku"] for r in rakuten_rows)
            orphan_rakuten_done = set()

            for row in orphan_rows:
                current = parse_price(row["salesPrice"])
                new_price, detail = calc_sell_price(page, case_id, row["rowIndex"], shop_keyword="Yahoo",
                                                     expected_purchase_usd=expected_purchase_usd)
                if new_price is None:
                    print(f"    [Yahoo] {row['sku']}: 価格を計算できませんでした（{detail}）")
                    log_rows.append([now, case_id, case["caseType"], SHOP_YAHOO, "-", row["sku"],
                                     f"計算失敗: {detail}"])
                    all_ok = False
                    continue
                ok, changed = apply_price(SHOP_YAHOO, row["sku"], current, new_price, detail)
                all_ok = all_ok and ok
                changed_any = changed_any or changed
                if changed and not DRY_RUN:
                    applied_changes.append({"case_id": case_id, "mall": SHOP_YAHOO,
                                            "sku": row["sku"], "price": new_price})
                    recheck_yahoo_price(page, case_id, row["rowIndex"], yahoo_token,
                                        {row["sku"]: new_price}, anomalies,
                                        expected_purchase_usd=expected_purchase_usd)

                base = strip_yahoo_suffix(row["sku"])
                if not base or base in rakuten_existing_skus or base in orphan_rakuten_done:
                    continue
                orphan_rakuten_done.add(base)

                try:
                    rakuten_prices = rakuten_get_current_prices(base)
                except Exception as e:
                    print(f"    [楽天(推測)] {base}: 取得エラー: {e}")
                    continue
                found_prices = [p for p in rakuten_prices.values() if p is not None]
                if not found_prices:
                    continue  # Related Skusに無く、実在もしない＝本当に楽天出品が無いだけ

                print(f"    [{row['sku']}] Related Skusに無いが楽天にも出品を発見: {base}")
                current_r = found_prices[0]
                new_price_r, detail_r = calc_sell_price(page, case_id, row["rowIndex"], shop_keyword="楽天",
                                                         expected_purchase_usd=expected_purchase_usd)
                if new_price_r is None:
                    print(f"    [楽天(推測)] {base}: 価格を計算できませんでした（{detail_r}）")
                    log_rows.append([now, case_id, case["caseType"], SHOP_RAKUTEN, "-", base,
                                     f"計算失敗: {detail_r}"])
                    all_ok = False
                    continue

                ok, changed = apply_price(SHOP_RAKUTEN, base, current_r, new_price_r, detail_r,
                                          store_hint="（推測）")
                all_ok = all_ok and ok
                changed_any = changed_any or changed
                if changed and not DRY_RUN:
                    applied_changes.append({"case_id": case_id, "mall": SHOP_RAKUTEN,
                                            "sku": base, "price": new_price_r})
                    recheck_rakuten_price(page, case_id, row["rowIndex"], base, new_price_r, anomalies,
                                          expected_purchase_usd=expected_purchase_usd, shop_keyword="楽天")

                if WOWMA_ENABLED:
                    try:
                        wowma_item = wowma_get_item(base)
                    except Exception as e:
                        print(f"    [Wowma] {base}: 取得エラー: {e}")
                        wowma_item = None
                        all_ok = False
                    if wowma_item is not None:
                        current_w = parse_price(wowma_item.get("itemPrice", "0"))
                        ok, w_changed = apply_price(SHOP_WOWMA, base, current_w, new_price_r, detail_r)
                        all_ok = all_ok and ok
                        changed_any = changed_any or w_changed
                        if w_changed and not DRY_RUN:
                            applied_changes.append({"case_id": case_id, "mall": SHOP_WOWMA,
                                                    "sku": base, "price": new_price_r})

            # 同じケース内で、別の商品（別の楽天SKU）に同じ価格が付いていないか確認する。
            # msy/akc など同一商品のYahooバリエーション同士は同額で正しいので除外し、
            # 「元になった楽天SKUが違うのに同額」だけを異常として扱う。
            if not DRY_RUN:
                this_case_changes = [c for c in applied_changes if c["case_id"] == case_id]
                by_price = {}
                for c in this_case_changes:
                    base = strip_yahoo_suffix(c["sku"]) if c["mall"] == SHOP_YAHOO else c["sku"]
                    by_price.setdefault((c["mall"], c["price"]), set()).add(base.lower())
                for (mall, price), bases in by_price.items():
                    if len(bases) > 1:
                        anomalies.append(
                            f"[{mall}] ケース{case_id}: 別商品なのに同じ価格 ¥{price:,} "
                            f"が付いています（{sorted(bases)}）"
                        )

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

    # 時間差での再確認ジョブ（case_orders_price_verify_delayed.py）に、
    # 今回書き込んだ内容を渡す
    if applied_changes:
        with open(APPLIED_CHANGES_FILE, "w", encoding="utf-8") as f:
            json.dump(applied_changes, f, ensure_ascii=False)
        print(f"変更内容を{APPLIED_CHANGES_FILE}に保存しました（{len(applied_changes)}件）。")

    if anomalies:
        print(f"\n⚠️ 検証で異常を検出しました（{len(anomalies)}件）:")
        for a in anomalies:
            print(f"  {a}")
        body = (
            f"{CW_MENTION_RYO}\n"
            f"[info][title]価格調整の検証で異常を検出（{len(anomalies)}件）[/title]"
            + "\n".join(anomalies)
            + "\n\n価格は自動では戻していません。内容の確認をお願いします。[/info]"
        )
        post_chatwork(body)
    else:
        print("\n検証で異常は見つかりませんでした。")

    print("=== Case Orders 価格調整 完了 ===")


if __name__ == "__main__":
    main()
