"""
「楽天在庫&価格チェック」スプレッドシート（ASINありシート）の在庫チェック結果に応じて、
楽天・Yahooの出品を自動的にClose/再開する。

Close処理:
  1. E列（在庫チェック）が「⚠️ 仕入不可」かつH列（在庫対応済み）が空の行を対象にする
  2. case_orders_auto_close.pyのrakuten_hide/yahoo_closeを再利用し、
     楽天はhideItem=true、YahooはsetStock=0にする
     （このシートには店舗名が無いため、両モールとも登録されている全店舗を試す）
  3. 成功したらH列に処理日時を書き込み、以後の実行では対象から除外する
     （1つでも失敗したら空のまま残し、次回再挑戦させる）

再開処理（Closeの逆）:
  1. H列（在庫対応済み）に値があり、かつE列が「✅ 正常」または「🟡 3rdパーティ」に
     変わった行（＝一度は仕入不可でCloseしたが、再チェックで仕入れ可能に戻った）を対象にする
  2. case_orders_auto_close.pyのrakuten_reopen/yahoo_restockを再利用し、
     楽天はhideItem=false、Yahooは在庫をRESTOCK_QUANTITY（既定100）に戻す
  3. 成功したらH列を空欄に戻し、次に再び仕入不可になったら改めてCloseの対象にする

実行結果は「自動Close_ログ」タブ（case_orders_auto_close.pyと共通）に追記する。
楽天・Yahooの停止/再開方式やSKU接尾辞の扱いはcase_orders_auto_close.pyと全く同じ
（詳細はそちらのdocstring参照）。このスクリプトはPlaywrightを使わず、
シート読み書きとモールAPI呼び出しのみで完結する。
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

from case_orders_auto_close import (
    DRY_RUN,
    JST,
    YAHOO_SUFFIXES,
    get_spreadsheet,
    get_yahoo_access_token,
    rakuten_hide,
    rakuten_reopen,
    yahoo_close,
    yahoo_restock,
    append_log,
)

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
RESTOCK_QUANTITY = int(os.environ.get("RESTOCK_QUANTITY", "100"))
# テスト用：指定した場合、この商品管理番号だけを対象にする（カンマ区切りで複数可）
ONLY_ITEM_NUMBERS = {
    s.strip() for s in os.environ.get("ONLY_ITEM_NUMBERS", "").split(",") if s.strip()
}

SHORTAGE_SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHORTAGE_SHEET_NAME = "ASINあり"

COL_ITEM_NUMBER = 0     # A列：商品管理番号
COL_NAME = 1            # B列：商品名
COL_STOCK_CHECK = 4     # E列：在庫チェック
COL_DONE = 7            # H列：在庫対応済み（このスクリプトが書き込む）
SHORTAGE_LABEL = "⚠️ 仕入不可"
AVAILABLE_LABELS = ("✅ 正常", "🟡 3rdパーティ")

# これらのメーカーはAmazon仕入れではないため、⚠️仕入不可と判定されてもCloseしない
# （メーカー列が無いシートのため、商品名に含まれるキーワードで判定する）
MAKER_EXCLUDE_KEYWORDS = [
    "Bath & Body Works", "Bath and Body Works", "Bath&Body Works",
    "バス&ボディワークス", "バス＆ボディワークス", "バスアンドボディワークス", "バスボディワークス",
    "Victoria's Secret", "Victorias Secret", "Victoria’s Secret",
    "ヴィクトリアズシークレット", "ビクトリアズシークレット", "ヴィクトリアシークレット", "ビクトリアシークレット",
    "Trader Joe's", "Trader Joes", "Trader Joe’s",
    "トレーダージョーズ", "トレージョーズ", "トレーダージョーズ",
    "Obagi",
    "オバジ",
]

# 単独の単語だと誤マッチしやすいメーカー（例："Secret"は一般的すぎる）は、
# 組み合わせの両方が商品名に含まれる場合だけ除外する（AND条件）
MAKER_EXCLUDE_COMBOS = [
    (["Secret", "シークレット"], ["Deodorant", "デオドラント"]),  # Secret（デオドラントブランド）
    (["Old Spice", "オールドスパイス"], ["Deodorant", "デオドラント"]),  # Old Spice（デオドラントブランド）
]


def has_excluded_maker(name: str) -> bool:
    lowered = name.lower()
    if any(keyword.lower() in lowered for keyword in MAKER_EXCLUDE_KEYWORDS):
        return True
    for group_a, group_b in MAKER_EXCLUDE_COMBOS:
        if any(a.lower() in lowered for a in group_a) and any(b.lower() in lowered for b in group_b):
            return True
    return False


def find_row_by_item_number(ws, item_number: str):
    """A列（商品管理番号）を検索し、その行番号（1始まり）を返す。見つからなければNone。

    処理開始時に読み込んだ行番号をそのまま書き込みに使うと、実行中にシートの行が
    追加・削除された場合に書き込み先がズレる（別商品の行を上書きしてしまう）ため、
    書き込み直前に必ずこの関数で現在の行番号を確認してから使う。
    """
    try:
        cell = ws.find(item_number, in_column=COL_ITEM_NUMBER + 1)
    except gspread.exceptions.CellNotFound:
        return None
    return cell.row if cell else None


def get_shortage_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHORTAGE_SPREADSHEET_ID).worksheet(SHORTAGE_SHEET_NAME)


def find_close_targets(values: list) -> list:
    """(行番号, 商品管理番号) のリストを返す。行番号は1始まり（シート上の実際の行）。"""
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
        name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        if has_excluded_maker(name):
            continue
        if ONLY_ITEM_NUMBERS and item_number not in ONLY_ITEM_NUMBERS:
            continue
        targets.append((i, item_number))
    return targets


def find_reopen_targets(values: list) -> list:
    """一度Closeした（H列に値がある）が、再チェックで仕入れ可能に戻った行を返す。"""
    targets = []
    for i, row in enumerate(values[1:], start=2):
        if len(row) <= COL_DONE or not row[COL_DONE].strip():
            continue  # 在庫対応済み（Close済み）でなければ対象外
        if len(row) <= COL_STOCK_CHECK or row[COL_STOCK_CHECK].strip() not in AVAILABLE_LABELS:
            continue
        item_number = row[COL_ITEM_NUMBER].strip()
        if not item_number:
            continue
        if ONLY_ITEM_NUMBERS and item_number not in ONLY_ITEM_NUMBERS:
            continue
        targets.append((i, item_number))
    return targets


def main():
    print("=== 仕入不可商品 自動Close/再開 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：モール側もシート側も一切変更しません")

    spreadsheet = get_spreadsheet()
    # Yahooのアクセストークンは短時間（目安1時間）で失効する。MAX_PER_RUNが大きいと
    # 1回の実行が数時間かかることがあるため、定期的に再取得して401エラーを防ぐ。
    yahoo_token_state = {"token": get_yahoo_access_token(spreadsheet), "fetched_at": time.time()}
    YAHOO_TOKEN_REFRESH_SEC = 40 * 60  # 40分ごとに再取得（失効目安1時間より十分早める）

    def get_fresh_yahoo_token():
        if time.time() - yahoo_token_state["fetched_at"] > YAHOO_TOKEN_REFRESH_SEC:
            print("  （Yahooアクセストークンを再取得します）")
            yahoo_token_state["token"] = get_yahoo_access_token(spreadsheet)
            yahoo_token_state["fetched_at"] = time.time()
        return yahoo_token_state["token"]

    shortage_ws = get_shortage_sheet()

    header = shortage_ws.row_values(1)
    if len(header) <= COL_DONE or header[COL_DONE].strip() != "在庫対応済み":
        if shortage_ws.col_count <= COL_DONE:
            shortage_ws.add_cols(COL_DONE + 1 - shortage_ws.col_count)
        shortage_ws.update_cell(1, COL_DONE + 1, "在庫対応済み")
        print("H列にヘッダー「在庫対応済み」を設定しました。")
    # I列（価格調整対応済み）は今後の価格調整自動化用に予約しておく（今回は何も書き込まない）
    if len(header) <= COL_DONE + 1 or header[COL_DONE + 1].strip() != "価格調整対応済み":
        if shortage_ws.col_count <= COL_DONE + 1:
            shortage_ws.add_cols(COL_DONE + 2 - shortage_ws.col_count)
        shortage_ws.update_cell(1, COL_DONE + 2, "価格調整対応済み")
        print("I列にヘッダー「価格調整対応済み」を設定しました（予約列、未使用）。")

    values = shortage_ws.get_all_values()
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []

    # ══ Close処理 ══
    close_targets = find_close_targets(values)
    print(f"\n--- Close対象（仕入不可・未対応）: {len(close_targets)}件 ---")
    closed = 0

    for row_num, item_number in close_targets[:MAX_PER_RUN]:
        print(f"\n--- [Close] {item_number}（{row_num}行目） ---")
        all_ok = True

        for store_name, message, ok in rakuten_hide(item_number):
            print(f"    [楽天] {store_name}: {message}")
            log_rows.append([now, "-", "仕入不可自動Close", "楽天", store_name, item_number, message])
            if not ok:
                all_ok = False

        candidates = [item_number + suffix for suffix in YAHOO_SUFFIXES]
        for store_name, message, ok in yahoo_close(get_fresh_yahoo_token(), candidates):
            print(f"    [Yahoo] {store_name}: {message}")
            log_rows.append([now, "-", "仕入不可自動Close", "Yahoo", store_name, item_number, message])
            if not ok:
                all_ok = False

        if not all_ok:
            print("  ⚠️ 失敗があったため、未対応のまま残します（次回再挑戦）。")
            continue

        closed += 1
        if DRY_RUN:
            print("  【DRY RUN】本番ならここでH列に在庫対応済みを記録します。")
            continue

        try:
            current_row = find_row_by_item_number(shortage_ws, item_number)
            if current_row is None:
                print(f"  ⚠️ シート上で{item_number}が見つからず、H列を更新できませんでした（モール側は停止済み）。")
            else:
                shortage_ws.update_cell(current_row, COL_DONE + 1, now)
                print(f"  ✅ H列（{current_row}行目）に在庫対応済み（{now}）を記録しました。")
        except Exception as e:
            print(f"  ⚠️ シート更新に失敗しました（モール側は停止済み）: {e}")

    # ══ 再開処理 ══
    reopen_targets = find_reopen_targets(values)
    print(f"\n--- 再開対象（Close済みだが仕入れ可能に戻った）: {len(reopen_targets)}件 ---")
    reopened = 0

    for row_num, item_number in reopen_targets[:MAX_PER_RUN]:
        print(f"\n--- [再開] {item_number}（{row_num}行目） ---")
        all_ok = True

        for store_name, message, ok in rakuten_reopen(item_number):
            print(f"    [楽天] {store_name}: {message}")
            log_rows.append([now, "-", "仕入可能化・自動再開", "楽天", store_name, item_number, message])
            if not ok:
                all_ok = False

        candidates = [item_number + suffix for suffix in YAHOO_SUFFIXES]
        for store_name, message, ok in yahoo_restock(get_fresh_yahoo_token(), candidates, RESTOCK_QUANTITY):
            print(f"    [Yahoo] {store_name}: {message}")
            log_rows.append([now, "-", "仕入可能化・自動再開", "Yahoo", store_name, item_number, message])
            if not ok:
                all_ok = False

        if not all_ok:
            print("  ⚠️ 失敗があったため、在庫対応済みのまま残します（次回再挑戦）。")
            continue

        reopened += 1
        if DRY_RUN:
            print("  【DRY RUN】本番ならここでH列の在庫対応済みを解除します。")
            continue

        try:
            current_row = find_row_by_item_number(shortage_ws, item_number)
            if current_row is None:
                print(f"  ⚠️ シート上で{item_number}が見つからず、H列を更新できませんでした（モール側は再開済み）。")
            else:
                shortage_ws.update_cell(current_row, COL_DONE + 1, "")
                print(f"  ✅ H列（{current_row}行目）の在庫対応済みを解除しました。")
        except Exception as e:
            print(f"  ⚠️ シート更新に失敗しました（モール側は再開済み）: {e}")

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを「自動Close_ログ」タブに{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print(f"\n=== 完了: Close {closed}件（残り{len(close_targets) - min(len(close_targets), MAX_PER_RUN)}件） / "
          f"再開 {reopened}件（残り{len(reopen_targets) - min(len(reopen_targets), MAX_PER_RUN)}件） ===")
    print("=== 仕入不可商品 自動Close/再開 完了 ===")


if __name__ == "__main__":
    main()
