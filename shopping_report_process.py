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


# ---------- スプレッドシート ----------
def open_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHOPPING_SPREADSHEET_ID)


# ---------- PO番号（シートのD1・旧形式レポート用フォールバック）----------
def get_po_number_from_sheet(ss):
    ws = ss.worksheet(SHEET_NAME) if SHEET_NAME else ss.sheet1
    po = str(ws.acell("D1").value or "").strip()
    print(f"タブ [{ws.title}] の D1 から PO# = {po!r}")
    return po


# ---------- 二重実行防止（_state タブに処理済み message_id を記録）----------
STATE_SHEET = "_state"


def get_state_ws(ss):
    try:
        return ss.worksheet(STATE_SHEET)
    except gspread.WorksheetNotFound:
        print(f"「{STATE_SHEET}」タブが無いため新規作成します")
        return ss.add_worksheet(title=STATE_SHEET, rows=10, cols=5)


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
            return m["body"], str(m.get("message_id", ""))
    print("Chatwork: [End Shopping Report] が見つかりません")
    return None, None


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
        # 「[Extra]」「[Got Extra]」どちらの見出しにも対応
        if low.startswith("[extra") or low.startswith("[got extra"):
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


# ---------- 店舗セクション分割（PO#付きレポート）----------
# 例: 「TG. WM (PO# 156477)」の行でセクション開始
SECTION_RE = re.compile(r"\(\s*PO#\s*(\d+)\s*\)", re.IGNORECASE)


def split_sections(text):
    """「店舗名 (PO# 123456)」行でレポートを分割し、[(店舗名, PO番号, 本文)] を返す。

    PO#付きの行が1つも無ければ空リスト（＝旧形式レポート）。
    先頭の全体サマリーやメモは、最初のPO#行より前なので自動的に無視される。
    """
    sections = []
    cur = None
    for line in text.splitlines():
        m = SECTION_RE.search(line)
        if m:
            if cur:
                sections.append(cur)
            store = SECTION_RE.sub("", line).strip() or "(店舗名なし)"
            cur = {"store": store, "po": m.group(1), "lines": []}
        elif cur is not None:
            cur["lines"].append(line)
    if cur:
        sections.append(cur)
    return [(c["store"], c["po"], "\n".join(c["lines"])) for c in sections]


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
    """変更を実行し、(closed_ok, reduced_ok, extra_ok, issues) を返す。"""
    issues = []
    set_done = []   # (code, reason) 実際に入力できたもの
    closed_ok = 0

    # 1) 数量変更（編集画面で number入力を書き換え → SUBMIT）
    if to_setqty:
        page.goto(f"{BASE_URL}/po-heads/edit/{po_number}", wait_until="networkidle")
        for code, ln, new_qty, reason in to_setqty:
            if new_qty is None:
                print(f"   ! {code}: 新Qtyを計算できずスキップ")
                issues.append(f"Could not compute new qty: {code}")
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
            if ok:
                set_done.append((code, ln["qty"], new_qty, reason))
            else:
                issues.append(f"Qty input not found: {code}")
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
            closed_ok += 1
        else:
            print(f"   ! Close対象の行/リンクが見つからず: {code}")
            issues.append(f"Close link not found: {code}")

    reduced_list = [(c, b, a) for c, b, a, r in set_done if r.startswith("部分購入")]
    extra_list = [(c, b, a) for c, b, a, r in set_done if r.startswith("エクストラ")]
    return closed_ok, reduced_list, extra_list, issues


