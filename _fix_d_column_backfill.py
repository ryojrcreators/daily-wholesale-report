"""
一時補正用：rakuten_price_adjust.pyがD列（通常購入販売価格）を更新していなかった
バグ修正前に処理済みだった4件について、D列を実際に設定した適正価格へ後追いで補正する。
モール側（楽天・Yahoo）は一切変更しない。シートのD列のみ書き換える。
"""

from rakuten_price_adjust import get_price_sheet, find_row_by_item_number

FIXES = {
    "0421ninpart3": 9564,
    "0613cy01": 33729,
    "0707newi0001": 5318,
    "0707newi0002": 5405,
}

COL_PRICE_JPY = 3  # D列

ws = get_price_sheet()
for item_number, price in FIXES.items():
    row = find_row_by_item_number(ws, item_number)
    if row is None:
        print(f"{item_number}: シート上で見つかりませんでした")
        continue
    ws.update_cell(row, COL_PRICE_JPY + 1, price)
    print(f"{item_number}（{row}行目）: D列を¥{price:,}に補正しました")
