"""一時的な調査用スクリプト（原因究明後に削除）"""
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

SO_ID = "4812260"  # 3460604 の内部ID
SHIPMENT_ID = "3460604"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
    page = context.new_page()

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

    info = page.evaluate(
        """(shipmentId) => {
            const tables = [...document.querySelectorAll('table')];
            return tables.map((t, i) => {
                const rows = [...t.querySelectorAll('tr')];
                const headerCells = rows.length ? [...rows[0].querySelectorAll('th,td')].map(c => c.textContent.trim()) : [];
                const dataRows = rows.slice(1, 4).map(tr =>
                    [...tr.querySelectorAll('td')].map(c => c.textContent.trim())
                );
                return { tableIndex: i, rowCount: rows.length, headerCells, sampleDataRows: dataRows };
            });
        }""",
        SHIPMENT_ID,
    )
    for t in info:
        print(t)

    browser.close()