# ---------- Chatwork 完了通知（本番実行時のみ）----------
def post_chatwork_summary(results):
    lines = ["✅ PO Edit Completed"]
    all_not_found = []
    any_store = False
    for r in results:
        has_change = r["closed"] or r["reduced"] or r["extra"] or r["issues"]
        if has_change:
            any_store = True
            lines.append("")  # 店舗ブロックの前に空行
            lines.append(f"{r['label']} (PO# {r['po']})")
            lines.append(f"{BASE_URL}/po-heads/view/{r['po']}")
            if r["closed"]:
                lines.append(f"Closed Not Bought: {r['closed']}")
            if r["reduced"]:
                lines.append("Reduced: " + " / ".join(f"{c} {b}->{a}" for c, b, a in r["reduced"]))
            if r["extra"]:
                lines.append("Extra: " + " / ".join(f"{c} {b}->{a}" for c, b, a in r["extra"]))
            for issue in r["issues"]:
                lines.append(f"⚠ {issue}")
        all_not_found.extend(r["not_found"])
    if not any_store:
        lines.append("")
        lines.append("No changes needed.")
    lines.append("")
    lines.append("⚠ Codes not found: " + (", ".join(all_not_found) if all_not_found else "none"))
    body = "\n".join(lines)

    print("----- Chatwork通知 -----")
    print(body)
    print("------------------------")
    resp = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data={"body": body},
    )
    print(f"Chatwork通知送信: status={resp.status_code}")


def main():
    print(f"=== Shopping Report Process (DRY_RUN={DRY_RUN}) ===")
    report, msg_id = fetch_report()
    if not report:
        raise SystemExit("処理対象のレポートがありません。")
    print(f"レポート message_id = {msg_id}")
    print("----- 取得レポート -----")
    print(report)
    print("------------------------")

    ss = open_spreadsheet()
    state_ws = get_state_ws(ss)

    # 二重実行防止：本番実行時、同じレポートを既に処理済みならスキップ
    if not DRY_RUN:
        processed_id = str(state_ws.acell("B1").value or "").strip()
        if processed_id and processed_id == str(msg_id):
            print(f"★ このレポート (message_id={msg_id}) は既に処理済みです。二重実行を防ぐためスキップします。")
            print("=== 完了 ===")
            return

    # 店舗セクション（PO#付き）に分割。無ければ旧形式として従来方式（タブD1）で処理
    sections = split_sections(report)
    targets = []  # (表示名, PO番号, not_bought, extras)
    if sections:
        print(f"店舗セクション: {len(sections)}件")
        for store, po, text in sections:
            nb, ex = parse_report(text)
            targets.append((store, po, nb, ex))
    else:
        print("PO#付きの店舗行が無いため、従来方式（タブD1のPO#）で処理します")
        po = get_po_number_from_sheet(ss)
        if not po.isdigit():
            raise SystemExit(f"PO番号が不正です（D1）: {po!r}")
        nb, ex = parse_report(report)
        targets.append(("(タブD1)", po, nb, ex))

    results = []  # 本番実行の結果（Chatwork通知用）

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        login(page)

        for label, po, nb, ex in targets:
            print(f"\n########## {label} / PO# {po} ##########")
            if not str(po).isdigit():
                print(f"！PO番号が不正のためスキップ: {po!r}")
                continue
            print(f"解析: Not Bought {len(nb)}件 / Extra {len(ex)}件")
            if not nb and not ex:
                print("対象項目なし。スキップ。")
                continue

            lines = read_po_lines(page, po)
            print(f"PO明細: {len(lines)}行 読み込み")

            to_close, to_setqty, not_found = build_plan(nb, ex, lines)
            print_plan(po, to_close, to_setqty, not_found)

            if DRY_RUN:
                print("★ DRY_RUN のため、実際の変更は行いません。")
            else:
                print("★ 変更を実行します...")
                closed_ok, reduced_list, extra_list, issues = apply_changes(page, po, to_close, to_setqty)
                results.append({
                    "label": label,
                    "po": po,
                    "closed": closed_ok,
                    "reduced": reduced_list,
                    "extra": extra_list,
                    "not_found": not_found,
                    "issues": issues,
                })
                print("実行完了。")

        browser.close()

    # 本番実行が完了したら、処理済みマーク（message_id）を記録して二重実行を防ぐ
    if not DRY_RUN and msg_id:
        state_ws.update("A1", [["processed_report_id", str(msg_id)]], value_input_option="RAW")
        print(f"処理済みマークを記録しました（_stateタブ, message_id={msg_id}）")

    # 完了通知（本番実行時のみ・通知失敗で処理は落とさない）
    if not DRY_RUN:
        try:
            post_chatwork_summary(results)
        except Exception as e:
            print(f"Chatwork通知の送信に失敗しました（処理自体は完了しています）: {e}")

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
