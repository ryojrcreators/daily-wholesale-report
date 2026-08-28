"""
2026-09-30〜 楽天ガイドライン変更（外国産米・ペットフード類・牛エキス入り食品の出品禁止）
に伴う該当商品の削除実行。

guideline_ban_scan.py が作った「削除候補_ガイドライン対応」タブを読み、
F列「確認済み」に承認マーク（ok/OK/○/削除/承認 のいずれか）が入っている行だけを対象に、
楽天・Yahooの出品を完全削除する（hide/在庫0ではなく、商品そのものを削除）。

承認マークが無い行は一切触らない。安全のため既定はDRY_RUN=true。
実行結果は各行の「対応メモ」列と、既存の「自動Close_ログ」タブ（他の自動化と共通）
の両方に記録する。

Wowma（LA Express）はcase_orders_wowma.pyのdeleteItemInfos（商品削除API）を使う。
このAPIは仕様書から実装したのみで未実機検証のため、本番実行前に必ずONLY_ITEM_CODESで
1件に絞ってDRY RUN→本番の順で試すこと。
"""

import os
import time
from datetime import datetime

import requests

from case_orders_auto_close import (
    DRY_RUN,
    JST,
    RMS_BASE,
    YAHOO_BASE,
    get_spreadsheet,
    get_rakuten_stores,
    get_yahoo_stores,
    get_yahoo_access_token,
    rakuten_auth_headers,
    append_log,
    API_INTERVAL,
)
from case_orders_wowma import wowma_delete_items, wowma_end_sale

CANDIDATE_SHEET_NAME = "削除候補_ガイドライン対応"
WOWMA_SHOP_LABEL = "LA Express"
APPROVE_VALUES = {"ok", "○", "delete", "削除", "済", "承認"}

# テスト用：指定した場合、この商品管理番号/商品コードだけを対象にする（カンマ区切りで複数可）
ONLY_ITEM_CODES = {
    s.strip() for s in os.environ.get("ONLY_ITEM_CODES", "").split(",") if s.strip()
}


def is_approved(value: str) -> bool:
    return bool(value) and value.strip().lower() in APPROVE_VALUES


def rakuten_delete_item(store: dict, manage_number: str) -> tuple:
    headers = rakuten_auth_headers(store)
    url = f"{RMS_BASE}/{manage_number}"
    if DRY_RUN:
        return True, "【DRY RUN】削除対象"
    try:
        res = requests.delete(url, headers=headers, timeout=30)
    except Exception as e:
        return False, f"削除エラー: {e}"
    if res.status_code in (200, 204):
        return True, "削除しました"
    if res.status_code == 404:
        return True, "すでに存在しません"
    return False, f"削除失敗({res.status_code}) {res.text[:150]}"


def yahoo_delete_item(token: str, store: dict, item_code: str) -> tuple:
    if DRY_RUN:
        return True, "【DRY RUN】削除対象"
    try:
        res = requests.post(
            f"{YAHOO_BASE}/deleteItem",
            headers={"Authorization": f"Bearer {token}"},
            data={"seller_id": store["seller_id"], "item_code": item_code},
            timeout=30,
        )
    except Exception as e:
        return False, f"削除エラー: {e}"
    if res.status_code < 400:
        return True, "削除しました"
    if res.status_code == 404:
        return True, "すでに存在しません"
    return False, f"削除失敗({res.status_code}) {res.text[:150]}"


def main():
    print("=== ガイドライン該当商品 削除実行 開始 ===")
    print(f"DRY_RUN={DRY_RUN}")

    spreadsheet = get_spreadsheet()
    ws = spreadsheet.worksheet(CANDIDATE_SHEET_NAME)
    all_rows = ws.get_all_values()
    header = all_rows[0]
    data = all_rows[1:]

    col_confirm = header.index("確認済み")
    col_note = header.index("対応メモ")

    rakuten_stores = {s["name"]: s for s in get_rakuten_stores()}
    yahoo_stores = {s["name"]: s for s in get_yahoo_stores()}
    yahoo_token = None

    log_rows = []
    processed = 0
    skipped_unapproved = 0

    for i, row in enumerate(data, start=2):
        mall = row[0] if len(row) > 0 else ""
        shop = row[1] if len(row) > 1 else ""
        code = row[2] if len(row) > 2 else ""
        name = row[3] if len(row) > 3 else ""
        confirm = row[col_confirm] if len(row) > col_confirm else ""

        if ONLY_ITEM_CODES and code not in ONLY_ITEM_CODES:
            continue

        if not is_approved(confirm):
            skipped_unapproved += 1
            continue

        if mall == "楽天":
            store = rakuten_stores.get(shop)
            if store is None:
                ok, note = False, f"店舗「{shop}」の認証情報が見つかりません"
            else:
                ok, note = rakuten_delete_item(store, code)
        elif mall == "Yahoo":
            if yahoo_token is None:
                yahoo_token = get_yahoo_access_token(spreadsheet)
            store = yahoo_stores.get(shop)
            if store is None:
                ok, note = False, f"店舗「{shop}」の認証情報が見つかりません"
            else:
                ok, note = yahoo_delete_item(yahoo_token, store, code)
        elif mall == "Wowma":
            end_ok, end_note = wowma_end_sale(code, DRY_RUN)
            if not end_ok:
                ok, note = False, f"販売終了への変更に失敗したため削除は行っていません: {end_note}"
            else:
                _, ok, note = wowma_delete_items([code], DRY_RUN)[0]
                note = f"{end_note} → {note}"
        else:
            ok, note = False, f"未対応モール: {mall}"

        print(f"  行{i} [{mall}/{shop}] {code} 「{name[:40]}」: {note}")
        ws.update_cell(i, col_note + 1, f"{note}（{datetime.now(JST).strftime('%Y/%m/%d %H:%M')}）")
        log_rows.append([
            datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S"),
            "-", "ガイドライン対応削除", mall, shop, code, note,
        ])
        processed += 1
        time.sleep(API_INTERVAL)

    append_log(spreadsheet, log_rows)

    print(f"\n処理件数: {processed}件（未承認のためスキップ: {skipped_unapproved}件）")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
