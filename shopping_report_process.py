"""
Chatworkのショッピングレポートを読み取り、対象POに対して
  - [Not Bought] (0/x)  … その行を Close
  - [Not Bought] (n/x)  … その行の Qty を n に変更
  - [Got Extra]  +k      … その行の Qty を 現在+k に変更
を行う。

PO番号は、ショッピングリスト用スプレッドシートの指定タブ D1 から読む。
レポートは Chatwork API で対象ルームの最新「[End Shopping Report]」を取得する。

DRY_RUN=true（既定）では、何をするかを一覧表示するだけで実際の変更は行わない。
DRY_RUN=false で実行する。Force Close は一切使わない。
"""

import os
import re
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from urllib.parse import quote

# ===== 設定 =====
DOMAIN = "app.jrcreators.com"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
BASE_URL = f"https://{DOMAIN}"

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SHOPPING_SPREADSHEET_ID = "1L2IKiEjimmXkXfSIt6xT8fbwWjkraDWOM-T62brYVdo"

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "296236026"  # ショッピングレポートのルーム

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
SHEET_NAME = os.environ.get("SHEET_NAME", "").strip()  # PO#(D1)を読むタブ。空なら先頭タブ

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


# ---------- PO番号（シートのD1）----------
def get_po_number_from_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SHOPPING_SPREADSHEET_ID)
    ws = ss.worksheet(SHEET_NAME) if SHEET_NAME else ss.sheet1
    po = str(ws.acell("D1").value or "").strip()
    print(f"タブ [{ws.title}] の D1 から PO# = {po!r}")
    return po


# ---------- Chatworkレポート取得 ----------
def fetch_report():
    r = requests.get(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages?force=1",
        headers={"X-ChatWorkToken": CW_TOKEN},
    )
    if r.status_code == 204:
        print("Chatwork: 新着メッセージなし（204）")
        return None
    if r.status_code != 200:
        raise Exception(f"Chatwork取得失敗: status={r.status_code} {r.text[:200]}")
    messages = r.json()
    # 「[End Shopping Report]」を含む最新メッセージ（末尾側が新しい）
    for m in reversed(messages):
        if "[End Shopping Report]" in m.get("body", ""):
            return m["body"]
    print("Chatwork: [End Shopping Report] が見つかりません")
    return None


# ---------- レポート解析 ----------
def parse_report(text):
    """(not_bought[(code,bought,needed)], extras[(code,extra)]) を返す。"""
    not_bought = []
    extras = []
    section = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("[not bought"):
            section = "nb"
            continue
        if low.startswith("[got extra"):
            section = "ex"
            continue
        if s.startswith("["):  # 他のセクション見出し
            section = None
            continue
        if section == "nb":
            m = re.match(r"^(\S+)\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)", s)
            if m:
                not_bought.append((m.group(1), int(m.group(2)), int(m.group(3))))
        elif section == "ex":
            m = re.match(r"^(\S+)\s*\+\s*(\d+)\s*extra", s, re.IGNORECASE)
            if m:
                extras.append((m.group(1), int(m.group(2))))
    return not_bought, extras


# ---------- ログイン ----------
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


