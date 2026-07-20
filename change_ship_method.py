"""
ChatworkにShipment ID（Package id）が届いたら、社内システムの
「Edit Shipping Details」からShip Methodを Yamato Nekopos に変更する。

処理の流れ:
- ログインは他のPlaywright系スクリプトと同じ2段階（Basic認証 + フォームログイン）
- /shipping-codes/edit/{Shipment ID} を開き、対応注文への /sales/view/{内部ID} リンクから
  内部の注文ID(SO#)を取得する（SoHeadsのShipment ID検索はbotセッションでは一覧が
  描画されないため、この経路で内部IDを得る）
- /sales/shipping-details/{内部ID} を開き、Package id が一致する行の Ship Method を変更してSave
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

    href = page.evaluate(
        """() => {
            const a = document.querySelector('a[href*="/sales/view/"]');
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

    # Package id が一致する行の Ship Method セレクトを操作。
    # 既に目的の値なら 'already'、変更したら 'changed'、見つからなければ理由を返す。
    result = page.evaluate(
        """({shipmentId, target}) => {
            const rows = [...document.querySelectorAll('table tr')];
            for (const tr of rows) {
                const cells = [...tr.querySelectorAll('td')];
                if (!cells.length) continue;
                const pkgId = cells[0].textContent.trim();
                if (pkgId !== String(shipmentId)) continue;
                const select = tr.querySelector('select');
                if (!select) return 'no-select';
                const cur = select.options[select.selectedIndex];
                if (cur && cur.textContent.trim() === target) return 'already';
                const opt = [...select.options].find(o => o.textContent.trim() === target);
                if (!opt) return 'no-option';
                select.value = opt.value;
                select.dispatchEvent(new Event('input', {bubbles:true}));
                select.dispatchEvent(new Event('change', {bubbles:true}));
                return 'changed';
            }
            return 'no-row';
        }""",
        {"shipmentId": shipment_id, "target": TARGET_SHIP_METHOD},
    )
    if result == "already":
        print("既に Yamato Nekopos のため変更不要")
        return True, "already Yamato Nekopos"
    if result != "changed":
        print(f"！変更できませんでした（{result}）: Package id {shipment_id}")
        try:
            page.screenshot(path="debug_shipping.png", full_page=True)
        except Exception:
            pass
        return False, f"Ship method not changed ({result})"

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
