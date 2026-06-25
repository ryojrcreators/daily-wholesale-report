import json
import os
import time
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

JST = timezone(timedelta(hours=9))
PST = timezone(timedelta(hours=-8))

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
STATUS_SHEET_NAME = "Status"


def write_rows_in_batches(worksheet, data, batch_rows=10000, retries=5):
    """シート全体を data で上書きする。

    大量データ（数万〜十万行）を1回のリクエストで送ると Sheets API が
    500/413 を返すため、行を分割して複数回に分けて書き込む。
    事前に worksheet.clear() を呼ぶ必要はない（resize で行数を合わせる）。
    """
    n_rows = len(data)
    n_cols = max((len(r) for r in data), default=1)
    # 行ごとの列数を揃える（短い行の末尾に古い値が残らないようにする）
    data = [row + [""] * (n_cols - len(row)) for row in data]

    worksheet.resize(rows=max(n_rows, 1), cols=max(n_cols, 1))

    start = 0
    while start < n_rows:
        chunk = data[start:start + batch_rows]
        rng = f"A{start + 1}"
        for attempt in range(retries):
            try:
                worksheet.update(rng, chunk)
                break
            except gspread.exceptions.APIError as e:
                if attempt == retries - 1:
                    raise
                wait = 5 * (attempt + 1)
                print(f"  書き込みリトライ ({attempt + 1}/{retries}) {wait}秒待機: {e}")
                time.sleep(wait)
        start += len(chunk)
    print(f"  合計 {n_rows}行を {batch_rows}行ずつ書き込みました")


def build_row(src_row, src_headers, dst_headers):
    """src_row を dst_headers（既存シートの列順）に並べ替えた行を作る。"""
    out = []
    for col in dst_headers:
        if col in src_headers:
            i = src_headers.index(col)
            out.append(src_row[i] if i < len(src_row) else "")
        else:
            out.append("")
    return out


def merge_row(ex_row, new_row, ex_headers, new_headers, width):
    """既存行に新しい値を上書きマージ。(マージ後の行, 変更があったか) を返す。"""
    merged = list(ex_row) + [""] * (width - len(ex_row))
    changed = False
    for col in new_headers:
        if col not in ex_headers:
            continue
        ni = new_headers.index(col)
        ei = ex_headers.index(col)
        new_val = new_row[ni] if ni < len(new_row) else ""
        if new_val and new_val != merged[ei]:
            merged[ei] = new_val
            changed = True
    return merged, changed


def group_consecutive(sorted_nums):
    """ソート済みの行番号リストを連続区間 [(start, end), ...] にまとめる。"""
    ranges = []
    for n in sorted_nums:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return [(s, e) for s, e in ranges]


def _get_spreadsheet():
    credentials_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
    gc = gspread.authorize(credentials)
    return gc.open_by_key(SPREADSHEET_ID)


def update_status(tab_name: str):
    """Statusタブの該当行にJST・PST更新日時を書き込む"""
    now_utc = datetime.now(timezone.utc)
    jst_str = now_utc.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    pst_str = now_utc.astimezone(PST).strftime("%Y/%m/%d %H:%M")

    spreadsheet = _get_spreadsheet()
    ws = spreadsheet.worksheet(STATUS_SHEET_NAME)

    data = ws.get_all_values()

    # ヘッダー行がなければ作成
    if not data or data[0] != ["タブ名", "最終更新 (JST)", "最終更新 (PST)"]:
        ws.clear()
        ws.update([["タブ名", "最終更新 (JST)", "最終更新 (PST)"]])
        data = [["タブ名", "最終更新 (JST)", "最終更新 (PST)"]]

    # 既存行を検索
    for i, row in enumerate(data[1:], start=2):
        if row and row[0] == tab_name:
            ws.update(f"A{i}:C{i}", [[tab_name, jst_str, pst_str]])
            print(f"  Statusタブ更新: {tab_name} → JST {jst_str} / PST {pst_str}")
            return

    # 該当行がなければ末尾に追加
    ws.append_row([tab_name, jst_str, pst_str])
    print(f"  Statusタブ追加: {tab_name} → JST {jst_str} / PST {pst_str}")
