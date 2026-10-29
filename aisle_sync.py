"""
ショッピングリスト（/po-heads/shopping-list/{PO#}）の Aisle と Code を読み取り、
Aisle 一覧スプレッドシートの「①Code-Aisle一覧」タブに、まだ無い Code だけを追記する。

- すでにある Code はスキップ（Aisle が違っていても書き換えず、ログに出すだけ）
- Aisle が空の行は追加しない
- ログインは po_import.py と同じ2段階（Basic認証 + フォームログイン）
- 環境変数: LOGIN_ID_1/PASS_1, LOGIN_ID_2/PASS_2, GOOGLE_CREDENTIALS, PO_NUMBER
- AISLE_DRY_RUN=1 のときは、書き込まずに「追加予定」だけ表示する
"""

import os
import json
from urllib.parse import quote

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

DOMAIN = "app.jrcreators.com"
AISLE_SPREADSHEET_ID = os.environ.get(
    "AISLE_SPREADSHEET_ID", "1QtmhMu6KI6M6QCLqMswF3_mAnKOvu-ea6xwMPeZLijA")
AISLE_SHEET_NAME = "①Code-Aisle一覧"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ショッピングリストの表から (Aisle, Code) の組を取り出す
AISLE_CODE_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const t of document.querySelectorAll('table')) {
    const ths = [...t.querySelectorAll('thead th, tr:first-child th')];
    const heads = ths.map(th => norm(th.innerText));
    const a = heads.indexOf('aisle'), c = heads.indexOf('code');
    if (a < 0 || c < 0) continue;
    const cx = th => { const r = th.getBoundingClientRect(); return (r.left + r.right) / 2; };
    const at = (tr, x) => [...tr.children].find(td => {
      const r = td.getBoundingClientRect(); return r.width > 0 && r.left <= x && x <= r.right; });
    const ax = cx(ths[a]), ccx = cx(ths[c]);
    return [...t.querySelectorAll('tbody tr')].map(tr => ({
      aisle: (at(tr, ax) || {innerText: ''}).innerText.trim(),
      code: (at(tr, ccx) || {innerText: ''}).innerText.trim(),
    })).filter(r => r.code);
  }
  return null;
}
"""


def read_shopping_list(po_number):
    """ショッピングリストから [(aisle, code), ...] を返す。"""
    login_id_1 = os.environ["LOGIN_ID_1"]
    login_pass_1 = os.environ["LOGIN_PASS_1"]
    login_id_2 = os.environ["LOGIN_ID_2"]
    login_pass_2 = os.environ["LOGIN_PASS_2"]
    login_url = f"https://{quote(login_id_1, safe='')}:{quote(login_pass_1, safe='')}@{DOMAIN}/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(login_url, wait_until="networkidle")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")
        page.fill('input[name="username"]', login_id_2)
        page.fill('input[type="password"]', login_pass_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(f"https://{DOMAIN}/po-heads/shopping-list/{po_number}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        rows = page.evaluate(AISLE_CODE_JS)
        browser.close()

    if rows is None:
        raise RuntimeError("ショッピングリストに Aisle / Code 列の表が見つかりません")
    return [(r["aisle"], r["code"]) for r in rows]


def sync_aisles(po_number):
    pairs = read_shopping_list(po_number)
    print(f"[Aisle同期] ショッピングリスト {len(pairs)}行を読み取りました（PO#{po_number}）")

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sa_email = creds.service_account_email  # 共有先の確認用（メールアドレスは秘密情報ではない）
    try:
        ws = gspread.authorize(creds).open_by_key(AISLE_SPREADSHEET_ID).worksheet(AISLE_SHEET_NAME)
        # 既存の Code → Aisle（大文字小文字は区別せずに照合）
        existing = {}
        for row in ws.get_all_values()[1:]:
            if row and row[0].strip():
                existing.setdefault(row[0].strip().upper(), row[1].strip() if len(row) > 1 else "")
    except Exception as e:
        raise RuntimeError(
            f"スプレッドシートを開けません。次のアカウントが『編集者』で共有されているか確認してください: "
            f"{sa_email} / 詳細: {type(e).__name__} {str(e)[:200]}") from e

    to_add, skipped_blank, skipped_dup, mismatch = [], [], 0, []
    seen = set()
    for aisle, code in pairs:
        key = code.strip().upper()
        if not aisle:
            skipped_blank.append(code)
            continue
        if key in existing or key in seen:
            skipped_dup += 1
            if key in existing and existing[key] and existing[key] != aisle:
                mismatch.append((code, existing[key], aisle))
            continue
        seen.add(key)
        to_add.append([code.strip(), aisle])

    print(f"[Aisle同期] 追加 {len(to_add)}件 / 既にある {skipped_dup}件 / Aisleが空 {len(skipped_blank)}件")
    if skipped_blank:
        print(f"[Aisle同期] Aisleが空のため追加しなかった Code: {', '.join(skipped_blank)}")
    for code, old, new in mismatch:
        print(f"[Aisle同期] 注意: {code} は既に {old} で登録済み（リストでは {new}）。書き換えません")

    if not to_add:
        return
    if os.environ.get("AISLE_DRY_RUN") == "1":
        print(f"[Aisle同期] DRY RUN: 追加予定 {to_add}")
        return
    # 表の最後の行の次から追記（RAW: 先頭ゼロなどを勝手に変換させない）
    ws.append_rows(to_add, value_input_option="RAW", table_range="A1")
    print(f"[Aisle同期] {len(to_add)}件をタブ『{AISLE_SHEET_NAME}』に追加しました")


if __name__ == "__main__":
    po = os.environ.get("PO_NUMBER", "").strip()
    if not po.isdigit():
        raise SystemExit(f"PO_NUMBER が不正です: {po!r}")
    sync_aisles(po)