# ---------- 編集画面から明細（index, code, qty, disabled）を読む ----------
def read_po_lines(page, po_number):
    page.goto(f"{BASE_URL}/po-heads/edit/{po_number}", wait_until="networkidle")
    lines = page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('input[name^="po_lines"]').forEach(inp => {
                const m = inp.name.match(/^po_lines\\[(\\d+)\\]\\[code\\]$/);
                if (!m) return;
                const idx = m[1];
                const qEl = document.querySelector('input[type=number][name="po_lines['+idx+'][qty]"]')
                          || document.querySelector('input[name="po_lines['+idx+'][qty]"]');
                out.push({
                    idx: idx,
                    code: (inp.value||'').trim(),
                    qty: qEl ? (qEl.value||'').trim() : null,
                    disabled: qEl ? qEl.disabled : null
                });
            });
            return out;
        }"""
    )
    return lines


# ---------- 実行計画を作る ----------
def build_plan(not_bought, extras, lines):
    by_code = {}
    for ln in lines:
        by_code.setdefault(ln["code"].lower(), ln)

    to_close = []      # (code, line)
    to_setqty = []     # (code, line, new_qty, reason)
    not_found = []     # code

    for code, bought, needed in not_bought:
        ln = by_code.get(code.lower())
        if not ln:
            not_found.append(code)
            continue
        if bought == 0:
            to_close.append((code, ln))
        else:
            to_setqty.append((code, ln, bought, f"部分購入 {bought}/{needed}"))

    for code, extra in extras:
        ln = by_code.get(code.lower())
        if not ln:
            not_found.append(code)
            continue
        try:
            cur = int(float(ln["qty"]))
        except (TypeError, ValueError):
            cur = None
        new_qty = (cur + extra) if cur is not None else None
        to_setqty.append((code, ln, new_qty, f"エクストラ +{extra}（現在 {ln['qty']}）"))

    return to_close, to_setqty, not_found


def print_plan(po_number, to_close, to_setqty, not_found):
    print(f"\n===== 実行計画（PO# {po_number}）=====")
    print(f"■ Close する行（{len(to_close)}件）:")
    for code, ln in to_close:
        print(f"   - {code}  [行{ln['idx']} / 現Qty {ln['qty']} / status可否 disabled={ln['disabled']}]")
    print(f"■ 数量変更する行（{len(to_setqty)}件）:")
    for code, ln, new_qty, reason in to_setqty:
        print(f"   - {code}  Qty {ln['qty']} → {new_qty}   （{reason}）")
    if not_found:
        print(f"■ ⚠ POに見つからなかったコード（{len(not_found)}件）: {', '.join(not_found)}")
    print("=" * 40)


# ---------- 実際の変更 ----------
def apply_changes(page, po_number, to_close, to_setqty):
    # 1) 数量変更（編集画面で number入力を書き換え → SUBMIT）
    if to_setqty:
        page.goto(f"{BASE_URL}/po-heads/edit/{po_number}", wait_until="networkidle")
        for code, ln, new_qty, reason in to_setqty:
            if new_qty is None:
                print(f"   ! {code}: 新Qtyを計算できずスキップ")
                continue
            sel = f'input[type=number][name="po_lines[{ln["idx"]}][qty]"]'
            ok = page.evaluate(
                """({sel, val}) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.disabled = false;
                    el.value = String(val);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }""",
                {"sel": sel, "val": new_qty},
            )
            print(f"   数量セット {code}: → {new_qty} ({'ok' if ok else '入力欄なし'})")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        print("   数量変更を保存しました（SUBMIT）")

    # 2) Close（View画面で各行のCloseリンク→ネイティブconfirmをOK）
    for code, ln in to_close:
        page.goto(f"{BASE_URL}/po-heads/view/{po_number}", wait_until="networkidle")
        page.once("dialog", lambda d: d.accept())
        clicked = page.evaluate(
            """(code) => {
                const rows = [...document.querySelectorAll('table tr')];
                for (const tr of rows) {
                    const cl = tr.querySelector('a[href*="/products/view/"]');
                    if (cl && cl.textContent.trim().toLowerCase() === code.toLowerCase()) {
                        const close = [...tr.querySelectorAll('a')].find(a => a.textContent.trim() === 'Close');
                        if (close) { close.click(); return true; }
                        return false;  // 既にCloseできない（Complete等）
                    }
                }
                return false;
            }""",
            code,
        )
        if clicked:
            page.wait_for_load_state("networkidle")
            print(f"   Close 実行: {code}")
        else:
            print(f"   ! Close対象の行/リンクが見つからず: {code}")


def main():
    print(f"=== Shopping Report Process (DRY_RUN={DRY_RUN}) ===")
    po_number = get_po_number_from_sheet()
    if not po_number.isdigit():
        raise SystemExit(f"PO番号が不正です（D1）: {po_number!r}")

    report = fetch_report()
    if not report:
        raise SystemExit("処理対象のレポートがありません。")
    print("----- 取得レポート -----")
    print(report)
    print("------------------------")

    not_bought, extras = parse_report(report)
    print(f"解析: Not Bought {len(not_bought)}件 / Got Extra {len(extras)}件")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        login(page)

        lines = read_po_lines(page, po_number)
        print(f"PO明細: {len(lines)}行 読み込み")

        to_close, to_setqty, not_found = build_plan(not_bought, extras, lines)
        print_plan(po_number, to_close, to_setqty, not_found)

        if DRY_RUN:
            print("\n★ DRY_RUN のため、実際の変更は行いません。")
        else:
            print("\n★ 変更を実行します...")
            apply_changes(page, po_number, to_close, to_setqty)
            print("実行完了。")

        browser.close()
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
