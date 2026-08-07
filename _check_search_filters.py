"""
一時調査用（読み取りのみ）：so-heads検索でどのフィルターがbotセッションで機能するかを
切り分ける。各ケースごとに新しいブラウザでログインし直す（セッション使い回しの影響を排除）。

1. フォーム上に実際に存在する入力/セレクト項目を一覧化する
2. start_date/end_date（フォーム入力・実績あり）のみ→件数
3. ship_dateだけをURLに付けた場合（sales_account_idなし）→件数
4. sales_account_idだけをURLに付けた場合（ship_dateなし）→件数
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from playwright.sync_api import sync_playwright

APP_DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{APP_DOMAIN}/"
SO_HEADS_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{APP_DOMAIN}/so-heads"
JST = timezone(timedelta(hours=9))

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def new_page(p):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1800, "height": 900},
        device_scale_factor=2,
        user_agent=USER_AGENT,
    )
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    return browser, page


def dump_result(label, page):
    info = page.evaluate(
        """() => {
            const bodyText = document.body.innerText;
            const m = bodyText.match(/Showing[^\\n]*/);
            const tables = [...document.querySelectorAll('table')];
            return {
                showing: m ? m[0] : null,
                tableRowCounts: tables.map(t => t.querySelectorAll('tbody tr').length),
                hasSearchForm: bodyText.includes('Start Date') && bodyText.includes('End Date'),
            };
        }"""
    )
    print(f"  [{label}] Showing={info['showing']} / table行数={info['tableRowCounts']} / "
          f"検索フォーム表示中={info['hasSearchForm']}")


today = datetime.now(JST).date()
start_str = (today - timedelta(days=14)).strftime("%Y-%m-%d")
end_str = today.strftime("%Y-%m-%d")

with sync_playwright() as p:
    # 1. フォームの入力/セレクト項目を一覧化
    browser, page = new_page(p)
    try:
        page.goto(SO_HEADS_URL, wait_until="networkidle")
        page.wait_for_timeout(1500)
        fields = page.evaluate(
            """() => {
                const inputs = [...document.querySelectorAll('input')].map(el =>
                    ({tag: 'input', type: el.type, name: el.name, id: el.id, placeholder: el.placeholder}));
                const selects = [...document.querySelectorAll('select')].map(el =>
                    ({tag: 'select', name: el.name, id: el.id,
                      options: [...el.options].map(o => `${o.value}:${o.textContent.trim()}`).slice(0, 15)}));
                return { inputs, selects };
            }"""
        )
        print("=== 1. フォーム項目一覧 ===")
        print("  inputs:")
        for f in fields["inputs"]:
            print(f"    {f}")
        print("  selects:")
        for f in fields["selects"]:
            print(f"    {f}")
    finally:
        browser.close()

    # 2. start_date/end_date フォーム入力のみ（ベースライン、既に実績確認済みだが再掲）
    browser, page = new_page(p)
    try:
        page.goto(SO_HEADS_URL, wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.locator('input[name="start_date"], input[placeholder*="Start"], input[id*="start"]').first.fill(start_str)
        page.locator('input[name="end_date"], input[placeholder*="End"], input[id*="end"]').first.fill(end_str)
        page.click('button:has-text("Search"), input[value="Search"]')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        print("\n=== 2. start_date/end_dateのみ（フォーム経由） ===")
        dump_result("baseline", page)
    finally:
        browser.close()

    # 3. ship_dateだけ（URL直打ち、sales_account_idなし）
    browser, page = new_page(p)
    try:
        url = f"{SO_HEADS_URL}?start_date=2026-04-30&ship_date={end_str}"
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        print("\n=== 3. ship_dateのみ（URL、sales_account_idなし） ===")
        dump_result("ship_date only", page)
    finally:
        browser.close()

    # 4. sales_account_idだけ（URL直打ち、ship_dateなし）
    browser, page = new_page(p)
    try:
        url = f"{SO_HEADS_URL}?start_date=2026-04-30&SoHeads%5Bsales_account_id%5D=3"
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        print("\n=== 4. sales_account_idのみ（URL、ship_dateなし） ===")
        dump_result("sales_account_id only", page)
    finally:
        browser.close()
