import os
import csv
import json
import time
import tempfile
import requests
import gspread
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from urllib.parse import quote
from status_sheet import update_status, write_rows_in_batches, build_row, merge_row, group_consecutive

# ===== 設定（環境変数から読み込み） =====
DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
SO_SEARCH_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/so-heads"
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
# SOデータの書き込み先（専用スプレッドシート）。未設定なら従来のSPREADSHEET_IDを使う。
PO_SO_SPREADSHEET_ID = os.environ.get("PO_SO_SPREADSHEET_ID") or SPREADSHEET_ID

DATE_COLUMN = "created_time"
KEY_COLUMNS = ["order_number", "sku"]


def _fetch_so_range(page, context, start_date, end_date):
    """指定期間のCSVを1回ダウンロードし、(status_code, 行リスト) を返す。

    行リストは [ヘッダー行, データ行...]。失敗時(200以外)は (status, None)。
    """
    page.goto(SO_SEARCH_URL, wait_until="networkidle")
    page.wait_for_timeout(2000)

    start_input = page.locator('input[name="start_date"], input[placeholder*="Start"], input[id*="start"]').first
    start_input.fill(start_date)
    end_input = page.locator('input[name="end_date"], input[placeholder*="End"], input[id*="end"]').first
    end_input.fill(end_date)

    page.click('button:has-text("Search"), input[value="Search"]')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    download_link = page.locator('a:has-text("Download"), button:has-text("Download")').first
    href = download_link.get_attribute("href")
    if not href:
        raise Exception("Download リンクが見つかりませんでした")
    download_url = f"https://{DOMAIN}{href}" if href.startswith("/") else href

    cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
    response = requests.get(
        download_url,
        cookies=cookie_dict,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
    )
    if response.status_code != 200:
        return response.status_code, None

    text = response.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    return 200, rows


def _fetch_so_range_recursive(page, context, start, end):
    """[start, end]（datetime, 両端含む）のデータ行を取得。

    500等で失敗した場合は期間を半分に分割して再試行し、サーバーの期間上限に自動適応する。
    戻り値: (ヘッダー行 or None, データ行のリスト)
    """
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    print(f"  取得中: {start_str} 〜 {end_str}")

    # 通信が一時的に切れることがあるため、例外時は数回リトライ
    status, rows = None, None
    for attempt in range(1, 5):  # 最大4回
        try:
            status, rows = _fetch_so_range(page, context, start_str, end_str)
            break
        except Exception as e:
            if attempt == 4:
                raise
            wait = 15 * attempt
            print(f"  通信エラー、{wait}秒後に再試行 ({attempt}/4): {e}")
            time.sleep(wait)

    if status == 200:
        if not rows:
            return None, []
        return rows[0], rows[1:]

    # 失敗：1日まで縮めても失敗なら諦める
    if start >= end:
        raise Exception(f"ダウンロード失敗: status={status}（期間 {start_str}）")

    half = (end - start).days // 2
    mid = start + timedelta(days=half)
    print(f"  status={status} のため期間を分割します: 〜{mid.strftime('%Y-%m-%d')} と {(mid + timedelta(days=1)).strftime('%Y-%m-%d')}〜")
    left_header, left_rows = _fetch_so_range_recursive(page, context, start, mid)
    right_header, right_rows = _fetch_so_range_recursive(page, context, mid + timedelta(days=1), end)
    return (left_header or right_header), (left_rows + right_rows)


