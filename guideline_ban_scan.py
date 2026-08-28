"""
2026-09-30〜 楽天ガイドライン変更（外国産米・ペットフード類・牛エキス入り食品の出品禁止）
に伴う該当商品の洗い出し。

「楽天_出品データ」「Yahoo_出品データ」タブ（rakuten_listing_sync.py / yahoo_listing_sync.py
が毎日更新している出品スナップショット）を商品名のキーワードでスキャンし、該当しそうな
商品を「削除候補_ガイドライン対応」タブに書き出す。

キーワードマッチは誤検知・見落としの両方があり得るため、これは一次候補リストであり、
実際の削除は人が目で確認してから別途実行する。Wowma（LA Express）は全件取得の仕組みが
まだ無いため対象外（別途手動確認）。
"""

import os
import re
import time

import gspread
from google.oauth2.service_account import Credentials

from case_orders_wowma import wowma_search_items, API_INTERVAL as WOWMA_API_INTERVAL

SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]
RAKUTEN_SHEET_NAME = "楽天_出品データ"
YAHOO_SHEET_NAME = "Yahoo_出品データ"
OUTPUT_SHEET_NAME = "削除候補_ガイドライン対応"
WOWMA_SHOP_LABEL = "LA Express"

# カテゴリごとのキーワード（商品名に部分一致すれば候補にする。大文字小文字は無視）
CATEGORY_KEYWORDS = {
    # 「米」「rice」を単独キーワードにすると「米国」「北米」「Rice Krispies」等の
    # 誤検知が大量発生する（2026-08-28、実データで確認：候補880件中ほぼ全てが誤検知）。
    # ユーザー確認の上、対象を「お米そのもの（生米・白米・玄米等）」に絞ったキーワードにする。
    "外国産米": [
        "白米", "玄米", "もち米", "無洗米", "精米",
        "ジャスミンライス", "バスマティライス", "スシライス",
        "jasmine rice", "basmati rice", "sushi rice",
        "white rice", "brown rice", "long grain rice",
    ],
    "ペットフード・ペットサプリ・ペットヘアケア": [
        "ペットフード", "ドッグフード", "キャットフード", "ペットおやつ",
        "ペット用サプリ", "ペットサプリ", "ペット用シャンプー", "ペットシャンプー",
        "ペット用ヘアケア", "犬用フード", "猫用フード", "犬 おやつ", "猫 おやつ",
        "犬用サプリ", "猫用サプリ", "犬用シャンプー", "猫用シャンプー",
        "dog food", "cat food", "pet food", "pet treat", "pet supplement",
        "pet shampoo",
    ],
    "牛エキス入り食品": ["牛エキス", "ビーフエキス", "beef extract"],
}

# カテゴリごとの除外キーワード。「玄米」等はお米そのもの以外の加工食品（茶・パスタ・
# プロテイン等）にも使われる表記のため、これらを含む場合は対象から外す。
# 2026-08-28、ユーザー確認: 玄米茶・玄米パスタは対象外。
CATEGORY_EXCLUDE_KEYWORDS = {
    "外国産米": ["茶", "パスタ", "プロテイン", "protein", "シロップ", "syrup"],
    # ディスペンサー・ボウル・おもちゃ等は「フード」を扱う器具であってフード本体ではない
    "ペットフード・ペットサプリ・ペットヘアケア": [
        "ディスペンサー", "dispenser", "ボウル", "bowl", "おもちゃ", "toy",
        "パズル", "puzzle", "フィーダー", "feeder",
        "カーペットクリーナー", "カーペットシャンプー", "carpet cleaner", "carpet shampoo",
    ],
}


def get_spreadsheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS"]
    import json
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def find_matches(mall: str, rows: list):
    """rows: [店舗名, 商品コード, 商品名, ...] のリスト。マッチした行を返す。"""
    results = []
    for row in rows:
        if len(row) < 3:
            continue
        shop, code, name = row[0], row[1], row[2]
        lowered = name.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            hit_keywords = [kw for kw in keywords if kw.lower() in lowered]
            if not hit_keywords:
                continue
            exclude_keywords = CATEGORY_EXCLUDE_KEYWORDS.get(category, [])
            if any(ex.lower() in lowered for ex in exclude_keywords):
                continue
            if hit_keywords:
                results.append([mall, shop, code, name, category, "、".join(hit_keywords)])
                break  # 1商品1カテゴリで十分（複数該当しても最初の1つだけ記録）
    return results


def main():
    print("=== ガイドライン該当商品スキャン開始 ===")
    spreadsheet = get_spreadsheet()

    all_results = []

    try:
        rakuten_ws = spreadsheet.worksheet(RAKUTEN_SHEET_NAME)
        rakuten_rows = rakuten_ws.get_all_values()[1:]
        print(f"楽天_出品データ: {len(rakuten_rows)}件読み込み")
        all_results += find_matches("楽天", rakuten_rows)
    except gspread.exceptions.WorksheetNotFound:
        print(f"「{RAKUTEN_SHEET_NAME}」タブが見つかりません。スキップします。")

    try:
        yahoo_ws = spreadsheet.worksheet(YAHOO_SHEET_NAME)
        yahoo_rows = yahoo_ws.get_all_values()[1:]
        print(f"Yahoo_出品データ: {len(yahoo_rows)}件読み込み")
        all_results += find_matches("Yahoo", yahoo_rows)
    except gspread.exceptions.WorksheetNotFound:
        print(f"「{YAHOO_SHEET_NAME}」タブが見つかりません。スキップします。")

    wowma_rows = []
    start_count = 1
    page_size = 500
    try:
        while True:
            items, max_count = wowma_search_items(start_count, page_size)
            wowma_rows += [[WOWMA_SHOP_LABEL, item.get("itemCode", ""), item.get("itemName", "")] for item in items]
            time.sleep(WOWMA_API_INTERVAL)
            if not items or start_count + len(items) > max_count:
                break
            start_count += len(items)
        print(f"Wowma（{WOWMA_SHOP_LABEL}）: {len(wowma_rows)}件読み込み")
        all_results += find_matches("Wowma", wowma_rows)
    except Exception as e:
        print(f"Wowmaの取得でエラーが発生したためスキップします: {e}")

    print(f"\n該当候補: {len(all_results)}件")
    for r in all_results[:50]:
        print(" ", r)
    if len(all_results) > 50:
        print(f"  ...他{len(all_results) - 50}件（シートに全件書き込みます）")

    # 出力タブに書き込み（毎回全体を上書き）
    try:
        out_ws = spreadsheet.worksheet(OUTPUT_SHEET_NAME)
        out_ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        out_ws = spreadsheet.add_worksheet(title=OUTPUT_SHEET_NAME, rows=max(len(all_results) + 10, 100), cols=8)

    header = ["モール", "店舗名", "商品管理番号/商品コード", "商品名", "該当カテゴリ", "マッチしたキーワード", "確認済み", "対応メモ"]
    out_ws.update(range_name="A1", values=[header] + all_results)
    print(f"\n「{OUTPUT_SHEET_NAME}」タブに書き込み完了。")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
