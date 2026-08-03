"""
「楽天在庫&価格チェック」スプレッドシート（ASINありシート）で「⚠️ 仕入不可」と
判定された商品を、楽天・Yahoo双方で自動的に停止する。

処理の流れ:
  1. ASINありシートを読み込み、E列（在庫チェック）が「⚠️ 仕入不可」かつ
     H列（対応済み）が空の行を対象にする
  2. 対象の商品管理番号（A列）ごとに、case_orders_auto_close.pyのrakuten_hide/
     yahoo_closeをそのまま再利用して、楽天はhideItem=true、YahooはsetStock=0にする
     （このシートには店舗名が無いため、両モールとも登録されている全店舗を試す）
  3. 成功したらH列に処理日時を書き込み、以後の実行では対象から除外する
     （1つでも失敗したら空のまま残し、次回再挑戦させる）
  4. 実行結果を「自動Close_ログ」タブ（case_orders_auto_close.pyと共通）に追記する

楽天・Yahooの停止方式やSKU接尾辞の扱いはcase_orders_auto_close.pyと全く同じ
（詳細はそちらのdocstring参照）。このスクリプトはPlaywrightを使わず、
シート読み書きとモールAPI呼び出しのみで完結する。
"""

import os
import sys
from datetime import datetime, timezone, timedelta

import gspread

from case_orders_auto_close import (
    DRY_RUN,
    JST,
    YAHOO_SUFFIXES,
    get_spreadsheet,
    get_yahoo_access_token,
    rakuten_hide,
    yahoo_close,
    append_log,
)

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
# テスト用：指定した場合、この商品管理番号だけを対象にする（カンマ区切りで複数可）
ONLY_ITEM_NUMBERS = {
    s.strip() for s in os.environ.get("ONLY_ITEM_NUMBERS", "").split(",") if s.strip()
}

SHORTAGE_SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHORTAGE_SHEET_NAME = "ASINあり"

COL_ITEM_NUMBER = 0     # A列：商品管理番号
COL_STOCK_CHECK = 4     # E列：在庫チェック
COL_DONE = 7             # H列：対応済み（このスクリプトが書き込む）
SHORTAGE_LABEL = "⚠️ 仕入不可"


def get_shortage_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    import json
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHORTAGE_SPREADSHEET_ID).worksheet(SHORTAGE_SHEET_NAME)


def find_targets(ws) -> list:
    """(行番号, 商品管理番号) のリストを返す。行番号は1始まり（シート上の実際の行）。"""
    values = ws.get_all_values()
    targets = []
    for i, row in enumerate(values[1:], start=2):  # 1行目はヘッダー
        if len(row) <= COL_STOCK_CHECK:
            continue
        if row[COL_STOCK_CHECK].strip() != SHORTAGE_LABEL:
            continue
        done = row[COL_DONE].strip() if len(row) > COL_DONE else ""
        if done:
            continue
        item_number = row[COL_ITEM_NUMBER].strip()
        if not item_number:
            continue
        if ONLY_ITEM_NUMBERS and item_number not in ONLY_ITEM_NUMBERS:
            continue
        targets.append((i, item_number))
    return targets


def main():
    print("=== 仕入不可商品 自動Close 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：モール側もシート側も一切変更しません")

    spreadsheet = get_spreadsheet()
    yahoo_token = get_yahoo_access_token(spreadsheet)
    shortage_ws = get_shortage_sheet()

    header = shortage_ws.row_values(1)
    if len(header) <= COL_DONE or header[COL_DONE].strip() != "対応済み":
        shortage_ws.update_cell(1, COL_DONE + 1, "対応済み")
        print("H列にヘッダー「対応済み」を設定しました。")

    targets = find_targets(shortage_ws)
    print(f"対象（仕入不可・未対応）: {len(targets)}件")
    if ONLY_ITEM_NUMBERS:
        print(f"ONLY_ITEM_NUMBERS指定により絞り込み済み（対象: {sorted(ONLY_ITEM_NUMBERS)}）")

    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []
    processed = 0

    for row_num, item_number in targets[:MAX_PER_RUN]:
        print(f"\n--- {item_number}（{row_num}行目） ---")
        all_ok = True

        for store_name, message, ok in rakuten_hide(item_number):
            print(f"    [楽天] {store_name}: {message}")
            log_rows.append([now, "-", "仕入不可自動Close", "楽天", store_name, item_number, message])
            if not ok:
                all_ok = False

        candidates = [item_number + suffix for suffix in YAHOO_SUFFIXES]
        for store_name, message, ok in yahoo_close(yahoo_token, candidates):
            print(f"    [Yahoo] {store_name}: {message}")
            log_rows.append([now, "-", "仕入不可自動Close", "Yahoo", store_name, item_number, message])
            if not ok:
                all_ok = False

        if not all_ok:
            print("  ⚠️ 失敗があったため、未対応のまま残します（次回再挑戦）。")
            continue

        processed += 1
        if DRY_RUN:
            print("  【DRY RUN】本番ならここでH列に対応済みを記録します。")
            continue

        try:
            shortage_ws.update_cell(row_num, COL_DONE + 1, now)  # gspreadは1始まり列番号
            print(f"  ✅ H列に対応済み（{now}）を記録しました。")
        except Exception as e:
            print(f"  ⚠️ シート更新に失敗しました（モール側は停止済み）: {e}")

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを「自動Close_ログ」タブに{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print(f"\n=== 完了: 処理{processed}件 / 対象外{len(targets) - min(len(targets), MAX_PER_RUN)}件（次回以降） ===")
    print("=== 仕入不可商品 自動Close 完了 ===")


if __name__ == "__main__":
    main()
