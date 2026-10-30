"""
卸 PO 自動入力（テスト版）

やること:
  1. 社内システムにログイン（Basic認証 + フォームログイン）
  2. PO 一覧ページ（daily / wholesale）を開く
  3. 各行の「Live Qty」の絶対値を「Purchase Qty」欄に入力する
  4. 入力結果を CSV と スクリーンショットに保存する

やらないこと（テスト版）:
  - PO作成画面の SUBMIT は、確認の Enter を押すまで押さない（--auto-submit で確認なし）
  - SUBMIT 後は、既存の GitHub Actions『po_import』を呼んで TG&WM タブへ取り込む
    （環境変数 GITHUB_TOKEN が必要。無ければ PO番号を表示するだけ）

PO作成画面でやること:
  「All Qty for Supplier」クリック → Order Number("1026 Shopping List")・
  Est Delivery(翌日)・Suppliers を入力 → Copy Price → Max Price 超過を一覧表示

ログイン情報は環境変数から読む（コードには書かない）:
  APP_DOMAIN, LOGIN_ID_1, LOGIN_PASS_1, LOGIN_ID_2, LOGIN_PASS_2
"""
import csv
import json
import urllib.error
import urllib.parse
import urllib.request
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from playwright.sync_api import sync_playwright

DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]      # Basic認証 ID
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]  # Basic認証 パスワード
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]      # フォームログイン ID
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]  # フォームログイン パスワード

TARGET_PATH = (
    "/po-temps/wholesale?flag=daily&flag_filter=&purchaser=14&_supplier_ids="
    "&_supplier_ids%5B%5D=190&_supplier_ids%5B%5D=1&_supplier_ids%5B%5D=183"
    "&_supplier_ids%5B%5D=191&_supplier_ids%5B%5D=133"
    "&suppliers_select_all=0&suppliers_select_all=1&neg_live=0&neg_live=1"
    "&has_suggested=0&has_rush=0"
)

# PO作成後に、既存の GitHub Actions『po_import』を呼んでスプレッドシートへ取り込む
GITHUB_REPO = "ryojrcreators/daily-wholesale-report"
GITHUB_REF = "main"
IMPORT_WORKFLOW = "po_import.yml"
IMPORT_SHEET = "TG&WM"
# 決まった個数単位でしか買えない商品（商品コード → 単位）。数量は単位の倍数に切り上げる
QTY_MULTIPLES = {"WO-84": 12, "W-114": 12, "W-108": 12, "BAL111": 6}
# 買い物担当（Jill）が Chatwork グループに「I'm off today」と書いた日は、PO を作らない
CW_ROOM_ID = "88583488"
CW_SHOPPER_ID = 10892606        # Jill S Vinales（po_import の通知あて先と同じアカウントID）
OFF_PATTERN = re.compile(r"i[’'`]?m\s+off\s+today", re.I)
# 結果通知の送り先（ルーム 296236026 = 「TG/WM list is ready」と同じルーム）
NOTIFY_ROOM_ID = "296236026"
NOTIFY_TO_ID = "2618849"
NOTIFY_TO_NAME = "Ryo Higuchi"
MAX_PAGES = 50  # 無限ループ防止の上限
MAX_EXPECTED_PAGES = 3  # 普段は最大2ページ。これを超えたらフィルタ異常とみなして入力せず止まる
# 画面のフィルタが想定どおりか確認するための期待値（URLの条件と同じ）
EXPECTED_FILTER = {
    "flag": "daily", "flag_filter": "", "purchaser": "14",
    "suppliers": {"190", "1", "183", "191", "133"},
    "neg_live": True, "has_suggested": False, "has_rush": False,
}
RECENT_CASE_DAYS = 7  # この日数以内に同じ商品のChange Priceケースがあれば、新しく作らない
SUPPLIER_LABEL ="1-TargetWalmart (cosmetics/other)"
OUT_DIR = Path(__file__).parent / "output"
LIVE_HEADER = "live qty"
PURCHASE_HEADER = "purchase qty"


def notify_chatwork(text):
    """実行結果を Chatwork に送る。送り先は環境変数で指定する（未設定なら画面に出すだけ）。
      CW_TOKEN          : Chatwork API トークン
      CW_NOTIFY_ROOM_ID : 送り先ルームID
      CW_NOTIFY_TO      : （任意）あて先のアカウントID。指定すると [To:ID] が付く
    通知に失敗しても、本体の処理は止めない。"""
    print(f"[通知] {text}")
    token = os.environ.get("CW_TOKEN", "")
    room = os.environ.get("CW_NOTIFY_ROOM_ID", NOTIFY_ROOM_ID)
    if not token or not room:
        return
    to = os.environ.get("CW_NOTIFY_TO", NOTIFY_TO_ID).strip()
    to_name = NOTIFY_TO_NAME if to == NOTIFY_TO_ID else ""
    body = (f"[To:{to}]{to_name}\n" if to else "") + f"[PO自動化 {now_la().strftime('%m/%d')}]\n{text}"
    data = urllib.parse.urlencode({"body": body}).encode()
    req = urllib.request.Request(f"https://api.chatwork.com/v2/rooms/{room}/messages",
                                 data=data, method="POST", headers={"X-ChatWorkToken": token})
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except Exception as e:
        print(f"! Chatwork 通知に失敗しました（{type(e).__name__}）")


