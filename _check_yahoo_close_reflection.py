"""
一時調査用（読み取りのみ）：Yahooの既存自動Close機能（setStock）が反映API（submitItem）
を呼んでいない件について、実際にCloseされた商品が本当にEditingFlag=1のまま
（＝未反映の可能性）になっているか確認する。

1. 「自動Close_ログ」タブから、Yahoo宛てに成功した直近のログ行を探し、実際の商品コードを拾う
2. その商品コードをgetItemで取得し、Quantity/EditingFlag/Display/HiddenFlagを表示する
   （変更は一切行わない）
"""

from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, YAHOO_STORES, yahoo_get_item

LOG_SHEET_NAME = "自動Close_ログ"

spreadsheet = get_spreadsheet()
ws = spreadsheet.worksheet(LOG_SHEET_NAME)
rows = ws.get_all_values()
header = rows[0] if rows else []
print(f"ログ列: {header}")
print(f"ログ行数: {len(rows) - 1}")

# モール=Yahoo かつ 結果に「在庫」という文字を含む（＝setStockで実際に変更した）行を新しい順に探す
# item_code列（A列基準の商品管理番号）は接尾辞なしのため、実際に変更されたコード
# （接尾辞付き）は「結果」欄の "je0000359-akc: 在庫...→0にしました" から抽出する
candidates = []
for row in rows[1:]:
    if len(row) < 7:
        continue
    mall = row[3]
    result = row[6]
    base_item_code = row[5]
    if mall != "Yahoo" or "在庫" not in result or "→" not in result:
        continue
    actual_item_code = result.split(":")[0].strip() if ":" in result else base_item_code
    if actual_item_code:
        candidates.append((row[0], row[4], actual_item_code, result))

print(f"\n該当するYahoo在庫変更ログ: {len(candidates)}件")
for r in candidates[-10:]:
    print(f"  {r}")

if not candidates:
    print("\n該当ログが見つからなかったため、商品状態の確認はスキップします。")
else:
    token = get_yahoo_access_token(spreadsheet)
    checked = set()
    print("\n=== 実際の商品状態（読み取りのみ） ===")
    for _, store_name, item_code, result in candidates[-10:]:
        if item_code in checked:
            continue
        checked.add(item_code)
        store = next((s for s in YAHOO_STORES if s["name"] == store_name), None)
        if store is None:
            print(f"  {item_code}: 店舗「{store_name}」が見つかりませんでした")
            continue
        try:
            item = yahoo_get_item(token, store, item_code)
        except Exception as e:
            print(f"  {item_code}: 取得エラー {e}")
            continue
        if item is None:
            print(f"  {item_code}（{store_name}）: 商品が見つかりませんでした")
            continue
        print(f"  {item_code}（{store_name}）: ログ内容=[{result}] / "
              f"現在Quantity={item.get('Quantity')} / EditingFlag={item.get('EditingFlag')} / "
              f"Display={item.get('Display')} / HiddenFlag={item.get('HiddenFlag')}")
