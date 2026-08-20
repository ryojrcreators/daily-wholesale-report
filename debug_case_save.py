"""
使い捨てデバッグスクリプト。update_case() と同じ手順（select2チップ削除→Reply入力→送信）を
行い、送信直後のページの様子を詳しく出力する（フラッシュメッセージ・エラー要素・
フォームのaction属性・送信ボタンの数など）。ケース155718・155561で「保存されていません」
エラーが再現するため原因調査に使う。ケースのステータス変更は本物だが、テスト用の
Replyメッセージを使うので後で手動で確認・削除してもらう前提。
"""

import os
from playwright.sync_api import sync_playwright

from case_orders_auto_close import BASE_URL, login, CASE_GROUP_RAKUTEN_YAHOO

CASE_ID = os.environ.get("DEBUG_CASE_ID", "155718")
REPLY_TEXT = "[DEBUG TEST] update_case investigation"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        login(page)

        page.goto(f"{BASE_URL}/case-orders/edit/{CASE_ID}", wait_until="networkidle")
        page.wait_for_timeout(300)

        groups = page.evaluate(
            """() => [...document.querySelectorAll('#case-groups-ids option')]
                     .filter(o => o.selected).map(o => ({value: o.value, text: o.textContent.trim()}))"""
        )
        print(f"現在のGroups: {groups}")

        forms_info = page.evaluate(
            """() => [...document.querySelectorAll('form')].map(f => ({
                action: f.action,
                submitButtons: [...f.querySelectorAll('button[type=submit], input[type=submit]')].length,
            }))"""
        )
        print(f"ページ内のform一覧: {forms_info}")

        textareas = page.evaluate(
            """() => [...document.querySelectorAll('textarea')].map(t => t.id)"""
        )
        print(f"textarea一覧: {textareas}")

        removed = page.evaluate(
            """() => {
                const chips = [...document.querySelectorAll('.select2-selection__choice')];
                const target = chips.find(c => c.textContent.includes('Rakuten/Yahoo'));
                if (!target) return {ok: false, reason: 'chip not found'};
                const x = target.querySelector('.select2-selection__choice__remove');
                if (!x) return {ok: false, reason: 'remove button not found'};
                x.click();
                return {ok: true};
            }"""
        )
        print(f"チップ削除結果: {removed}")
        page.wait_for_timeout(500)

        page.fill('textarea[id^="case-order-replies-"][id$="-message"]', REPLY_TEXT)
        print("Reply入力完了")

        submit_count = page.locator('form[action*="/case-orders/edit/"] button[type="submit"]').count()
        print(f"送信ボタンの一致数: {submit_count}")

        validity = page.evaluate(
            """() => {
                const form = document.querySelector('form[action*="/case-orders/edit/"]');
                if (!form) return {found: false};
                const invalid = [...form.querySelectorAll(':invalid')].map(el => ({
                    tag: el.tagName, name: el.name || el.id, validationMessage: el.validationMessage,
                }));
                return {found: true, formValid: form.checkValidity(), invalid};
            }"""
        )
        print(f"送信前のフォーム妥当性チェック: {validity}")

        nav_happened = False
        try:
            with page.expect_navigation(timeout=6000):
                page.click('form[action*="/case-orders/edit/"] button[type="submit"]')
            nav_happened = True
        except Exception as e:
            print(f"expect_navigation がタイムアウト/失敗しました（＝ページ遷移が起きなかった可能性）: {e}")
        print(f"ナビゲーションが発生したか: {nav_happened}")

        print("送信ボタンをクリックしました")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        print(f"送信後のURL: {page.url}")

        flash = page.evaluate(
            """() => {
                const el = document.querySelector('.message, .alert, .flash, .error-message');
                return el ? el.textContent.trim().slice(0, 300) : null;
            }"""
        )
        print(f"フラッシュ/エラー要素: {flash}")

        body_snippet = page.evaluate("() => document.body.innerText.slice(0, 2000)")
        print(f"送信直後の本文冒頭2000文字:\n{body_snippet}")

        page.screenshot(path="debug_after_submit.png", full_page=True)
        print("スクリーンショット保存: debug_after_submit.png")

        # 再読み込みして最終状態を確認
        page.goto(f"{BASE_URL}/case-orders/edit/{CASE_ID}", wait_until="networkidle")
        page.wait_for_timeout(300)
        reloaded_groups = page.evaluate(
            """() => [...document.querySelectorAll('#case-groups-ids option')]
                     .filter(o => o.selected).map(o => o.value)"""
        )
        reloaded_has_reply = REPLY_TEXT in page.evaluate("() => document.body.innerText")
        print(f"再読み込み後のGroups: {reloaded_groups}")
        print(f"再読み込み後にReplyが見つかったか: {reloaded_has_reply}")

        browser.close()


if __name__ == "__main__":
    main()
