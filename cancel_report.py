"""月次のキャンセル集計をスプレッドシート「店舗別パフォーマンス」タブへ書き込む。

これまで「Cancel集計マクロ.xlsm」で手作業していた処理の置き換え。

処理の流れ:
  1. 社内システムにログインし、対象月のキャンセルCSVを2種類ダウンロードする
       全体のキャンセル     … has_cs_request なし
       店舗都合のキャンセル … has_cs_request=1
  2. Excelマクロと同じロジックで店舗ごとに集計する
       件数      = 店舗ごとの order_number のユニーク数（明細行数ではない）
       Price合計 = Σ(qty × price) を小数点以下切り上げ
  3. スプレッドシートの該当月・該当店舗の行へ書き込む

書き込むのは「返金額 / キャンセル件数 / 弊社都合キャンセル金額 / 弊社都合キャンセル件数」の
4列だけ。パーセントの2列はシート側の数式で計算されるため、値を入れて数式を壊さないよう
意図的に触らない。

安全策:
  - 既定はドライラン。実際に書き込むのは WRITE=1 のときだけ。
  - すでに値が入っているセルは書き換えない（OVERWRITE=1 で明示的に許可した場合のみ上書き）。
  - CSVの取得に失敗したら、シートには一切触れずに終了する。

環境変数:
  APP_DOMAIN / LOGIN_ID_1 / LOGIN_PASS_1 / LOGIN_ID_2 / LOGIN_PASS_2  社内システム
  GOOGLE_CREDENTIALS          サービスアカウントのJSON
  SALES_KPI_SPREADSHEET_ID    書き込み先スプレッドシートID
  TARGET_MONTH                対象月 "2026-07"（未指定なら前月）
  SHEET_NAME                  タブ名（既定「店舗別パフォーマンス」）
  WRITE=1                     実際に書き込む
  OVERWRITE=1                 既存の値も上書きする
"""
import csv
import io
import json
import math
import os
from calendar import monthrange
from collections import defaultdict
from datetime import date
from urllib.parse import quote

import gspread
import requests
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# ===== 社内システム =====
DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_URL = (f"https://{quote(LOGIN_ID_1, safe='')}:"
             f"{quote(LOGIN_PASS_1, safe='')}@{DOMAIN}/")

# ===== スプレッドシート =====
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
# Secretへのコピペで前後に空白や改行が混ざることがあるため取り除く
SPREADSHEET_ID = os.environ["SALES_KPI_SPREADSHEET_ID"].strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "店舗別パフォーマンス").strip()

WRITE = os.environ.get("WRITE") == "1"
OVERWRITE = os.environ.get("OVERWRITE") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# CSVのshop_name → シートC列の店舗名
STORE_MAP = {
    "アメリカーナ": "Americana",
    "Founder": "Founder",
    "American Kitchen": "American Kitchen",
    "Meta Store": "Meta",
}

# 書き込む列（シートの見出し名で探す）
# ※「返金額（％）」「弊社都合キャンセル金額（％）」はシートの数式で計算されるため書き込まない
COL_REFUND_AMOUNT = "返金額"
COL_CANCEL_COUNT = "キャンセル件数"
COL_OWN_AMOUNT = "弊社都合キャンセル金額"
COL_OWN_COUNT = "弊社都合キャンセル件数"
COL_STORE = "店舗名"


def target_month():
    """対象月を (年, 月) で返す。未指定なら前月。"""
    raw = os.environ.get("TARGET_MONTH", "").strip()
    if raw:
        y, m = raw.split("-")
        return int(y), int(m)
    today = date.today()
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


# ---------------------------------------------------------------- CSV取得

def login(playwright):
    """Basic認証 → フォームログインを済ませたブラウザコンテキストを返す"""
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1800, "height": 900},
        user_agent=UA,
    )
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    return browser, context


def build_query(start, end, shop_fault_only):
    """キャンセル検索のクエリ文字列を組み立てる（SKU店舗ID 1と15＝楽天・Yahoo、ステータス7＝キャンセル）"""
    query = (
        f"start_date={start}"
        f"&end_date={end}"
        "&SoHeads%5Bsku_shop_id%5D%5B%5D=1"
        "&SoHeads%5Bsku_shop_id%5D%5B%5D=15"
        "&SoHeads%5Bso_status_id%5D%5B%5D=7"
        "&has_credit=0"
        "&SoHeads%5Bresend%5D=0"
    )
    return query + "&has_cs_request=1" if shop_fault_only else query


