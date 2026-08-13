"""
使い捨てデバッグスクリプト。指定ケースのDescription欄と、Calc計算ツールを開いた際の
Purchase Price欄の中身（HTML構造も含む）をそのまま出力する。

Change Priceケースで、社内ツールの計算が古い仕入価格を使ってしまい、
Descriptionに書かれた新しい仕入価格が反映されていない問題を調査するために使う。
価格は一切変更しない（閲覧のみ）。
"""

import os
from playwright.sync_api import sync_playwright

from case_orders_auto_close import BASE_URL, login

CASE_ID = os.environ.get("DEBUG_CASE_ID", "155222")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        login(page)

        page.goto(f"{BASE_URL}/case-orders/view/{CASE_ID}", wait_until="networkidle")
        page.wait_for_timeout(500)

        print("==== ページ本文（Description周辺を探す） ====")
        body_text = page.evaluate("() => document.body.innerText")
        idx = body_text.find("Description")
        print(body_text[max(0, idx - 100): idx + 600] if idx >= 0 else "「Description」という文字列が見つかりません")

        print("\n==== Descriptionを含む要素のHTML ====")
        html = page.evaluate(
            """() => {
                const els = [...document.querySelectorAll('*')].filter(
                    el => el.children.length === 0 && el.textContent.trim() === 'Description'
                );
                if (els.length === 0) return 'label要素が見つかりません';
                const label = els[0];
                const container = label.closest('tr') || label.closest('div') || label.parentElement;
                return container ? container.outerHTML.slice(0, 1500) : 'container無し';
            }"""
        )
        print(html)

        print("\n==== Calc計算ツールを開いてPurchase Price欄を確認 ====")
        context2 = context
        before = set(context2.pages)
        try:
            with context2.expect_page(timeout=8000) as popup_info:
                page.evaluate(
                    """() => {
                        const table = [...document.querySelectorAll('table')].find(t => {
                            const hs = [...t.querySelectorAll('th')].map(th => th.textContent.trim());
                            return hs.includes('Sku') && hs.includes('Shop');
                        });
                        const row = table.querySelectorAll('tbody tr')[0];
                        const link = [...row.querySelectorAll('a')].find(a => a.textContent.trim() === 'Calc');
                        link.click();
                    }"""
                )
            calc_page = popup_info.value
            calc_page.wait_for_load_state("networkidle")
            calc_page.wait_for_timeout(500)
            print("calc page url:", calc_page.url)
            calc_html = calc_page.evaluate(
                """() => {
                    const inputs = [...document.querySelectorAll('input, select')];
                    return inputs.map(i => `${i.name || i.id || '(no name)'} = ${i.value}`).join('\\n');
                }"""
            )
            print(calc_html)
        except Exception as e:
            print("計算ツールを開けませんでした:", e)

        browser.close()


if __name__ == "__main__":
    main()
