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


def fetch_order_via_csv(cookie_dict, shipment_id):
    """Shipment IDでSO CSVを取得し、該当注文の情報dictを返す。

    HTML検索ではbotセッションで0件になるが、CSVダウンロードならフィルタが効くため
    こちらで order_number / created_time / 現在の ship_method を取得する。
    """
    url = f"{BASE_URL}/sales/download?ShippingCodes%5Bid%5D={shipment_id}"
    r = requests.get(
        url,
        cookies=cookie_dict,
        headers={"User-Agent": USER_AGENT},
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
    )
    if r.status_code != 200:
        print(f"CSVダウンロード失敗: status={r.status_code}")
        return None
    rows = list(csv.reader(r.content.decode("utf-8-sig", errors="replace").splitlines()))
    if len(rows) < 2:
        print("CSVに該当データがありません（0件）")
        return None
    return dict(zip(rows[0], rows[1]))


def find_internal_order_id(page, order_number, created_time):
    """created日で日付検索し、order_numberが一致する行の /sales/view/ から内部IDを返す。

    Shipment IDフィルタのHTML検索はbotでは0件になるが、日付検索なら結果が描画される
    （so_sheets.py で実証済み）ため、created日の前後だけを狭く検索して該当行を探す。
    """
    try:
        d = datetime.strptime(created_time.split(",")[0].strip(), "%m/%d/%y").date()
    except Exception:
        d = date.today()
    start = (d - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (d + timedelta(days=3)).strftime("%Y-%m-%d")
    print(f"日付 {start}〜{end} で検索し、注文 {order_number} を探します")

    page.goto(f"{BASE_URL}/so-heads", wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.locator('input[name="start_date"]').first.fill(start)
    page.locator('input[name="end_date"]').first.fill(end)

    # Line Status=Hold などが既定フィルタで一覧から除外される可能性があるため、
    # SO/Line の全ステータスを明示的に選択して、Hold注文も結果に含める。
    page.evaluate(
        """() => {
            // SO Status（複数選択）を全選択
            const cb = document.querySelector('#so-heads-select-all');
            if (cb) {
                cb.checked = true;
                const container = cb.closest('.input.checkbox');
                const sel = container && container.previousElementSibling
                    ? container.previousElementSibling.querySelector('select') : null;
                if (sel) {
                    [...sel.options].forEach(o => o.selected = true);
                    sel.dispatchEvent(new Event('change', {bubbles:true}));
                }
            }
            // Line Status（単一選択）は空＝全件のままにする
        }"""
    )

    page.click('button:has-text("Search")')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)

    href = page.evaluate(
        """(orderNum) => {
            const links = [...document.querySelectorAll('a[href*="/sales/view/"]')];
            for (const a of links) {
                if (a.textContent.trim() === orderNum) return a.getAttribute('href');
            }
            return null;
        }""",
        order_number,
    )
    if not href:
        info = page.evaluate(
            """(orderNum) => {
                const links = [...document.querySelectorAll('a[href*="/sales/view/"]')];
                const resultDiv = document.querySelector('#resultdiv');
                return {
                    url: location.href,
                    resultDivExists: !!resultDiv,
                    viewLinkCount: links.length,
                    sampleOrderNumbers: links.slice(0, 15).map(a => a.textContent.trim()),
                    targetInHtml: document.documentElement.outerHTML.includes(orderNum),
                };
            }""",
            order_number,
        )
        print(f"内部ID未検出のデバッグ: {info}")
        try:
            page.screenshot(path="debug_soheads.png", full_page=True)
        except Exception:
            pass
        return None
    tail = href.rstrip("/").split("/")[-1]
    return tail if tail.isdigit() else None


def change_ship_method(page, shipment_id):
    """指定Shipment IDのShip MethodをYamato Nekoposに変更する。成功したらTrueを返す。"""
    # 1) Shipment IDでCSVを取得（HTML検索は0件でもCSVは効く）
    cookie_dict = {c["name"]: c["value"] for c in page.context.cookies()}
    rec = fetch_order_via_csv(cookie_dict, shipment_id)
    if not rec:
        return False, "Order not found (CSV empty)"
    order_number = (rec.get("order_number") or "").strip()
    created_time = (rec.get("created_time") or "").strip()
    current_method = (rec.get("ship_method") or "").strip()
    print(f"CSV取得: order_number={order_number!r} created={created_time!r} 現ship_method={current_method!r}")
    if not order_number:
        return False, "order_number missing in CSV"
    if current_method == TARGET_SHIP_METHOD:
        print("既に Yamato Nekopos のため変更不要")
        return True, "already Yamato Nekopos"

    # 2) created日で日付検索して内部ID(/sales/view/{id})を取得
    so_id = find_internal_order_id(page, order_number, created_time)
    if not so_id:
        print("！内部ID(/sales/view/)が見つかりません")
        try:
            page.screenshot(path="debug_soheads.png", full_page=True)
        except Exception:
            pass
        return False, "Internal order id not found"
    print(f"内部ID = {so_id}")

    # 3) shipping-detailsページを開き、Package idが一致する行のShip Methodを変更
    page.goto(f"{BASE_URL}/sales/shipping-details/{so_id}", wait_until="networkidle")
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
        try:
            page.screenshot(path="debug_shipping.png", full_page=True)
        except Exception:
            pass
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


def main():
    shipment_id = os.environ.get("SHIPMENT_ID", "").strip()
    if not shipment_id.isdigit():
        raise SystemExit(f"SHIPMENT_ID が不正です: {shipment_id!r}（数字を指定してください）")
    print(f"=== Ship Method変更: Shipment ID {shipment_id} ===")

    diagnose = os.environ.get("DIAGNOSE", "").strip() == "1"

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
        success, error_reason = change_ship_method(page, shipment_id)
        browser.close()

    post_chatwork(shipment_id, success, error_reason)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