def download_csv(context, query, label):
    """/sales/download を直接叩いてCSVを取得する（検索画面を開く必要はない）"""
    url = f"https://{DOMAIN}/sales/download?{query}"
    cookies = {c["name"]: c["value"] for c in context.cookies()}
    res = requests.get(
        url,
        cookies=cookies,
        headers={"User-Agent": UA},
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
        timeout=300,
    )
    if res.status_code != 200:
        raise SystemExit(f"[{label}] ダウンロード失敗: status={res.status_code}")

    text = res.content.decode("utf-8-sig")
    if "<html" in text[:500].lower():
        raise SystemExit(f"[{label}] CSVではなくHTMLが返っています（ログイン切れの可能性）")

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise SystemExit(f"[{label}] 0件でした。シートには書き込まずに終了します")
    print(f"[{label}] 明細 {len(rows)}行")
    return rows


def aggregate(rows):
    """Excelマクロの AggregateSheet と同じ集計。{店舗名: (件数, 金額)} を返す"""
    orders = defaultdict(set)
    totals = defaultdict(float)
    for r in rows:
        shop = (r.get("shop_name") or "").strip()
        if not shop:
            continue
        orders[shop].add(r.get("order_number", ""))
        try:
            qty = float((r.get("qty") or "0").replace(",", ""))
            price = float((r.get("price") or "0").replace(",", ""))
        except ValueError:
            qty, price = 0.0, 0.0
        totals[shop] += qty * price
    return {shop: (len(ids), math.ceil(totals[shop])) for shop, ids in orders.items()}


# ---------------------------------------------------------------- シート

def open_sheet():
    if not SPREADSHEET_ID:
        raise SystemExit(
            "SALES_KPI_SPREADSHEET_ID が空です。"
            "GitHub Secrets に書き込み先スプレッドシートのIDを登録してください"
        )

    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)

    try:
        book = client.open_by_key(SPREADSHEET_ID)
    except gspread.exceptions.SpreadsheetNotFound:
        raise SystemExit(
            f"スプレッドシート（ID: {SPREADSHEET_ID}）を開けませんでした。\n"
            "  Googleは権限のないファイルも「見つかりません」と返すため、原因は次のどちらかです:\n"
            "   1. Secret の SALES_KPI_SPREADSHEET_ID が間違っている\n"
            "   2. サービスアカウントにこのファイルが共有されていない\n"
            "  → スプレッドシートの共有設定で、サービスアカウントのメールアドレスを"
            "「編集者」として追加してください"
        )

    try:
        return book.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        names = [w.title for w in book.worksheets()]
        raise SystemExit(
            f"「{SHEET_NAME}」というタブが見つかりません。\n"
            f"  このファイルのタブ一覧: {names}\n"
            "  → 環境変数 SHEET_NAME で正しいタブ名を指定してください"
        )


def normalize(text):
    """見出し比較用に、空白・改行を取り除く"""
    return "".join(str(text).split())


def find_header(grid):
    """見出し行を探し、(行インデックス, 見出しの並び) を返す。

    見出しには「弊社都合キャンセル金額（％） *全返金額に対する」のように
    注記が続くものがあるため、完全一致ではなく前方一致で探す。
    """
    for i, row in enumerate(grid):
        cells = [normalize(c) for c in row]
        if any(c.startswith(normalize(COL_OWN_COUNT)) for c in cells) and \
           any(c.startswith(normalize(COL_CANCEL_COUNT)) for c in cells):
            return i, cells
    raise SystemExit(
        f"「{SHEET_NAME}」タブに見出し行（{COL_OWN_COUNT} を含む行）が見つかりません。"
        "タブ名か列名が変わっていないか確認してください"
    )


def col_of(headers, name):
    """見出し名から列インデックスを返す（前方一致）。無ければ None"""
    key = normalize(name)
    for j, c in enumerate(headers):
        if c.startswith(key):
            return j
    return None


def find_rows(grid, header_index, store_col, month_label):
    """対象月の各店舗の行番号（1始まり）を {シート店舗名: 行番号} で返す。

    月はA列にしか入っていない（4行に1回）ため、直前の値を引き継いで判定する。
    """
    found = {}
    current = ""
    for i in range(header_index + 1, len(grid)):
        row = grid[i]
        first = row[0].strip() if row else ""
        if first:
            current = first
        if current != month_label:
            continue
        name = row[store_col].strip() if len(row) > store_col else ""
        if name:
            found[name] = i + 1  # gspreadの行番号は1始まり
    return found


