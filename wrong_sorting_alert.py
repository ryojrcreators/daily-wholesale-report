"""
社内システム（app.jrcreators.com）の Credits 一覧を巡回し、
Reason が「Wrong Sorting」の新規クレジットを見つけたら Chatwork に通知する。

処理の流れ:
  1. Playwrightで app.jrcreators.com にログイン（Basic認証 + フォームログインの2段階）
  2. /credits を新しい順（デフォルト表示）にページ送りしながら読み取り、
     スプレッドシートに保存済みの「最後に確認したCredit ID」より新しい行だけを対象にする
  3. 対象行のうち Reason が「Wrong Sorting」のものを Chatwork に通知する
  4. 今回スキャンした中で最大のCredit IDをスプレッドシートに保存する
     （Wrong Sorting以外の行も含めて更新するので、次回は毎回全件を読み直さずに済む）

初回実行時（保存済みIDが無い時）は、既存の履歴を通知せず「現在の最新ID」を
基準として保存するだけにする（過去分を一気に通知しないため）。
"""

import os
import sys
import json
from urllib.parse import quote

import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# ── 社内システム ──────────────────────────────────
DOMAIN = "app.jrcreators.com"
BASE_URL = f"https://{DOMAIN}"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_URL = f"https://{quote(LOGIN_ID_1, safe='')}:{quote(LOGIN_PASS_1, safe='')}@{DOMAIN}/"

CREDITS_URL = f"{BASE_URL}/credits"
TARGET_REASON = "Wrong Sorting"
MAX_PAGES = 50  # 異常時に無限ページ送りしないための上限（1ページ20件 → 最大1000件分）

MENTIONS = (
    "[To:7610335]Saori Otaniさん\n"
    "[To:7579500]Yuka Tsuzukiさん\n"
    "[To:7604400]Mayu Taniguchiさん\n"
    "[To:6958569]Fujika Tanahashiさん\n"
)

# ── Chatwork ──────────────────────────────────────
CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "83994351"  # Wrong Sorting 通知専用ルーム

# ── スプレッドシート ──────────────────────────────
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
STATE_SHEET_NAME = "WrongSorting_State"


# ══ スプレッドシート ══════════════════════════════
def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def load_last_seen_id(spreadsheet):
    try:
        ws = spreadsheet.worksheet(STATE_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        return None, None
    for row in ws.get_all_values():
        if row and row[0] == "last_seen_id":
            return (int(row[1]) if row[1] else None), ws
    return None, ws


def save_last_seen_id(spreadsheet, ws, new_id: int):
    if ws is None:
        ws = spreadsheet.add_worksheet(title=STATE_SHEET_NAME, rows=10, cols=2)
    values = ws.get_all_values()
    for i, row in enumerate(values, start=1):
        if row and row[0] == "last_seen_id":
            ws.update(range_name=f"A{i}:B{i}", values=[["last_seen_id", str(new_id)]])
            return
    ws.append_row(["last_seen_id", str(new_id)])


# ══ Chatwork ══════════════════════════════════════
def post_chatwork(body: str):
    resp = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data={"body": body},
        timeout=30,
    )
    print(f"Chatwork通知送信: status={resp.status_code}")
    if resp.status_code >= 400:
        print(f"  応答: {resp.text[:200]}")


# ══ 社内システム（Playwright） ════════════════════
def login(page):
    print("社内システムにログイン中...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


def fetch_credits_page(page, page_no: int) -> list:
    """1ページ分のCredits一覧を読み取る。戻り値は [{id, soHead, reason, creator, createdTime}]"""
    url = CREDITS_URL if page_no == 1 else f"{CREDITS_URL}?page={page_no}"
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(300)

    info = page.evaluate(
        """() => {
            const tables = [...document.querySelectorAll('table')];
            const target = tables.find(t => {
                const hs = [...t.querySelectorAll('th')].map(th => th.textContent.trim());
                return hs.includes('Reason') && hs.includes('Created Time');
            });
            if (!target) return { rows: null };
            const headers = [...target.querySelectorAll('th')].map(th => th.textContent.trim());
            const idxId = headers.indexOf('Id');
            const idxSoHead = headers.indexOf('So Head');
            const idxReason = headers.indexOf('Reason');
            const idxCreator = headers.indexOf('Creator');
            const idxTime = headers.indexOf('Created Time');
            const rows = [...target.querySelectorAll('tbody tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                return {
                    id: (tds[idxId] || '').replace(/,/g, ''),
                    soHead: tds[idxSoHead] || '',
                    reason: tds[idxReason] || '',
                    creator: tds[idxCreator] || '',
                    createdTime: tds[idxTime] || '',
                };
            });
            return { rows };
        }"""
    )
    rows = info.get("rows") or []
    return [r for r in rows if r["id"].isdigit()]


# ══ メイン ════════════════════════════════════════
def main():
    print("=== Wrong Sorting 通知チェック 開始 ===")
    spreadsheet = get_spreadsheet()
    last_seen_id, state_ws = load_last_seen_id(spreadsheet)
    first_run = last_seen_id is None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        try:
            login(page)
        except Exception as e:
            print(f"ログインに失敗しました: {e}")
            browser.close()
            sys.exit(1)

        if first_run:
            # 初回実行：最新ページの最大IDだけを基準として記録し、既存の履歴は通知しない
            rows = fetch_credits_page(page, 1)
            browser.close()
            if not rows:
                print("Credits一覧が空でした。基準IDを保存できません。")
                sys.exit(1)
            max_id_seen = max(int(r["id"]) for r in rows)
            save_last_seen_id(spreadsheet, state_ws, max_id_seen)
            print(f"初回実行のため通知は行わず、基準ID={max_id_seen} を保存しました。")
            print("=== 完了 ===")
            return

        new_rows = []
        max_id_seen = last_seen_id

        for page_no in range(1, MAX_PAGES + 1):
            try:
                rows = fetch_credits_page(page, page_no)
            except Exception as e:
                print(f"{page_no}ページ目の取得に失敗しました: {e}")
                break

            if not rows:
                print(f"{page_no}ページ目に行がありません。終了します。")
                break

            print(f"{page_no}ページ目: {len(rows)}件（先頭ID={rows[0]['id']} / 末尾ID={rows[-1]['id']}）")

            reached_known = False
            for r in rows:
                rid = int(r["id"])
                if rid <= last_seen_id:
                    reached_known = True
                    continue
                max_id_seen = max(max_id_seen, rid)
                if r["reason"] == TARGET_REASON:
                    new_rows.append(r)

            if reached_known:
                break

        browser.close()

    print(f"新規のWrong Sorting: {len(new_rows)}件")
    for r in sorted(new_rows, key=lambda r: int(r["id"])):
        body = (
            MENTIONS +
            "[info][title]Wrong Sorting クレジットが作成されました[/title]"
            f"Credit ID: {r['id']}\n"
            f"So Head: {r['soHead']}\n"
            f"Creator: {r['creator']}\n"
            f"Created: {r['createdTime']}\n"
            f"{BASE_URL}/credits/view/{r['id']}[/info]"
        )
        post_chatwork(body)

    if max_id_seen > (last_seen_id or 0):
        save_last_seen_id(spreadsheet, state_ws, max_id_seen)
        print(f"基準IDを {last_seen_id} → {max_id_seen} に更新しました。")

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
