"""一時調査用（読み取りのみ）: See's商品のYahoo getItem全項目を出力し、公開/非公開を示す項目を探す。"""
from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, yahoo_get_item

ss = get_spreadsheet()
token = get_yahoo_access_token(ss)
for code in ["11241011akc", "11241011msy", "my20230761akc", "my20230761msy", "sees0002akc", "sees0002msy"]:
    for store in get_yahoo_stores():
        item = yahoo_get_item(token, store, code)
        if item is None:
            continue
        skip = {"Headline", "Caption", "Explanation", "Abstract", "RelevantLinks", "Path", "Name"}
        keys = {k: (v[:40] if isinstance(v, str) else v) for k, v in item.items() if k not in skip}
        print(f"\n### {code}")
        print(keys)
