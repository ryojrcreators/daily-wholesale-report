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

SHIPMENT_ID = "3561500"

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

    url = f"{BASE_URL}/shipping-codes/edit/{SHIPMENT_ID}"
    print(f"開きます: {url}")
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(500)

    print(f"最終URL: {page.url}")
    print(f"タイトル: {page.title()}")

    info = page.evaluate(
        """() => {
            const links = [...document.querySelectorAll('a[href*="/sales/view/"]')].map(a => ({
                href: a.getAttribute('href'),
                text: a.textContent.trim(),
            }));
            const heading = document.querySelector('h1, h2, .content h1, .content h2');
            return {
                links,
                heading: heading ? heading.textContent.trim() : null,
                bodySnippet: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 1500),
            };
        }"""
    )
    print(f"見出し: {info['heading']}")
    print(f"/sales/view/ リンク一覧（{len(info['links'])}件）:")
    for l in info["links"]:
        print(f"  - {l['href']}  (text={l['text']!r})")
    print("---本文冒頭1500字---")
    print(info["bodySnippet"])

    page.screenshot(path="debug_shipping_code_edit.png", full_page=True)
    browser.close()
