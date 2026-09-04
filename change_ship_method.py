"""
ChatworkにShipment ID（Package id）が届いたら、社内システムの
「Edit Shipping Details」からShip Methodを Yamato Nekopos に変更する。
ただし、既に Yamato Nekopos だった場合は逆に Sagawa CDS に切り替える（トグル動作）。

処理の流れ:
- ログインは他のPlaywright系スクリプトと同じ2段階（Basic認証 + フォームログイン）
- /shipping-codes/edit/{Shipment ID} を開き、対応注文への /sales/view/{内部ID} リンクから
  内部の注文ID(SO#)を取得する（SoHeadsのShipment ID検索はbotセッションでは一覧が
  描画されないため、この経路で内部IDを得る）
- /sales/shipping-details/{内部ID} を開き、Shipping Country が JP であること・商品明細に
  BLOCKED_PRODUCT_CODE（例: W-229）が含まれていないことを確認してから、Package id が
  一致する行の Ship Method を変更してSave（現在値がYamato Nekoposなら代わりにSagawa CDSへ、
  それ以外ならYamato Nekoposへ。条件を満たさない場合や Block Upgrade チェック済みの場合は
  変更せずエラー報告）
- 完了後、Chatworkルーム(442638900)へ結果を通知
"""

import os
import requests
from playwright.sync_api import sync_playwright
from urllib.parse import quote

DOMAIN = "app.jrcreators.com"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
BASE_URL = f"https://{DOMAIN}"

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "442638900"

