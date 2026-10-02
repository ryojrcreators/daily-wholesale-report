"""一時調査用（読み取りのみ）: See's商品のYahoo getItem全項目を出力し、公開/非公開を示す項目を探す。"""
from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, yahoo_get_item

ss = get_spreadsheet()
token = get_yahoo_access_token(ss)
for code in ["10000404akc", "10000404msy", "sees0002akc"]:
    for store in get_yahoo_stores():
        item = yahoo_get_item(token, store, code)
        if item is None:
            continue
        skip = {"Headline", "Caption", "Explanation", "Abstract", "RelevantLinks", "SpAdditional", "Image", "LibImage1", "LibImage2", "LibImage3", "LibImage8"}
        keys = {k: (v[:40] if isinstance(v, str) else v) for k, v in item.items() if k not in skip}
        print(f"\n### {code}")
        print(keys)
