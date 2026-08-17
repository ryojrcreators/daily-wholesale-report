import os
from urllib.parse import quote
from playwright.sync_api import sync_playwright

DOMAIN = "app.jrcreators.com"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_URL = f"https://{quote(LOGIN_ID_1, safe='')}:{quote(LOGIN_PASS_1, safe='')}@{DOMAIN}/"
BASE_URL = f"https://{DOMAIN}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

SO_ID = "4812260"
SHIPMENT_ID = "3460604"
TARGET = "Yamato Nekopos"

requests_log = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
    page = context.new_page()

    def on_request(req):
        if req.method == "POST":
            requests_log.append(f"REQUEST POST {req.url} postData={req.post_data}")
    def on_response(res):
        if res.request.method == "POST":
            requests_log.append(f"RESPONSE {res.status} {res.url}")

    page.on("request", on_request)
    page.on("response", on_response)

    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")

    page.goto(f"{BASE_URL}/sales/shipping-details/{SO_ID}", wait_until="networkidle")
    page.wait_for_timeout(1000)

    result = page.evaluate(
        """({shipmentId, target}) => {
            const rows = [...document.querySelectorAll('table tr')];
            for (const tr of rows) {
                const cells = [...tr.querySelectorAll(':scope > td')];
                if (!cells.length) continue;
                const pkgId = cells[0].textContent.trim();
                if (pkgId !== String(shipmentId)) continue;
                const select = tr.querySelector('select');
                if (!select) return 'no-select';
                const opt = [...select.options].find(o => o.textContent.trim() === target);
                if (!opt) return 'no-option';
                select.value = opt.value;
                select.dispatchEvent(new Event('input', {bubbles:true}));
                select.dispatchEvent(new Event('change', {bubbles:true}));
                return 'changed:' + select.value;
            }
            return 'no-row';
        }""",
        {"shipmentId": SHIPMENT_ID, "target": TARGET},
    )
    print(f"select操作結果: {result}")

    requests_log.clear()
    save_btn = page.locator('button:has-text("Save"), input[type="submit"][value="Save"]').first
    save_btn.click()
    page.wait_for_load_state("networkidle")
    print("=== Save時の通信ログ ===")
    for line in requests_log:
        print(line[:500])

    browser.close()