def a1(col_index, row_number):
    """0始まりの列インデックスと1始まりの行番号を A1 表記にする"""
    col, name = col_index + 1, ""
    while col:
        col, rem = divmod(col - 1, 26)
        name = chr(65 + rem) + name
    return f"{name}{row_number}"


# ---------------------------------------------------------------- 本体

def main():
    year, month = target_month()
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"
    month_label = f"{year}/{month}"

    print(f"=== 対象月: {month_label}（{start} 〜 {end}）===")
    print(f"モード: {'書き込み' if WRITE else 'ドライラン（書き込みません）'}"
          f"{' / 既存値も上書き' if OVERWRITE else ''}")

    # 1) 先にシート側を確認する（権限やタブ名の問題なら、CSVを取りに行く前に止める）
    ws = open_sheet()
    grid = ws.get_all_values()
    header_index, headers = find_header(grid)
    print(f"\n見出し行: {header_index + 1}行目")

    cols = {}
    missing = []
    for name in (COL_STORE, COL_REFUND_AMOUNT, COL_CANCEL_COUNT,
                 COL_OWN_AMOUNT, COL_OWN_COUNT):
        index = col_of(headers, name)
        if index is None:
            missing.append(name)
        else:
            cols[name] = index
    if missing:
        raise SystemExit(f"見出しが見つかりません: {missing}")

    # 「店舗名」の見出しは2列にまたがっていて、実際の店舗名は右隣の列に入る
    store_col = cols[COL_STORE] + 1

    rows_by_store = find_rows(grid, header_index, store_col, month_label)
    if not rows_by_store:
        raise SystemExit(f"{month_label} の行がシートに見つかりません。先に行を用意してください")
    print("対象行: " + ", ".join(f"{k}={v}行目" for k, v in rows_by_store.items()))
    print("※ パーセントの2列はシートの数式で計算されるため書き込みません")

    # 2) CSVを取得して集計する
    with sync_playwright() as p:
        browser, context = login(p)
        print("\nログイン完了")
        all_rows = download_csv(context, build_query(start, end, False), "全体のキャンセル")
        shop_rows = download_csv(context, build_query(start, end, True), "店舗都合のキャンセル")
        browser.close()

    all_agg = aggregate(all_rows)
    shop_agg = aggregate(shop_rows)

    print("\n--- 集計結果 ---")
    for shop in STORE_MAP:
        a_count, a_amount = all_agg.get(shop, (0, 0))
        s_count, s_amount = shop_agg.get(shop, (0, 0))
        print(f"  {shop}: 全体 {a_count}件/{a_amount:,}円  "
              f"弊社都合 {s_count}件/{s_amount:,}円")
    others = sorted(set(all_agg) - set(STORE_MAP))
    if others:
        print(f"  （対象外の店舗: {', '.join(others)}）")

    # 3) 書き込む内容を組み立てる
    updates = []
    skipped = []
    print("\n--- 書き込み内容 ---")
    for csv_name, sheet_name in STORE_MAP.items():
        row_number = rows_by_store.get(sheet_name)
        if not row_number:
            print(f"  {sheet_name}: 行が見つからないためスキップ")
            continue

        current = grid[row_number - 1]
        get = lambda col: (current[cols[col]].strip() if len(current) > cols[col] else "")

        a_count, a_amount = all_agg.get(csv_name, (0, 0))
        s_count, s_amount = shop_agg.get(csv_name, (0, 0))

        values = {
            COL_REFUND_AMOUNT: a_amount,
            COL_CANCEL_COUNT: a_count,
            COL_OWN_AMOUNT: s_amount,
            COL_OWN_COUNT: s_count,
        }

        for col_name, value in values.items():
            cell = a1(cols[col_name], row_number)
            existing = get(col_name)
            if existing and not OVERWRITE:
                skipped.append(f"{sheet_name} {col_name}（{cell}）は既に「{existing}」が入っています")
                continue
            print(f"  {cell}  {sheet_name:<18}{col_name:<24}→ {value}")
            updates.append({"range": cell, "values": [[value]]})

    if skipped:
        print("\n--- 既存の値があるためスキップ ---")
        for s in skipped:
            print("  " + s)
        print("  ※ 上書きしたい場合は OVERWRITE=1 を付けて実行してください")

    if not updates:
        print("\n書き込む項目がありませんでした。")
        return

    # 4) 書き込み
    if not WRITE:
        print(f"\nドライランのため書き込みません（{len(updates)}セル）。"
              "実際に書き込むには WRITE=1 を付けて実行してください")
        return

    ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"\n{len(updates)}セルを書き込みました。")


if __name__ == "__main__":
    main()
