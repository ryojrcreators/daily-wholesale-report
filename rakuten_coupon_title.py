"""
セール時に、楽天の全商品名の先頭にクーポン文言（例：【最大2000円クーポン 7/26(日)01:59まで】）を
一括で追加・削除する。今まで手作業だった「RMSで全商品CSVをダウンロード→編集→WinSCPでアップロード」
をAPIだけで完結させる。

対象は1店舗（アメリカーナ = RAKUTEN_SHOP_NAME_1）の全商品。

動作モード（MODE環境変数）:
  apply  : 全商品名の先頭に COUPON_TEXT を追加する。適用前に、その時点の商品名を
           スプレッドシートの「楽天_商品名バックアップ」タブに保存する（上書き）。
           既に COUPON_TEXT で始まっている商品はスキップする（二重付与防止）。
  revert : 「楽天_商品名バックアップ」タブの内容を読み、そこに保存されている
           元の商品名にPATCHで戻す。戻し終わったらバックアップタブの内容を消す
           （次回のapply時に、古いバックアップと混同しないため）。

なぜバックアップ方式にしたか:
  「先頭のCOUPON_TEXTの文字数分だけ削って戻す」という単純な方式だと、セール中に
  誰かが手動で商品名を編集した場合にズレて壊れる。実際の商品名をそのまま保存して
  戻す方式なら、途中で何があっても元の状態に確実に戻せる。
"""

import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta

import requests
import gspread
from google.oauth2.service_account import Credentials

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MODE = os.environ.get("MODE", "apply")  # "apply" または "revert"
COUPON_TEXT = os.environ.get("COUPON_TEXT", "")

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS_JSON"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

STORE = {
    "name": os.environ["RAKUTEN_SHOP_NAME_1"],
    "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"],
    "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_1"],
}

RMS_BASE = "https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers"
SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/items/search"
HITS_PER_PAGE = 100
PAGE_INTERVAL = 1.0
API_INTERVAL = 1.0

BACKUP_SHEET_NAME = "楽天_商品名バックアップ"
BACKUP_HEADER = ["商品管理番号", "元の商品名", "バックアップ日時(JST)"]


def auth_header() -> dict:
    import base64
    token = base64.b64encode(
        f"{STORE['service_secret']}:{STORE['license_key']}".encode()
    ).decode()
    return {"Authorization": f"ESA {token}"}


def search_all_titles() -> dict:
    """{商品管理番号: 現在の商品名} を全件取得する（rakuten_listing_sync.pyと同じページング方式）。"""
    headers = auth_header()
    items = {}
    cursor_mark = "*"
    page = 1
    while True:
        params = {"hits": HITS_PER_PAGE, "cursorMark": cursor_mark}
        res = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        if res.status_code == 401:
            raise RuntimeError("認証エラー（401）。ライセンスキーの期限切れの可能性があります。")
        res.raise_for_status()
        data = res.json()

        results = data.get("results", [])
        print(f"  ページ{page}: {len(results)}件取得（累計 {data.get('numFound', '?')}件中）")
        for r in results:
            item = r.get("item", r)
            manage_number = item.get("manageNumber", "")
            title = item.get("title", "")
            if manage_number:
                items[manage_number] = title

        next_cursor = data.get("nextCursorMark")
        if not results or not next_cursor or next_cursor == cursor_mark:
            break
        cursor_mark = next_cursor
        page += 1
        time.sleep(PAGE_INTERVAL)
    return items


def update_title(manage_number: str, new_title: str) -> tuple:
    """戻り値は (成功したか, メッセージ)。"""
    if DRY_RUN:
        return True, f"{manage_number}: 【DRY RUN】「{new_title}」に更新予定"
    url = f"{RMS_BASE}/{manage_number}"
    try:
        res = requests.patch(url, headers=auth_header(), json={"title": new_title}, timeout=30)
    except Exception as e:
        return False, f"{manage_number}: 更新エラー: {e}"
    finally:
        time.sleep(API_INTERVAL)
    if res.status_code == 204:
        return True, f"{manage_number}: 更新しました"
    return False, f"{manage_number}: 更新失敗({res.status_code}) {res.text[:150]}"


def get_backup_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(LISTING_SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(BACKUP_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=BACKUP_SHEET_NAME, rows=2000, cols=len(BACKUP_HEADER))
        ws.append_row(BACKUP_HEADER)
        return ws


def run_apply():
    if not COUPON_TEXT:
        print("COUPON_TEXT が空です。付ける文言を指定してください。")
        sys.exit(1)

    print(f"=== クーポン文言の一括付与 開始（{STORE['name']}） ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際には変更しません")
    print(f"付与する文言: {COUPON_TEXT}")

    print("全商品の商品名を取得中...")
    titles = search_all_titles()
    print(f"取得件数: {len(titles)}件")

    targets = {mn: t for mn, t in titles.items() if not t.startswith(COUPON_TEXT)}
    already = len(titles) - len(targets)
    if already:
        print(f"既にこの文言が付いている商品: {already}件（スキップ）")

    if not targets:
        print("対象がありません。終了。")
        return

    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    backup_rows = [[mn, t, now] for mn, t in targets.items()]

    if not DRY_RUN:
        ws = get_backup_sheet()
        ws.clear()
        ws.append_row(BACKUP_HEADER)
        ws.append_rows(backup_rows, value_input_option="USER_ENTERED")
        print(f"「{BACKUP_SHEET_NAME}」に元の商品名を{len(backup_rows)}件保存しました。")
    else:
        print(f"【DRY RUN】バックアップ保存対象: {len(backup_rows)}件")

    ok_count, ng_count = 0, 0
    for mn, old_title in targets.items():
        new_title = COUPON_TEXT + old_title
        ok, message = update_title(mn, new_title)
        print(f"  {message}")
        if ok:
            ok_count += 1
        else:
            ng_count += 1

    print(f"\n成功: {ok_count}件 / 失敗: {ng_count}件")
    print("=== クーポン文言の一括付与 完了 ===")


def run_revert():
    print(f"=== クーポン文言の一括解除 開始（{STORE['name']}） ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際には変更しません")

    ws = get_backup_sheet()
    rows = ws.get_all_values()[1:]  # ヘッダーを除く
    if not rows:
        print(f"「{BACKUP_SHEET_NAME}」にバックアップがありません。何もせず終了します。")
        return

    print(f"バックアップ件数: {len(rows)}件")

    ok_count, ng_count = 0, 0
    for row in rows:
        if len(row) < 2 or not row[0]:
            continue
        manage_number, original_title = row[0], row[1]
        ok, message = update_title(manage_number, original_title)
        print(f"  {message}")
        if ok:
            ok_count += 1
        else:
            ng_count += 1

    print(f"\n成功: {ok_count}件 / 失敗: {ng_count}件")

    if ng_count == 0 and not DRY_RUN:
        ws.clear()
        ws.append_row(BACKUP_HEADER)
        print(f"「{BACKUP_SHEET_NAME}」をクリアしました。")
    elif ng_count:
        print("失敗があったため、バックアップは消さずに残します（再実行できるように）。")

    print("=== クーポン文言の一括解除 完了 ===")


def main():
    if MODE == "apply":
        run_apply()
    elif MODE == "revert":
        run_revert()
    else:
        print(f"不明なMODE: {MODE}（apply または revert を指定してください）")
        sys.exit(1)


if __name__ == "__main__":
    main()
