"""
【使い捨てスクリプト】Report Delay で誤って In-Progress にしたケースを New に戻す。

revert_case_ids.txt に列挙した Case ID を順に開き、ステータスを New（case-status-id="1"）
に変更して保存する。report_delay.py と同じログイン方式・同じ編集ページ(cs-edit)を使う。
実行後、このスクリプト・ワークフロー・IDリストは削除してよい。
"""

import os
import time
from playwright.sync_api import sync_playwright
from urllib.parse import quote

DOMAIN       = os.environ["APP_DOMAIN"]
LOGIN_ID_1   = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2   = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]

LOGIN_ID_1_ENC   = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
BASE_URL  = f"https://{DOMAIN}"

STATUS_NEW = "1"
ID_FILE = "revert_case_ids.txt"


def load_ids():
    with open(ID_FILE, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def main():
    ids = load_ids()
    print(f"対象 Case ID: {len(ids)}件 を New に戻します")

    ok = 0
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # ===== ログイン（report_delay.py と同じ手順）=====
        print("Basic認証付きでトップページを開いています...")
        page.goto(LOGIN_URL, wait_until="networkidle")
        print("Loginボタンをクリックしています...")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")
        print("フォームログインを処理しています...")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")
        print("ログイン完了")

        for i, cid in enumerate(ids, 1):
            try:
                page.goto(f"{BASE_URL}/case-orders/cs-edit/{cid}", wait_until="networkidle")
                sel = page.query_selector('#case-status-id')
                if sel is None:
                    print(f"  [{i}/{len(ids)}] Case {cid}: ステータス欄が見つからない → スキップ")
                    failed.append(cid)
                    continue
                page.select_option('#case-status-id', STATUS_NEW)
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")
                ok += 1
                if i % 20 == 0 or i == len(ids):
                    print(f"  [{i}/{len(ids)}] 完了（成功 {ok} / 失敗 {len(failed)}）")
                time.sleep(0.3)
            except Exception as e:
                print(f"  [{i}/{len(ids)}] Case {cid}: エラー {e}")
                failed.append(cid)

        browser.close()

    print(f"\n{'='*40}")
    print(f"New に戻した件数: {ok}件 / 失敗: {len(failed)}件")
    if failed:
        print("失敗した Case ID:")
        print("  " + ", ".join(failed))
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
