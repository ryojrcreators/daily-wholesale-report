"""
ChatworkにShipment ID（Package id）が届いたら、社内システムの
「Edit Shipping Details」からShip Methodを Yamato Nekopos に変更する。

- ログインは他のPlaywright系スクリプトと同じ2段階（Basic認証 + フォームログイン）
- 検索: {BASE_URL}/so-heads?ShippingCodes[id]={shipment_id}
- 検索結果のOrder Numberリンク（/sales/view/...）をクリック
- 「Edit Shipping Details」をクリックし、Package id が一致する行の Ship Method を変更
- Save をクリックして保存
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


def change_ship_method(page, shipment_id):
    """指定Shipment IDのShip MethodをYamato Nekoposに変更する。成功したらTrueを返す。"""
    search_url = f"{BASE_URL}/so-heads?ShippingCodes%5Bid%5D={shipment_id}"
    print(f"検索: {search_url}")
    page.goto(search_url, wait_until="networkidle")

    # 「Order Number」列見出しを探し、その列の最初の行にあるリンクをクリックする
    clicked = page.evaluate(
        """() => {
            const tables = [...document.querySelectorAll('table')];
            for (const t of tables) {
                const rows = [...t.querySelectorAll('tr')];
                if (!rows.length) continue;
                const headerCells = [...rows[0].querySelectorAll('th,td')].map(c => c.textContent.trim());
                const colIdx = headerCells.indexOf('Order Number');
                if (colIdx < 0) continue;
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length <= colIdx) continue;
                    const link = cells[colIdx].querySelector('a');
                    if (link) { link.click(); return true; }
                }
            }
            return false;
        }"""
    )
    if not clicked:
        print(f"！Shipment ID {shipment_id} に対応するOrderが見つかりません")
        debug = page.evaluate(
            """() => {
                const tables = [...document.querySelectorAll('table')];
                return {
                    url: location.href,
                    title: document.title,
                    tableCount: tables.length,
                    tables: tables.map(t => {
                        const rows = [...t.querySelectorAll('tr')];
                        return {
                            rowCount: rows.length,
                            headerCells: rows.length ? [...rows[0].querySelectorAll('th,td')].map(c => c.textContent.trim()) : [],
                            firstDataRowLinks: rows.length > 1 ? [...rows[1].querySelectorAll('a')].map(a => ({text: a.textContent.trim(), href: a.getAttribute('href')})) : [],
                        };
                    }),
                };
            }"""
        )
        print(f"デバッグ情報: {debug}")
        return False, "Order not found"
    page.wait_for_load_state("networkidle")

    print("Edit Shipping Detailsをクリックしています...")
    edit_link = page.locator('a:has-text("Edit Shipping Details"), button:has-text("Edit Shipping Details")').first
    if edit_link.count() == 0:
        print("！Edit Shipping Detailsが見つかりません")
        return False, "Edit Shipping Details link not found"
    edit_link.click()
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1000)

    # Package id が一致する行の Ship Method セレクトを操作
    ok = page.evaluate(
        """({shipmentId, target}) => {
            const rows = [...document.querySelectorAll('table tr')];
            for (const tr of rows) {
                const cells = [...tr.querySelectorAll('td')];
                if (!cells.length) continue;
                const pkgId = cells[0].textContent.trim();
                if (pkgId !== String(shipmentId)) continue;
                const select = tr.querySelector('select');
                if (!select) return false;
                const opt = [...select.options].find(o => o.textContent.trim() === target);
                if (!opt) return false;
                select.value = opt.value;
                select.dispatchEvent(new Event('input', {bubbles:true}));
                select.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }
            return false;
        }""",
        {"shipmentId": shipment_id, "target": TARGET_SHIP_METHOD},
    )
    if not ok:
        print(f"！Package id {shipment_id} の行、またはShip Method欄が見つかりません")
        return False, "Package row or Ship Method select not found"

    print("Ship Methodを変更しました。Saveをクリックします...")
    save_btn = page.locator('button:has-text("Save"), input[type="submit"][value="Save"]').first
    if save_btn.count() == 0:
        print("！Saveボタンが見つかりません")
        return False, "Save button not found"
    save_btn.click()
    page.wait_for_load_state("networkidle")
    print("保存完了")
    return True, ""


def post_chatwork(shipment_id, success, error_reason):
    if success:
        message = f"✅ Shipment {shipment_id}: Ship Method changed to {TARGET_SHIP_METHOD}"
    else:
        message = f"⚠ Shipment {shipment_id}: Ship Method change failed ({error_reason})"
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