TARGET_SHIP_METHOD = "Yamato Nekopos"
ALT_SHIP_METHOD = "Sagawa CDS"  # 既にTARGET_SHIP_METHODだった場合の切り替え先
BLOCKED_PRODUCT_CODE = "W-229"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def login(page):
    print("ログイン中...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


def find_internal_order_id(page, shipment_id):
    """Shipment IDから内部の注文ID(SO#)を取得する。

    /shipping-codes/edit/{Shipment ID} のページに、対応する注文への
    /sales/view/{内部ID} リンクが含まれているので、そこから内部IDを取り出す。
    （HTMLのSO検索一覧はbotセッションでは描画されないため、この経路を使う）
    """
    url = f"{BASE_URL}/shipping-codes/edit/{shipment_id}"
    print(f"内部ID取得のため {url} を開きます")
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(500)

    # 注意: ページ右上の通知ベル（お知らせ）にも無関係な /sales/view/ リンクが
    # 複数含まれているため、単純に最初のリンクを使うと別の注文を誤って
    # 掴んでしまう（実際にこれが原因で誤検出したことがある）。
    # 「SO# 1234567」本体のリンクはリンクテキストが数字のみなので、それで絞り込む。
    href = page.evaluate(
        """() => {
            const links = [...document.querySelectorAll('a[href*="/sales/view/"]')];
            const a = links.find(l => /^\\d+$/.test(l.textContent.trim()));
            return a ? a.getAttribute('href') : null;
        }"""
    )
    if not href:
        info = page.evaluate(
            """() => ({
                url: location.href,
                title: document.title,
                bodySnippet: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 400),
            })"""
        )
        print(f"内部ID未検出のデバッグ: {info}")
        try:
            page.screenshot(path="debug_shipping_code.png", full_page=True)
        except Exception:
            pass
        return None
    tail = href.rstrip("/").split("/")[-1]
    return tail if tail.isdigit() else None


def change_ship_method(page, shipment_id):
    """指定Shipment IDのShip MethodをYamato Nekoposに変更する。成功したらTrueを返す。"""
    # 1) /shipping-codes/edit/{Shipment ID} から内部ID(/sales/view/{id})を取得
    so_id = find_internal_order_id(page, shipment_id)
    if not so_id:
        print("！内部ID(/sales/view/)が見つかりません")
        return False, "Internal order id not found"
    print(f"内部ID = {so_id}")

    # 2) shipping-detailsページを開き、Package idが一致する行のShip Methodを変更
    page.goto(f"{BASE_URL}/sales/shipping-details/{so_id}", wait_until="networkidle")
    page.wait_for_timeout(1000)

    # Shipping Country が JP 以外なら、Yamato Nekopos（国内配送）に変更せずエラー報告する
    shipping_country = page.evaluate(
        """() => {
            const label = [...document.querySelectorAll('label')]
                .find(l => l.textContent.trim().startsWith('Shipping Country'));
            if (!label) return null;
            let input = null;
            const forId = label.getAttribute('for');
            if (forId) input = document.getElementById(forId);
            if (!input) {
                const container = label.closest('div');
                input = container ? container.querySelector('input') : null;
            }
            return input ? input.value.trim() : null;
        }"""
    )
    if shipping_country is None:
        print("！Shipping Country欄が見つかりません。安全のため変更を中止します")
        try:
            page.screenshot(path="debug_shipping.png", full_page=True)
        except Exception:
            pass
        return False, "Shipping Country field not found"
    if shipping_country.upper() != "JP":
        print(f"！Shipping Country={shipping_country!r}（JP以外）のため変更を中止します")
        return False, f"Shipping Country is {shipping_country} (not JP) — Ship Method not changed"
    print(f"Shipping Country={shipping_country!r} を確認。変更を続行します")

    # 対象Package idに紐づく商品明細（Code/Package列を持つ別テーブル）に
    # BLOCKED_PRODUCT_CODE が含まれていたら、Ship Methodは変更せずエラー報告する。
    # 注意: table.rows / tr.cells（テーブルのネイティブAPI）は、そのテーブル・行に
    # 直接属する行/セルだけを返す。querySelectorAll('td')だと、セル内に入れ子テーブル
    # （L/W/H寸法等の折りたたみ表示）がある場合そこのtdまで拾って列がズレるため使わない
    # （実際にこれが原因で誤判定するバグがあった）。
    product_codes = page.evaluate(
        """(shipmentId) => {
            const tables = [...document.querySelectorAll('table')];
            for (const t of tables) {
                const rows = [...t.rows];
                if (!rows.length) continue;
                const headerCells = [...rows[0].cells].map(c => c.textContent.trim());
                const idxCode = headerCells.indexOf('Code');
                const idxPackage = headerCells.indexOf('Package');
                if (idxCode < 0 || idxPackage < 0) continue;
                const codes = [];
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].cells;
                    if (cells.length <= Math.max(idxCode, idxPackage)) continue;
                    if (cells[idxPackage].textContent.trim() !== String(shipmentId)) continue;
                    codes.push(cells[idxCode].textContent.trim());
                }
                return codes;
            }
            return null;
        }""",
        shipment_id,
    )
    if product_codes is None:
        print("！商品明細テーブル（Code/Package列）が見つかりません。安全のため変更を中止します")
        try:
            page.screenshot(path="debug_shipping.png", full_page=True)
        except Exception:
            pass
        return False, "Product code table not found"
    if BLOCKED_PRODUCT_CODE in product_codes:
        print(f"！商品コード {BLOCKED_PRODUCT_CODE} が含まれるため変更を中止します")
        return False, f"Product code {BLOCKED_PRODUCT_CODE} present — Ship Method not changed"
    print(f"商品コード確認OK（{len(product_codes)}件、{BLOCKED_PRODUCT_CODE}なし）。変更を続行します")

    # Package id が一致する行の Ship Method セレクトを操作。
    # 既に TARGET_SHIP_METHOD（Yamato Nekopos）になっている場合は、
    # 逆に ALT_SHIP_METHOD（Sagawa CDS）へ切り替える（トグル動作）。
    # Block Upgrade がチェック済みなら 'block-upgrade'、切り替え先が既に選択済みなら
    # 'already'、変更したら 'changed'、見つからなければ理由を返す。
    result = page.evaluate(
        """({shipmentId, target, altTarget}) => {
            const rows = [...document.querySelectorAll('table tr')];
            for (const tr of rows) {
                const cells = [...tr.cells];
                if (!cells.length) continue;
                const pkgId = cells[0].textContent.trim();
                if (pkgId !== String(shipmentId)) continue;
                const blockCb = tr.querySelector('input[type="checkbox"]');
                if (blockCb && blockCb.checked) return {result: 'block-upgrade'};
                const select = tr.querySelector('select');
                if (!select) return {result: 'no-select'};
                const cur = select.options[select.selectedIndex];
                const curText = cur ? cur.textContent.trim() : '';
                const effectiveTarget = curText === target ? altTarget : target;
                if (curText === effectiveTarget) return {result: 'already', target: effectiveTarget};
                const opt = [...select.options].find(o => o.textContent.trim() === effectiveTarget);
                if (!opt) return {result: 'no-option', target: effectiveTarget};
                select.value = opt.value;
                select.dispatchEvent(new Event('input', {bubbles:true}));
                select.dispatchEvent(new Event('change', {bubbles:true}));
                return {result: 'changed', target: effectiveTarget};
            }
            return {result: 'no-row'};
        }""",
        {"shipmentId": shipment_id, "target": TARGET_SHIP_METHOD, "altTarget": ALT_SHIP_METHOD},
    )
    outcome = result["result"]
    applied_target = result.get("target")
    if outcome == "already":
        print(f"既に {applied_target} のため変更不要")
        return True, f"already {applied_target}"
    if outcome == "block-upgrade":
        print("！Block Upgrade がチェック済みのため変更を中止します")
        return False, "Block Upgrade is checked — Ship Method not changed"
    if outcome != "changed":
        print(f"！変更できませんでした（{outcome}）: Package id {shipment_id}")
        try:
            page.screenshot(path="debug_shipping.png", full_page=True)
        except Exception:
            pass
        return False, f"Ship method not changed ({outcome})"

    print(f"Ship Methodを {applied_target} に変更しました。Saveをクリックします...")
    save_btn = page.locator('button:has-text("Save"), input[type="submit"][value="Save"]').first
    if save_btn.count() == 0:
        print("！Saveボタンが見つかりません")
        return False, "Save button not found"
    save_btn.click()
    page.wait_for_load_state("networkidle")
    print("保存完了")
    return True, f"changed to {applied_target}"


def post_chatwork(shipment_id, success, detail):
    if success:
        message = f"✅ Shipment {shipment_id}: Ship Method {detail}"
    else:
        message = f"⚠ Shipment {shipment_id}: Ship Method change failed ({detail})"
    resp = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data={"body": message},
    )
    print(f"Chatwork通知送信: status={resp.status_code}")


def main():
    shipment_id = os.environ.get("SHIPMENT_ID", "").strip()
    if not shipment_id.isdigit():
        raise SystemExit(f"SHIPMENT_ID が不正です: {shipment_id!r}（数字を指定してください）")
    print(f"=== Ship Method変更: Shipment ID {shipment_id} ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        login(page)
        success, error_reason = change_ship_method(page, shipment_id)
        browser.close()

    post_chatwork(shipment_id, success, error_reason)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