def download_so_csv(backfill_months=None):
    """SO検索画面からCSVをダウンロードする

    backfill_months を指定すると、その月数前〜当日の期間でダウンロードする（初回一括取り込み用）。
    期間が長くサーバーが500を返す場合は自動で期間を分割して取得する。
    指定がなければ通常どおり当日分のみ。
    """
    today = date.today()
    if backfill_months:
        start = today - relativedelta(months=backfill_months)
        print(f"★バックフィルモード：過去 {backfill_months} ヶ月分を取得します")
    else:
        start = today
    print(f"検索期間: {start.strftime('%Y-%m-%d')} 〜 {today.strftime('%Y-%m-%d')}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # ===== ステップ1：Basic認証 =====
        print("Basic認証付きでトップページを開いています...")
        page.goto(LOGIN_URL, wait_until="networkidle")

        # ===== ステップ2：Loginボタンをクリック =====
        print("Loginボタンをクリックしています...")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")

        # ===== ステップ3：フォームログイン =====
        print("フォームログインを処理しています...")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")
        print("ログイン完了")

        # ===== 期間ごとにダウンロード（必要に応じて自動分割）=====
        print("CSVをダウンロードしています...")
        header, data_rows = _fetch_so_range_recursive(page, context, start, today)

        browser.close()

    if header is None:
        print("該当データがありませんでした（空）。")
        header = []
        data_rows = []

    # バックフィルで期間分割した場合、境界での重複を念のため除去（order_number + sku）
    if header:
        try:
            o_i = header.index("order_number")
            s_i = header.index("sku")
            seen = set()
            deduped = []
            for row in data_rows:
                if len(row) <= max(o_i, s_i):
                    deduped.append(row)
                    continue
                key = (row[o_i], row[s_i])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(row)
            if len(deduped) != len(data_rows):
                print(f"分割境界の重複を {len(data_rows) - len(deduped)}行 除去しました")
            data_rows = deduped
        except ValueError:
            pass

    tmp_path = tempfile.mktemp(suffix=".csv")
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if header:
            writer.writerow(header)
        writer.writerows(data_rows)
    print(f"CSVを保存しました: {tmp_path}（データ {len(data_rows)}行）")
    return tmp_path


def parse_so_date(date_str):
    """created_time の文字列をdatetimeに変換する"""
    if not date_str:
        return None
    try:
        # 例: "5/22/26, 6:43 AM"
        return datetime.strptime(date_str.strip(), "%m/%d/%y, %I:%M %p")
    except Exception:
        try:
            return datetime.strptime(date_str.strip().split(",")[0], "%m/%d/%y")
        except Exception:
            return None


def update_so_sheet(csv_path, backfill=False):
    """SOタブにCSVデータを反映する（重複スキップ・差分更新・6ヶ月超削除）

    backfill=True の場合は、既存データを全消去してCSVの内容で丸ごと入れ替える（初回一括取り込み用）。
    重複判定キーは order_number + sku（1注文に複数商品行があるため両方で一意になる）。
    """
    print("Googleスプレッドシート（SOタブ）を更新しています...")

    credentials_info = json.loads(GOOGLE_CREDENTIALS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
    gc = gspread.authorize(credentials)

    spreadsheet = gc.open_by_key(PO_SO_SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet("SO")
    except gspread.WorksheetNotFound:
        print("「SO」タブが無いため新規作成します")
        worksheet = spreadsheet.add_worksheet(title="SO", rows=100, cols=40)

    # 新しいCSVを読み込む
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        new_rows = list(reader)

    if not new_rows:
        print("CSVが空です。処理をスキップします。")
        return

    new_headers = new_rows[0]
    new_data = new_rows[1:]
    print(f"新規データ: {len(new_data)}行")

    # ===== バックフィルモード：既存を全消去してCSVで丸ごと入れ替え =====
    if backfill:
        clean_data = [row for row in new_data if any(row)]
        write_rows_in_batches(worksheet, [new_headers] + clean_data)
        print(f"★バックフィル：ヘッダー + {len(clean_data)}行 で入れ替えました")
        update_status("SO")
        return

    try:
        order_idx_new = new_headers.index("order_number")
        sku_idx_new = new_headers.index("sku")
    except ValueError as e:
        print(f"CSVの列名が見つかりません: {e}")
        return

    # 既存データを取得
    existing_data = worksheet.get_all_values()

    # シートが空の場合：ヘッダーごと書き込む
    if not existing_data or not any(cell for row in existing_data for cell in row):
        print("シートが空のため、全データを書き込みます...")
        write_rows_in_batches(worksheet, [new_headers] + new_data)
        return

    existing_headers = existing_data[0]
    existing_rows = existing_data[1:]

    # 列インデックスを取得
    try:
        date_idx_ex = existing_headers.index(DATE_COLUMN)
        order_idx_ex = existing_headers.index("order_number")
        sku_idx_ex = existing_headers.index("sku")
    except ValueError as e:
        print(f"既存シートの列名が見つかりません: {e}")
        return

    # 既存行を「キー → シート行番号(1始まり)」でマップ化し、同時に6ヶ月超の古い行も収集
    # （シート全体を書き換えず、追記・部分更新・部分削除だけで済ませる）
    six_months_ago = datetime.today() - relativedelta(months=6)
    width = len(existing_headers)
    key_to_rownum = {}
    existing_by_rownum = {}
    old_rownums = []
    for offset, row in enumerate(existing_rows):
        rownum = offset + 2  # 1行目はヘッダーなので +2
        if len(row) > max(order_idx_ex, sku_idx_ex):
            key_to_rownum[(row[order_idx_ex], row[sku_idx_ex])] = rownum
        existing_by_rownum[rownum] = row
        d = parse_so_date(row[date_idx_ex]) if len(row) > date_idx_ex else None
        if d is not None and d < six_months_ago:
            old_rownums.append(rownum)

    # 当日分を「新規追加」「変更あり既存」に振り分け
    to_append = []
    to_update = []   # (rownum, 行データ)
    skipped = 0
    seen_new = set()
    for new_row in new_data:
        if not any(new_row):
            continue
        if len(new_row) <= max(order_idx_new, sku_idx_new):
            continue
        key = (new_row[order_idx_new], new_row[sku_idx_new])
        if key in seen_new:
            continue  # 同じCSV内の重複は1回だけ扱う
        seen_new.add(key)

        if key not in key_to_rownum:
            to_append.append(build_row(new_row, new_headers, existing_headers))
        else:
            rownum = key_to_rownum[key]
            merged, changed = merge_row(existing_by_rownum[rownum], new_row, existing_headers, new_headers, width)
            if changed:
                to_update.append((rownum, merged))
            else:
                skipped += 1

    # 1) 新規行を末尾に追記（追記だけなので軽い）
    if to_append:
        worksheet.append_rows(to_append, value_input_option="RAW")
    print(f"新規追加: {len(to_append)}行 / 差分更新: {len(to_update)}行 / 変更なし: {skipped}行")

    # 2) 変更のあった既存行だけを1回のバッチで更新
    if to_update:
        batch = [{"range": f"A{rownum}", "values": [row]} for rownum, row in to_update]
        worksheet.batch_update(batch, value_input_option="RAW")

    # 3) 6ヶ月超の古い行だけ削除（連続区間にまとめ、下から消して行番号のズレを防ぐ）
    if old_rownums:
        ranges = group_consecutive(sorted(old_rownums))
        for start, end in sorted(ranges, reverse=True):
            worksheet.delete_rows(start, end)
        print(f"6ヶ月超の古いデータを {len(old_rownums)}行 削除しました（{len(ranges)}区間）")

    update_status("SO")


if __name__ == "__main__":
    print("=== Daily SO CSV → Google Sheets (SOタブ) ===")
    backfill_raw = os.environ.get("BACKFILL_MONTHS", "").strip()
    backfill_months = int(backfill_raw) if backfill_raw.isdigit() and int(backfill_raw) > 0 else None
    csv_path = download_so_csv(backfill_months)
    update_so_sheet(csv_path, backfill=bool(backfill_months))
    print("=== 完了 ===")