def shopper_is_off_today():
    """買い物担当が今日（LA時間）休みと連絡しているか。休みなら True。
    確認できなかった（トークン無し・通信失敗）ときは None を返す。
    トークンは環境変数 CW_TOKEN（Chatwork API トークン）から読む。"""
    token = os.environ.get("CW_TOKEN", "")
    if not token:
        print("! CW_TOKEN が未設定のため、休みの連絡の確認をスキップします")
        return None
    req = urllib.request.Request(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages?force=1",
        headers={"X-ChatWorkToken": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8")
            messages = json.loads(raw) if raw.strip() else []  # 新着なしのとき 204(空)が返る
    except Exception as e:
        print(f"! Chatwork の読み取りに失敗しました（{type(e).__name__}）。休みの連絡を確認できません")
        return None
    today = now_la().date()
    for m in messages:
        if (m.get("account") or {}).get("account_id") != CW_SHOPPER_ID:
            continue
        sent = datetime.fromtimestamp(m["send_time"], ZoneInfo("America/Los_Angeles")).date()
        if sent != today:
            continue
        body = re.sub(r"\[qt\].*?\[/qt\]", "", m.get("body", ""), flags=re.S)  # 引用部分は除く
        if OFF_PATTERN.search(body):
            return True
    return False


def now_la():
    """LA時間の現在時刻（時間帯情報なし）。Order Number・Est Delivery・土日判定・直近ケース判定の基準。
    GitHub Actions（UTC）で動かしても、PCの時間帯に関係なく同じ日付になる。"""
    return datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)


def login(page):
    page.goto(f"https://{DOMAIN}/", wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


# ページ内の表から「Live Qty」「Purchase Qty」列を探して、行ごとの情報を返す
FIND_TABLE_JS = """
([liveH, purH]) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const tables = [...document.querySelectorAll('table')];
  for (let t = 0; t < tables.length; t++) {
    const ths = [...tables[t].querySelectorAll('thead th, tr:first-child th')];
    const heads = ths.map(th => norm(th.innerText));
    const live = heads.findIndex(h => h.includes(liveH));
    const pur = heads.findIndex(h => h.includes(purH));
    if (live >= 0 && pur >= 0) {
      // 見出しセルの画面上の横位置と重なるセルを探す（非表示セルがあってもずれない）
      const cx = th => { const r = th.getBoundingClientRect(); return (r.left + r.right) / 2; };
      const liveX = cx(ths[live]), purX = cx(ths[pur]);
      const codeH = heads.indexOf('product code');
      const codeX = codeH >= 0 ? cx(ths[codeH]) : null;
      const cellAt = (tr, x) => [...tr.children].findIndex(td => {
        const r = td.getBoundingClientRect();
        return r.width > 0 && r.left <= x && x <= r.right;
      });
      const rows = [...tables[t].querySelectorAll('tbody tr')].map((tr, i) => {
        const tds = [...tr.children];
        const li = cellAt(tr, liveX), pi = cellAt(tr, purX);
        const ci = codeX === null ? -1 : cellAt(tr, codeX);
        return {
          row: i, liveIdx: li, purIdx: pi, nCells: tds.length,
          code: ci >= 0 ? tds[ci].innerText.trim() : '',
          live: li >= 0 ? tds[li].innerText.trim() : null,
          hasInput: pi >= 0 && !!tds[pi].querySelector('input'),
          first: tds.slice(0, 4).map(td => td.innerText.trim().slice(0, 40)),
        };
      });
      return { table: t, live, pur, heads, rows };
    }
  }
  return { table: -1, tables: tables.length,
           heads: tables.map(t => [...t.querySelectorAll('th')].map(th => norm(th.innerText))) };
}
"""


# PO作成画面の明細行を読み、単価がMax Priceを超えていないか／赤く表示されていないかを返す
CHECK_LINES_JS = """
() => {
  const num = s => { const m = (s || '').replace(/,/g, '').match(/-?\\d+(\\.\\d+)?/); return m ? parseFloat(m[0]) : null; };
  const isRed = el => {
    const c = getComputedStyle(el);
    const rgb = s => (s.match(/\\d+/g) || []).map(Number);
    const [r, g, b] = rgb(c.color), [br, bg, bb, ba] = rgb(c.backgroundColor);
    const redText = r > 180 && g < 90 && b < 90;
    const redBg = br > 180 && bg < 90 && bb < 90 && (ba === undefined || ba > 0);
    return redText || redBg;
  };
  const out = [];
  for (const price of document.querySelectorAll('input.priceinput')) {
    const tr = price.closest('tr');
    const qty = tr.querySelector('input.qtyinput');
    const cells = [...tr.children].filter(td => td.getBoundingClientRect().width > 0);
    // Max Price は「remove」ボタン列の1つ手前にある数値セル
    const nums = cells.map(td => num(td.innerText)).filter(v => v !== null);
    const maxCell = cells.length >= 2 ? cells[cells.length - 2] : null;
    const max = maxCell ? num(maxCell.innerText) : null;
    const p = num(price.value);
    out.push({
      code: (tr.querySelector('td') || {}).innerText ? tr.querySelector('td').innerText.trim().split('\\n')[0] : '',
      qty: qty ? qty.value : '', price: price.value, max,
      over: p !== null && max !== null && p > max,
      err: price.classList.contains('input-error'),
      href: (tr.querySelector('td a[href*="/products/view/"]') || {getAttribute: () => ''}).getAttribute('href'),
      // 実機で確認済み: Max Budget 超過の単価欄には input-error クラスが付き、背景が赤くなる
      red: price.classList.contains('input-error') || isRed(price) || isRed(tr)
           || [...tr.querySelectorAll('td, input, span')].some(isRed),
      // 赤字の実例を見て判定を調整するための手がかり（クラス名のみ・個人情報なし）
      rowClass: tr.className, priceClass: price.className,
    });
  }
  return out;
}
"""


READ_FILTER_JS = """
() => ({
  flag: document.querySelector('#flag')?.value,
  flag_filter: document.querySelector('#flag-filter')?.value,
  purchaser: document.querySelector('#purchaser')?.value,
  suppliers: [...(document.querySelector('#supplier-ids')?.selectedOptions || [])].map(o => o.value),
  neg_live: document.querySelector('#neg-live')?.checked,
  has_suggested: document.querySelector('#has-suggested')?.checked,
  has_rush: document.querySelector('#has-rush')?.checked,
})
"""


def filter_problems(page):
    """画面のフィルタが想定と違う点の一覧を返す（空なら問題なし）。"""
    got = page.evaluate(READ_FILTER_JS)
    exp = EXPECTED_FILTER
    problems = []
    for k in ("flag", "flag_filter", "purchaser"):
        if got.get(k) != exp[k]:
            problems.append(f"{k}: 期待 {exp[k]!r} / 実際 {got.get(k)!r}")
    if set(got.get("suppliers") or []) != exp["suppliers"]:
        problems.append(f"suppliers: 期待 {sorted(exp['suppliers'])} / 実際 {sorted(got.get('suppliers') or [])}")
    for k in ("neg_live", "has_suggested", "has_rush"):
        if bool(got.get(k)) != exp[k]:
            problems.append(f"{k}: 期待 {exp[k]} / 実際 {got.get(k)}")
    pos = page_position(page)
    if pos and pos[1] > MAX_EXPECTED_PAGES:
        problems.append(f"ページ数が多すぎる: 全{pos[1]}ページ（想定は最大{MAX_EXPECTED_PAGES}ページ）")
    return problems


def page_position(page):
    """画面下の 'page 1 of 3' から (現在, 全体) を読む。無ければ None。"""
    m = re.search(r"page\s+(\d+)\s+of\s+(\d+)", page.inner_text("body"), re.I)
    return (int(m.group(1)), int(m.group(2))) if m else None


def go_next_page(page, table, first_cells):
    """「next >」で次ページへ進む。進めたら True、最終ページなら False。"""
    pos = page_position(page)
    if pos and pos[0] >= pos[1]:
        return False
    nxt = page.locator("a", has_text=re.compile(r"^\s*next\s*>?\s*$", re.I)).first
    if nxt.count() == 0 or not nxt.is_visible():
        return False
    nxt.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    new_pos = page_position(page)
    if pos and new_pos and new_pos[0] <= pos[0]:
        print(f"! 次ページへ進めませんでした（{pos} → {new_pos}）。ここで止めます")
        return False
    print(f"  次のページへ移動しました（{new_pos}）")
    return True


def parse_qty(text):
    """'1,234' / '-5' / '(5)' → 絶対値の整数。読めなければ None。"""
    if text is None:
        return None
    s = text.replace(",", "").replace("(", "-").replace(")", "").strip()
    try:
        return abs(int(float(s)))
    except ValueError:
        return None


def main():
    # 土日は実行しない（LA時間の曜日で判定）。どうしても動かしたいときは --force を付ける
    if now_la().weekday() >= 5 and "--force" not in sys.argv:
        print("土日（LA時間）のため実行しません（動かす場合は --force を付けてください）")
        return
    # 買い物担当が「I'm off today」と連絡していたら、今日は作らない
    if "--force" not in sys.argv and "--no-off-check" not in sys.argv:
        off = shopper_is_off_today()
        if off:
            print("買い物担当（Jill）から休みの連絡があったため、今日は実行しません"
                  "（動かす場合は --force を付けてください）")
            return
        if off is None and "--auto-submit" in sys.argv:
            print("休みの連絡を確認できないため、自動実行は安全のため中止します")
            return
        if off is None:
            if input("休みの連絡を確認できていません。続けますか？ (y/n): ").strip().lower() != "y":
                return
    if ("--headless" in sys.argv and "--auto-submit" not in sys.argv
            and "--dry-run" not in sys.argv):
        raise SystemExit("--headless は --auto-submit か --dry-run と一緒に使ってください"
                         "（確認の入力待ちで止まるため）")
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    headless = "--headless" in sys.argv

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            http_credentials={"username": LOGIN_ID_1, "password": LOGIN_PASS_1},
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        login(page)

        # 今日の PO がすでにあるなら、何もせずに止まる（二重作成・ケースの二重作成を防ぐ）
        if stop_if_po_exists(context):
            browser.close()
            return

        page.goto(f"https://{DOMAIN}{TARGET_PATH}", wait_until="networkidle")

        # ---- 全ページ分の Purchase Qty を入力する（ページ移動しても入力は残る）----
        report = []
        page_no = 1
        while True:
            problems = filter_problems(page)
            if problems:
                print(f"! {page_no}ページ目: フィルタが想定と違うため、入力せずに止まります（PO作成もしません）")
                for pr in problems:
                    print("   -", pr)
                notify_chatwork("⚠ 停止: フィルタが想定と違うため、入力もPO作成もしていません。\n"
                                + "\n".join(f"・{pr}" for pr in problems))
                page.screenshot(path=str(OUT_DIR / f"{stamp}_filter_error_p{page_no}.png"), full_page=True)
                if not headless:
                    input("Enterで終了...")
                browser.close()
                return
            info = page.evaluate(FIND_TABLE_JS, [LIVE_HEADER, PURCHASE_HEADER])
            if info["table"] < 0:
                print(f"{page_no}ページ目: Live Qty / Purchase Qty 列のある表が見つかりません。")
                print("ページ内の表の見出し:", info.get("heads"))
                page.screenshot(path=str(OUT_DIR / f"{stamp}_notfound_p{page_no}.png"), full_page=True)
                if page_no == 1:
                    # 一覧が空のときは、画面に「No Records」と出る。異常ではなく、対象が0件の日
                    if re.search(r"No Records", page.inner_text("body"), re.I):
                        notify_chatwork("ℹ 今日の対象商品は0件でした（Purchase Qty の一覧が空）。"
                                        "PO は作っていません。")
                    else:
                        notify_chatwork("⚠ 停止: Purchase Qty の表が見つかりません。PO作成はしていません。")
                    browser.close()
                    return
                report.append([page_no, "", "", "", "", "", "", "", "TABLE_NOT_FOUND"])
                break

            rows = info["rows"]
            print(f"{page_no}ページ目: 行数 {len(rows)}")
            if page_no == 1:
                print("表の見出し:", info["heads"])
            table = page.locator("table").nth(info["table"])
            for r in rows:
                qty = parse_qty(r["live"])
                unit = QTY_MULTIPLES.get((r.get("code") or "").strip().upper())
                if qty is not None and unit:
                    rounded = -(-qty // unit) * unit  # 単位の倍数に切り上げ（例: 2→12, 13→24）
                    print(f"  {r['code']}: Live Qty {r['live']} → {qty} → {unit}の倍数に切り上げ → {rounded}")
                    qty = rounded
                if qty is None or not r["hasInput"]:
                    report.append([page_no, r["row"], *r["first"], r["live"], "", "skip"])
                    continue
                cell_input = (
                    table.locator("tbody tr").nth(r["row"])
                    .locator("> *").nth(r["purIdx"]).locator("input").first
                )
                cell_input.fill(str(qty))
                actual = cell_input.input_value()
                status = "ok" if actual == str(qty) else "MISMATCH"
                report.append([page_no, r["row"], *r["first"], r["live"], actual, status])
            page.screenshot(path=str(OUT_DIR / f"{stamp}_filled_p{page_no}.png"), full_page=True)

            first_cells = rows[0]["first"] if rows else None
            if not go_next_page(page, table, first_cells):
                break
            page_no += 1
            if page_no > MAX_PAGES:
                print(f"! {MAX_PAGES}ページを超えたため停止します")
                report.append([page_no, "", "", "", "", "", "", "", "TOO_MANY_PAGES"])
                break

        csv_path = OUT_DIR / f"{stamp}_fill_report.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["page", "row", "col1", "col2", "col3", "col4", "live_qty", "purchase_qty_input", "status"])
            w.writerows(report)

        ok = sum(1 for x in report if x[-1] == "ok")
        print(f"全{page_no}ページ 入力OK: {ok} / スキップ: {sum(1 for x in report if x[-1] == 'skip')} "
              f"/ 不一致: {sum(1 for x in report if x[-1] == 'MISMATCH')}")
        print(f"レポート: {csv_path}")

        if ok == 0 or ok != len(report):
            print("入力できなかった行があるため、PO作成画面へは進みません。レポートを確認してください。")
            bad = sum(1 for x in report if x[-1] != "ok")
            notify_chatwork(f"⚠ 停止: Purchase Qty を入力できない行が{bad}件あったため、PO作成をしていません"
                            f"（入力OK {ok}件）。")
            if not headless:
                input("Enterで終了...")
            browser.close()
            return

        # ---- All Qty for Supplier → PO作成画面（SUBMITは押さない）----
        po_page = open_po_add_page(context, page)
        fill_po_header(po_page)
        over = copy_price_and_check(po_page, stamp)

        # 試走: ここまでで終了。ケース作成も PO の SUBMIT も、取り込みの起動もしない
        if "--dry-run" in sys.argv:
            lines = [f"🧪 試走（PO もケースも作っていません）: 商品 {ok}行 / {page_no}ページ。"
                     "PO作成画面の入力と Copy Price まで正常に進みました。"]
            if over:
                lines.append(f"Max Price超過 {len(over)}件（本番ならケースを作成します）:")
                for x in over[:15]:
                    lines.append(f"  ・{x['code']}  単価 {x['price']} / Max {x['max']}")
                if len(over) > 15:
                    lines.append(f"  …ほか {len(over) - 15}件")
            else:
                lines.append("Max Price超過: なし")
            notify_chatwork("\n".join(lines))
            browser.close()
            return

        # Max Price 超過の商品は Change Price ケースを作成（--auto-submit なしなら SUBMIT 前に確認）
        if over:
            print(f"Change Priceケースを作成します（{len(over)}件）")
            results = []
            for x in over:
                status, detail = create_change_price_case(context, x, "--auto-submit" in sys.argv, stamp)
                results.append([x["code"], x["price"], x["max"], status, detail])
            res_path = OUT_DIR / f"{stamp}_case_results.csv"
            with open(res_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["product_code", "price", "max_price", "result", "detail"])
                w.writerows(results)
            print(f"ケース作成結果: {res_path}")
            for st in ("created", "skip_duplicate", "skip_no_assignee", "skip_no_link"):
                n = sum(1 for r in results if r[3] == st)
                if n:
                    print(f"  {st}: {n}件")

        # 作業中に別の実行が PO を作っていないか、SUBMIT の直前にもう一度確認する
        if stop_if_po_exists(context):
            browser.close()
            return

        po_number = submit_po(po_page, "--auto-submit" in sys.argv, stamp)
        if po_number:
            imported = dispatch_import(po_number)
            lines = [f"✅ PO#{po_number} を作成しました（商品 {ok}行 / {page_no}ページ）。"]
            if over:
                cnt = {}
                for r in results:
                    cnt[r[3]] = cnt.get(r[3], 0) + 1
                lines.append(f"Max Price超過 {len(over)}件 → ケース作成 {cnt.get('created', 0)}件 / "
                             f"7日以内に作成済みで見送り {cnt.get('skip_duplicate', 0)}件 / "
                             f"担当不明などで未作成 {cnt.get('skip_no_assignee', 0) + cnt.get('skip_no_link', 0)}件")
                for r in results:
                    if r[3] in ("skip_no_assignee", "skip_no_link"):
                        lines.append(f"  ! {r[0]}: ケース未作成（手動で対応してください）")
            lines.append("TG&WMタブへの取り込みを起動しました。" if imported
                         else "! TG&WMタブへの取り込みを起動できませんでした。「Import PO」で取り込んでください。")
            notify_chatwork("\n".join(lines))
        else:
            notify_chatwork("⚠ PO の作成を確認できませんでした。画面で、PO が作られたか確認してください。")
        browser.close()


def open_po_add_page(context, page):
    """「All Qty for Supplier」を押して PO 作成画面(po-heads/add)のページを返す。
    同じタブで遷移する場合も、新しいタブが開く場合も対応する。"""
    btn = page.locator(':is(button, a, input[type=submit], input[type=button]):has-text("All Qty"),'
                       ' input[value*="All Qty" i]').first
    try:
        with context.expect_page(timeout=4000) as new_page_info:
            btn.click()
        po_page = new_page_info.value
    except Exception:
        po_page = page  # 新しいタブが開かなかった = 同じタブで遷移
    po_page.wait_for_url("**/po-heads/add**", timeout=30000)
    po_page.wait_for_load_state("networkidle")
    print("PO作成画面を開きました")
    return po_page


def order_number_today():
    """今日の Order Number。例: 1026 Shopping List（LA時間の日付）"""
    return f"{now_la().strftime('%m%d')} Shopping List"


# PO一覧の表から Order Number / Suppliers / Id を読む
PO_LIST_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  for (const t of document.querySelectorAll('table')) {
    const heads = [...t.querySelectorAll('th')].map(th => norm(th.textContent).toLowerCase());
    const o = heads.indexOf('order number'), s = heads.indexOf('suppliers'), i = heads.indexOf('id');
    if (o < 0 || s < 0 || i < 0) continue;
    return [...t.querySelectorAll('tbody tr')].map(tr => {
      const td = [...tr.children];
      return { id: norm(td[i]?.textContent), order: norm(td[o]?.textContent),
               supplier: norm(td[s]?.textContent) };
    });
  }
  return null;
}
"""


def find_existing_po(context):
    """今日の Order Number + 同じ Supplier の PO がすでにあれば、その PO番号を返す。無ければ None。
    （同じ Order Number でも別の Supplier の PO は、重複とみなさない）
    PO一覧を調べられなかったときは、安全のため例外を出して止める。"""
    order_no = order_number_today()
    page = context.new_page()
    try:
        url = f"https://{DOMAIN}/po-heads?" + urllib.parse.urlencode({"PoHeads[order_number]": order_no})
        page.goto(url, wait_until="networkidle")
        # 検索フォームが出ていれば PO一覧のページ。結果が0件のときは表の行が無いだけ
        has_form = page.locator("#poheads-order-number").count() > 0
        rows = page.evaluate(PO_LIST_JS)
        if rows is None and not has_form:
            OUT_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(OUT_DIR / "po_list_error.png"), full_page=True)
            raise RuntimeError(f"PO一覧のページを開けませんでした（title={page.title()!r} / "
                               f"url={page.url.split('?')[0]}）。二重作成の確認ができません")
        if rows is None:
            rows = []  # フォームはあるが表が無い = 該当なし
    finally:
        page.close()
    for r in rows:
        if r["order"].lower() == order_no.lower() and r["supplier"] == SUPPLIER_LABEL:
            return r["id"].replace(",", "")
    return None


def stop_if_po_exists(context):
    """今日の PO がすでにあれば通知して True を返す（--allow-duplicate で無視できる）。"""
    if "--allow-duplicate" in sys.argv:
        return False
    if "--dry-run" in sys.argv:
        return False  # 試走は PO もケースも作らないので、今日の PO があっても続ける
    existing = find_existing_po(context)
    if existing:
        notify_chatwork(f"⚠ 停止: 今日のPO（{order_number_today()} / {SUPPLIER_LABEL}）がすでにあります"
                        f"（PO#{existing}）。二重作成を防ぐため、何もしていません。")
        return True
    return False


def fill_po_header(po_page):
    today = now_la()
    order_no = order_number_today()
    delivery = (today + timedelta(days=1)).strftime("%Y-%m-%d")  # 翌日 例: 2026-10-27

    po_page.fill("#order-number", order_no)
    # Suppliers は select2 の元になっている select に、表示名で直接指定する
    po_page.select_option("#suppliers-id", label=SUPPLIER_LABEL)
    pick_delivery_date(po_page, today + timedelta(days=1))
    got = po_page.input_value("#est-delivery")
    if got != delivery:
        raise RuntimeError(f"Est Delivery が入りませんでした（期待 {delivery} / 実際 {got!r}）")
    print(f"Order Number: {order_no} / Est Delivery: {delivery} / Supplier: {SUPPLIER_LABEL}")


def submit_po(po_page, auto_submit, stamp):
    """PO作成画面の SUBMIT を押し、作られた PO番号を返す。押さなかった/失敗したら None。
    --auto-submit が無いときは、SUBMIT の直前で止まって確認する。"""
    po_page.bring_to_front()
    if not auto_submit:
        ans = input("★ PO を SUBMIT します。画面を確認して Enter（やめるときは n と入力して Enter）...")
        if ans.strip().lower() == "n":
            print("SUBMIT せずに終了します（PO未作成）")
            return None
    po_page.click('button:has-text("SUBMIT"), input[type=submit][value*="SUBMIT" i]')
    try:
        po_page.wait_for_url(re.compile(r"/po-heads/view/\d+"), timeout=30000)
    except Exception:
        po_page.screenshot(path=str(OUT_DIR / f"{stamp}_po_submit_unknown.png"), full_page=True)
        print("! SUBMIT 後に PO の詳細ページへ移動しませんでした。PO が作られたか画面で確認してください")
        return None
    m = re.search(r"/po-heads/view/(\d+)", po_page.url)
    po_number = m.group(1) if m else None
    po_page.wait_for_load_state("networkidle")
    po_page.screenshot(path=str(OUT_DIR / f"{stamp}_po_submitted.png"), full_page=True)
    print(f"PO を作成しました: PO#{po_number}")
    return po_number


def dispatch_import(po_number):
    """既存の GitHub Actions『po_import』を起動し、PO を TG&WM タブへ取り込む。
    （スプレッドシートの『Import PO』と同じ処理。完了後 Chatwork 通知も既存どおり出る）
    GitHub のトークンは環境変数 GITHUB_TOKEN から読む。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(f"! GITHUB_TOKEN が未設定のため取り込みは起動しません。"
              f"スプレッドシートの『Import PO』に PO#{po_number} を入れてください")
        return False
    url = (f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/"
           f"{IMPORT_WORKFLOW}/dispatches")
    body = json.dumps({"ref": GITHUB_REF,
                       "inputs": {"po_number": str(po_number), "sheet_name": IMPORT_SHEET}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            print(f"取り込みを起動しました（PO#{po_number} → タブ {IMPORT_SHEET} / HTTP {res.status}）")
            return True
    except urllib.error.HTTPError as e:
        print(f"! 取り込みの起動に失敗しました（HTTP {e.code}）。『Import PO』から手動で取り込んでください: PO#{po_number}")
        return False


def pick_delivery_date(po_page, target):
    """日付ピッカーを開いて、人がやるのと同じように日付セルをクリックする。
    （直接文字を入れるだけだと、ピッカーが値を消してしまうため）"""
    po_page.click("#est-delivery")
    picker = po_page.locator(".datepicker-days").first
    picker.wait_for(state="visible", timeout=5000)
    today = now_la()
    if (target.year, target.month) != (today.year, today.month):
        picker.locator("th.next").click()  # 翌月へ
    picker.locator("td.day:not(.old):not(.new)").filter(
        has_text=re.compile(rf"^{target.day}$")
    ).first.click()


def copy_price_and_check(po_page, stamp):
    """Copy Price を押し、単価が Max Price を超えている行を一覧にする。"""
    po_page.click("#copy-from-last")
    po_page.wait_for_load_state("networkidle")
    po_page.wait_for_timeout(1500)

    # Max Budget 超過のとき「XXX exceeds Max Budget」という警告ポップアップ(SweetAlert)が
    # 商品ごとに出る。文面を記録して OK で閉じる（OK を押さないと次の操作ができない）
    popup_titles = []
    for _ in range(200):
        popup = po_page.locator(".swal2-popup:visible").first
        if popup.count() == 0:
            break
        title = popup.locator(".swal2-title").first.inner_text().strip()
        popup_titles.append(title)
        popup.locator(".swal2-confirm").first.click()
        po_page.wait_for_timeout(600)
    for t in popup_titles:
        print(f"  警告ポップアップ: {t}")

    # Copy Price で画面が再読み込みされても、入力内容が残っているか確認する
    kept = {
        "order": po_page.input_value("#order-number"),
        "delivery": po_page.input_value("#est-delivery"),
        "supplier": po_page.locator("#suppliers-id option:checked").inner_text(),
    }
    print("Copy Price後の入力内容:", kept)

    lines = po_page.evaluate(CHECK_LINES_JS)
    over = [x for x in lines if x["over"] or x["red"]]
    print(f"商品行: {len(lines)} / Max Price超過または赤表示: {len(over)}")
    for x in over:
        print(f"  ! {x['code']}  単価={x['price']}  Max={x['max']}  数量={x['qty']}"
              f"{'  (赤表示)' if x['red'] else ''}")

    # 警告メッセージ（アラート・ポップアップ等）が出ていれば文面を表示する
    warns = po_page.evaluate("""() => [...document.querySelectorAll(
        '.alert, .flash, .message, .error, .warning-message, [role=alert], .toast, .swal2-popup, .modal.show')]
        .filter(e => e.getBoundingClientRect().width > 0)
        .map(e => e.innerText.trim().slice(0, 200)).filter(Boolean)""")
    for t in warns:
        print("  警告表示:", t)

    csv_path = OUT_DIR / f"{stamp}_po_lines.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["product_code", "qty", "price", "max_price", "over_max", "red", "row_class", "price_class"])
        for x in lines:
            w.writerow([x["code"], x["qty"], x["price"], x["max"], x["over"], x["red"],
                        x["rowClass"], x["priceClass"]])
    print(f"PO明細: {csv_path}")
    shot = OUT_DIR / f"{stamp}_po_add.png"
    po_page.screenshot(path=str(shot), full_page=True)
    print(f"スクリーンショット: {shot}")
    # ケースを作る対象は「単価がMax超過」または「input-error（赤）」の行だけ（誤検知を避ける）
    return [x for x in lines if x["over"] or x["err"]]


# 商品ページの Related Skus 表から Shop 名の一覧を取る
SHOPS_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const t of document.querySelectorAll('table')) {
    const ths = [...t.querySelectorAll('thead th, tr:first-child th')];
    const heads = ths.map(th => norm(th.innerText));
    const sku = heads.indexOf('sku'), shop = heads.indexOf('shop');
    if (sku < 0 || shop < 0) continue;
    const x = th => { const r = th.getBoundingClientRect(); return (r.left + r.right) / 2; };
    const sx = x(ths[shop]);
    return [...t.querySelectorAll('tbody tr')].map(tr => {
      const td = [...tr.children].find(c => { const r = c.getBoundingClientRect();
        return r.width > 0 && r.left <= sx && sx <= r.right; });
      return td ? td.innerText.trim() : '';
    }).filter(Boolean);
  }
  return null;
}
"""


# 商品ページの「Case Orders」表（Reason / Date）を読む。例: "Change Price", "6/22/26, 2:00 AM"
CASES_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const t of document.querySelectorAll('table')) {
    const ths = [...t.querySelectorAll('thead th, tr:first-child th')];
    const heads = ths.map(th => norm(th.innerText));
    const reason = heads.indexOf('reason'), date = heads.indexOf('date');
    if (reason < 0 || date < 0) continue;
    const cx = th => { const r = th.getBoundingClientRect(); return (r.left + r.right) / 2; };
    const at = (tr, x) => [...tr.children].find(c => { const r = c.getBoundingClientRect();
      return r.width > 0 && r.left <= x && x <= r.right; });
    const rx = cx(ths[reason]), dx = cx(ths[date]);
    return [...t.querySelectorAll('tbody tr')].map(tr => ({
      reason: (at(tr, rx) || {innerText: ''}).innerText.trim(),
      date: (at(tr, dx) || {innerText: ''}).innerText.trim(),
    }));
  }
  return [];
}
"""


def recent_same_case(cases):
    """直近 RECENT_CASE_DAYS 日以内に同じ『Change Price』ケースがあれば、その日付文字列を返す。"""
    limit = now_la() - timedelta(days=RECENT_CASE_DAYS)
    for c in cases:
        if c["reason"].lower() != "change price":
            continue
        try:
            d = datetime.strptime(c["date"], "%m/%d/%y, %I:%M %p")
        except ValueError:
            print(f"  ! ケース日付を読めませんでした: {c['date']!r}（重複とみなして作成を見送ります）")
            return c["date"]
        if d >= limit:
            return c["date"]
    return None


def shop_to_assignee(shop):
    """Shop 名 → Assigned To の選択肢。分からなければ None。"""
    s = shop.lower()
    if "yahoo" in s or "楽天" in s or "rakuten" in s:
        return "Rakuten/Yahoo (Mkt)"
    if "california mart" in s:
        return "CA mart (Mkt)"
    if "dash" in s or "shop la" in s:
        return "Amazon (Mkt)"
    return None


def create_change_price_case(context, item, auto_submit, stamp):
    """超過した商品の『Change Price』ケースを作り、SUBMITを押す（SUBMITだけ。他の操作はしない）。"""
    m = re.search(r"/products/view/(\d+)", item["href"] or "")
    if not m:
        print(f"  {item['code']}: 商品ページのリンクが見つからないためスキップ")
        return "skip_no_link", "商品ページのリンクが見つからない"
    pid = m.group(1)
    page = context.new_page()
    page.goto(f"https://{DOMAIN}/products/view/{pid}", wait_until="networkidle")
    dup = recent_same_case(page.evaluate(CASES_JS))
    if dup:
        print(f"  {item['code']}: {RECENT_CASE_DAYS}日以内に同じChange Priceケースがあるため作成しません（{dup}）")
        page.close()
        return "skip_duplicate", f"{RECENT_CASE_DAYS}日以内に同じケースあり（{dup}）"
    shops = page.evaluate(SHOPS_JS) or []
    assignees, unknown = [], []
    for sh in shops:
        a = shop_to_assignee(sh)
        if a is None:
            unknown.append(sh)
        elif a not in assignees:
            assignees.append(a)
    print(f"  {item['code']}: Related Skusの店舗={shops} → Assigned To={assignees}")
    if unknown:
        print(f"  ! 振り分けルールにない店舗があります: {unknown}（この店舗分は担当に入れていません）")
    if not assignees:
        print(f"  {item['code']}: 担当を決められないため、ケースは作りません。手動で対応してください")
        page.close()
        return "skip_no_assignee", f"担当を決められない（店舗: {shops}）"

    page.goto(f"https://{DOMAIN}/case-orders/add?product_id={pid}&p_code={item['code']}",
              wait_until="networkidle")
    page.locator("select", has=page.locator("option", has_text="Awaiting Customer Response")
                 ).select_option(label="New")
    page.locator("select", has=page.locator("option", has_text="Re-Stocked")
                 ).select_option(label="Change Price")
    # Assigned To は複数選択。選択肢のうち Amazon / CA mart / Rakuten-Yahoo を含む select を探す
    assign_sel = page.locator("select[multiple]", has=page.locator("option", has_text="Amazon (Mkt)"))
    assign_sel.select_option(label=assignees)

    desc = page.locator("textarea:visible").first
    text = desc.input_value()
    price = item["price"]
    if re.search(r"Purchase price:", text):
        text = re.sub(r"Purchase price:[^\n]*", f"Purchase price: {price}", text, count=1)
    else:
        text = f"Purchase price: {price}\n" + text
    desc.fill(text)

    shot = OUT_DIR / f"{stamp}_case_{item['code']}.png"
    page.screenshot(path=str(shot), full_page=True)
    print(f"  ケース入力内容: {shot}")

    if not auto_submit:
        page.bring_to_front()
        ans = input(f"  ★ {item['code']} のケースを作成します。画面を確認して Enter で SUBMIT"
                    f"（このケースだけ作らないなら n と入力して Enter）...")
        if ans.strip().lower() == "n":
            print(f"  {item['code']}: ユーザー操作によりケースを作成しませんでした")
            page.close()
            return "skip_user", "確認画面で n を入力"
    page.click('button:has-text("SUBMIT"), input[type=submit][value*="SUBMIT" i]')
    page.wait_for_load_state("networkidle")
    print(f"  {item['code']}: ケースをSUBMITしました → {page.url.split('?')[0]}")
    page.screenshot(path=str(OUT_DIR / f"{stamp}_case_{item['code']}_done.png"), full_page=True)
    page.close()
    detail = f"Assigned To={', '.join(assignees)} / Purchase price={price}"
    if unknown:
        detail += f" / 未対応の店舗={unknown}"
    return "created", detail


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify_chatwork(f"⚠ 想定外のエラーで止まりました（{type(e).__name__}）。"
                        f"PO が作られているか画面で確認してください。")
        raise
