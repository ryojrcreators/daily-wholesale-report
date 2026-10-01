"""
Yamato Nekopos / Sagawa CDS / Sagawa USPS(ePacket) の週次出荷件数レポートを
Chatworkへ送る。毎週金曜 18:00 JST（倉庫の当日バッチが締まった後）に実行する想定。

処理の流れ:
- /manifest-batches を開く（直近約2週間分=最大30行が残っている）。この中から
  今週の月〜金に作成されたバッチだけを対象に、Yamato Nekopos・Sagawa CDSの
  pieces件数を日付ごとに合計する（この2つは100%日本向け）
- 今週の月〜金それぞれについて /sales/download?ship_date=...&ShippingCodes[ship_code_method_id][]=4
  （4=ePacket）をCSVで取得し、email列のドメインが .jp 系（marketplace.amazon.co.jp /
  *.rakuten.ne.jp 等）かどうかで日本向け/他を集計する
  （参考: 2026-09-24、Shipping Country欄と同じ精度で判定できることを実機で確認済み）
- 5日分を合計してレポート文を組み立て、Chatworkルーム(105004197)へ送信
"""

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import quote

import os
import requests
from playwright.sync_api import sync_playwright

DOMAIN = "app.jrcreators.com"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
BASE_URL = f"https://{DOMAIN}"

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "105004197"

# DRY_RUN=true の間は、集計してログに出すだけでChatwork送信は行わない
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

EPACKET_METHOD_ID = 4
YAMATO_NEKOPOS = "Yamato Nekopos"
SAGAWA_CDS = "Sagawa CDS"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


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


def get_week_mon_fri(today):
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def parse_batch_date(created_text):
    """'10/1/26, 11:45 AM' -> datetime.date(2026, 10, 1)"""
    date_part = created_text.split(",")[0].strip()
    m, d, y = date_part.split("/")
    return datetime(2000 + int(y), int(m), int(d)).date()


def collect_manifest_counts(page, monday, friday):
    """今週の月〜金について、{date: {carrier: pieces}} を返す"""
    page.goto(f"{BASE_URL}/manifest-batches", wait_until="networkidle")
    page.wait_for_timeout(500)

    rows = page.evaluate(
        """() => {
            const tables = [...document.querySelectorAll('table')];
            for (const t of tables) {
                const headerRow = t.rows[0];
                if (!headerRow) continue;
                const header = [...headerRow.cells].map(c => c.textContent.trim().toLowerCase());
                const iCarrier = header.indexOf('carrier_id');
                const iCreated = header.indexOf('created');
                let iPieces = header.indexOf('peices');
                if (iPieces < 0) iPieces = header.indexOf('pieces');
                if (iCarrier < 0 || iCreated < 0 || iPieces < 0) continue;
                const out = [];
                for (let i = 1; i < t.rows.length; i++) {
                    const cells = t.rows[i].cells;
                    if (cells.length <= Math.max(iCarrier, iCreated, iPieces)) continue;
                    out.push({
                        carrier: cells[iCarrier].textContent.trim(),
                        created: cells[iCreated].textContent.trim(),
                        pieces: cells[iPieces].textContent.trim(),
                    });
                }
                return out;
            }
            return [];
        }"""
    )
    if not rows:
        print("！manifest-batchesのテーブルが見つかりません")
        try:
            page.screenshot(path="debug_manifest.png", full_page=True)
        except Exception:
            pass
        return {}

    by_date = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["carrier"] not in (YAMATO_NEKOPOS, SAGAWA_CDS):
            continue
        d = parse_batch_date(r["created"])
        if not (monday <= d <= friday):
            continue
        pieces = int((r["pieces"] or "0").replace(",", "") or 0)
        by_date[d][r["carrier"]] += pieces
    return by_date


def is_japan_domain(email):
    domain = email.strip().split("@")[-1].lower()
    return domain.endswith(".co.jp") or "rakuten.ne.jp" in domain


def collect_epacket_split(context, target_date):
    """指定日のePacket発送を、emailドメインで日本/他に分けて件数を返す"""
    url = (
        f"{BASE_URL}/sales/download?ship_date={target_date.isoformat()}"
        f"&ShippingCodes%5Bship_code_method_id%5D%5B%5D={EPACKET_METHOD_ID}"
    )
    cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
    resp = requests.get(
        url,
        cookies=cookie_dict,
        headers={"User-Agent": USER_AGENT},
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"！ePacket CSV取得失敗 status={resp.status_code} date={target_date}")
        return 0, 0

    text = resp.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return 0, 0
    header = rows[0]
    if "email" not in header:
        print(f"！ePacket CSVにemail列がありません date={target_date}")
        return 0, 0
    i_email = header.index("email")

    jp = other = 0
    for row in rows[1:]:
        if len(row) <= i_email or not row[i_email]:
            continue
        if is_japan_domain(row[i_email]):
            jp += 1
        else:
            other += 1
    return jp, other


def build_report(monday, friday, nekopos, cds, epacket_jp, epacket_other):
    total_jp = nekopos + cds + epacket_jp

    def fmt(n):
        return f"{n:,}"

    period = (
        f"{monday.month}/{monday.day} ({WEEKDAY_JP[monday.weekday()]}) "
        f"～ {friday.month}/{friday.day} ({WEEKDAY_JP[friday.weekday()]})"
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"【出荷レポート】{period}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"・ヤマト ネコポス          : {fmt(nekopos):>6} 件",
        f"・佐川 CDS                : {fmt(cds):>6} 件",
        f"・佐川 USPS(ePacket) 日本  : {fmt(epacket_jp):>6} 件",
        f"・佐川 USPS(ePacket) 他    : {fmt(epacket_other):>6} 件",
        "────────────────────────────",
        f"■ 日本向け合計            : {fmt(total_jp):>6} 件",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def post_chatwork(message):
    resp = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data={"body": message},
        timeout=30,
    )
    print(f"Chatwork通知送信: status={resp.status_code}")


def main():
    print("=== 出荷件数 週次レポート 開始 ===")
    monday, friday = get_week_mon_fri(date.today())
    print(f"対象期間: {monday} 〜 {friday}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        login(page)

        by_date = collect_manifest_counts(page, monday, friday)
        missing_dates = [
            monday + timedelta(days=i)
            for i in range(5)
            if (monday + timedelta(days=i)) not in by_date
        ]

        nekopos_total = cds_total = epacket_jp_total = epacket_other_total = 0
        d = monday
        while d <= friday:
            carriers = by_date.get(d, {})
            nekopos = carriers.get(YAMATO_NEKOPOS, 0)
            cds = carriers.get(SAGAWA_CDS, 0)
            epacket_jp, epacket_other = collect_epacket_split(context, d)
            print(f"{d}: Nekopos={nekopos} CDS={cds} ePacket日本={epacket_jp} ePacket他={epacket_other}")
            nekopos_total += nekopos
            cds_total += cds
            epacket_jp_total += epacket_jp
            epacket_other_total += epacket_other
            d += timedelta(days=1)

        browser.close()

    message = build_report(monday, friday, nekopos_total, cds_total, epacket_jp_total, epacket_other_total)
    if missing_dates:
        missing_str = ", ".join(d.isoformat() for d in missing_dates)
        message += f"\n⚠ manifest-batchesに以下の日のNekopos/CDSデータが見つかりませんでした（0件として集計）: {missing_str}"
    print(message)
    if DRY_RUN:
        print("（DRY RUNのためChatworkへは送信していません）")
    else:
        post_chatwork(message)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
