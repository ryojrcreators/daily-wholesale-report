"""
ChatworkにShipment ID（Package id）が届いたら、社内システムの
「Edit Shipping Details」からShip Methodを Yamato Nekopos に変更する。

処理の流れ（HTMLのShipment ID検索はbotセッションでは0件になるため、CSVを併用する）:
- ログインは他のPlaywright系スクリプトと同じ2段階（Basic認証 + フォームログイン）
- Shipment IDでCSV（/sales/download?ShippingCodes[id]=...）を取得し、order_number・created日・
  現在のship_methodを得る（CSVならフィルタが効く）
- created日で日付検索（日付検索なら結果が描画される）し、order_numberが一致する行の
  /sales/view/{内部ID} から内部IDを取得
- /sales/shipping-details/{内部ID} を開き、Package id が一致する行の Ship Method を変更してSave
- 完了後、Chatworkルーム(442638900)へ結果を通知
"""

import os
import csv
import requests
from datetime import date, datetime, timedelta
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
# so_sheets.py と同様、SO検索ページは Basic認証をURLに埋め込んでアクセスする
SO_SEARCH_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/so-heads"

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


def diagnose_csv(cookie_dict, shipment_id):
    """botセッションでSO CSVを直接ダウンロードし、列構成と該当行を調べる（診断用）。"""
    import csv

    today = date.today()
    start = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    end = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    auth = (LOGIN_ID_1, LOGIN_PASS_1)
    headers = {"User-Agent": USER_AGENT}

    def fetch(url):
        r = requests.get(url, cookies=cookie_dict, headers=headers, auth=auth)
        text = r.content.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(text.splitlines()))
        return r.status_code, len(r.content), rows

    # A) Shipment IDフィルタ付きダウンロード
    urlA = f"{BASE_URL}/sales/download?ShippingCodes%5Bid%5D={shipment_id}"
    print(f"\n[A] Shipment IDフィルタ付き: {urlA}")
    sa, ba, rowsA = fetch(urlA)
    print(f"[A] status={sa} bytes={ba} rows={len(rowsA)}")
    if rowsA:
        print(f"[A] header={rowsA[0]}")
        for r in rowsA[1:5]:
            print(f"[A] row={r}")

    # B) 日付範囲のみ（so_sheets方式・確実に動く）
    urlB = f"{BASE_URL}/sales/download?start_date={start}&end_date={end}"
    print(f"\n[B] 日付範囲のみ: {urlB}")
    sb, bb, rowsB = fetch(urlB)
    print(f"[B] status={sb} bytes={bb} rows={len(rowsB)}")
    if rowsB:
        print(f"[B] header={rowsB[0]}")
        matches = 0
        for i, r in enumerate(rowsB[1:], start=1):
            joined = "\t".join(r)
            if shipment_id in joined or "4938929" in joined:
                print(f"[B] MATCH 行{i}: {r}")
                matches += 1
                if matches >= 5:
                    break
        if matches == 0:
            print(f"[B] {shipment_id} も 4938929 も含む行は見つかりませんでした")


def probe_endpoints(page, shipment_id):
    """shipping_code_id(=Shipment ID)を使って、直接アクセスできる編集/詳細ページを探す。"""
    candidates = [
        f"{BASE_URL}/shipping-codes/view/{shipment_id}",
        f"{BASE_URL}/shipping-codes/edit/{shipment_id}",
        f"{BASE_URL}/shipping-codes/{shipment_id}",
        f"{BASE_URL}/sales/shipping-details/{shipment_id}",
    ]
    for url in candidates:
        try:
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(800)
        except Exception as e:
            print(f"[probe] {url}\n  → 例外: {e}")
            continue
        info = page.evaluate(
            """() => {
                const hasYamato = [...document.querySelectorAll('select')].some(s =>
                    [...s.options].some(o => o.textContent.trim() === 'Yamato Nekopos'));
                const uniq = arr => [...new Set(arr)].slice(0, 5);
                return {
                    finalUrl: location.href,
                    title: document.title,
                    hasYamatoSelect: hasYamato,
                    salesViewLinks: uniq([...document.querySelectorAll('a[href*="/sales/view/"]')].map(a => a.getAttribute('href'))),
                    shipDetailLinks: uniq([...document.querySelectorAll('a[href*="/sales/shipping-details/"]')].map(a => a.getAttribute('href'))),
                    bodyLen: document.body.innerText.length,
                    bodySnippet: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 400),
                };
            }"""
        )
        print(f"[probe] {url}\n  → {info}")


def main():
    shipment_id = os.environ.get("SHIPMENT_ID", "").strip()
    if not shipment_id.isdigit():
        raise SystemExit(f"SHIPMENT_ID が不正です: {shipment_id!r}（数字を指定してください）")
    print(f"=== Ship Method変更: Shipment ID {shipment_id} ===")

    diagnose = os.environ.get("DIAGNOSE", "").strip() == "1"
    probe = os.environ.get("PROBE", "").strip() == "1"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        login(page)
        if diagnose:
            cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
            browser.close()
            diagnose_csv(cookie_dict, shipment_id)
            print("=== 診断完了 ===")
            return
        if probe:
            probe_endpoints(page, shipment_id)
            browser.close()
            print("=== プローブ完了 ===")
            return
        success, error_reason = change_ship_method(page, shipment_id)
        browser.close()

    post_chatwork(shipment_id, success, error_reason)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
