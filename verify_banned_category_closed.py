"""
一時確認用: 9/30ガイドライン対応でCloseした全商品（楽天66件・Yahoo53件）が
現在も非公開/在庫0のままか、実際にAPIを叩いて再確認する。書き込みは行わない。
"""

from case_orders_auto_close import get_rakuten_stores, rakuten_auth_headers, RMS_BASE
from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, yahoo_get_item
import requests
import time

RAKUTEN_ITEMS = [
    # 米・米加工品・ペット・ヘアケア（49件）
    "ry23013233", "0126re023", "ry23013235", "ry23013235-1",
    "ma66593", "my031904270", "23007669", "mins-001", "my20230867", "my20230867-3",
    "my20230438", "160515-055", "150602-057", "150602-056",
    "11007133", "11007134", "11007135", "11007136", "9497141", "9497142",
    "je0000982", "je0001098",
    "my094301202", "my094301203", "my094301204",
    "my202211033", "my202211033-2", "my202211034", "my202211034-2",
    "my202211035", "my202211035-2", "my202211036", "my202211036-2",
    "my202211037", "my202211037-2", "my202211038", "my202211038-2",
    "my20221254", "my20221255", "my20221256", "my20221257",
    "my20221258", "my20221259", "my20221260", "my20221261",
    "wa-9497141", "wa-yi250218-04", "wa-yi250414-01",
    "14007001",
    # 牛エキス系（17件）
    "my20231547", "my20231547-2", "my20231547-4", "my20231547-8",
    "now1960", "now1960-2", "now1960-3",
    "nissin2", "nissin4", "nissin8",
    "ry23014184", "ry23014187", "ry23014176",
    "my083102214", "jo0000060", "aj0001016", "ma01082402",
]

YAHOO_ITEMS = [
    "my20231547akc", "my20231547msy",
    "my20231547-2akc", "my20231547-2msy",
    "my20231547-4akc", "my20231547-4msy",
    "my20231547-8akc", "my20231547-8msy",
    "now1960-akc", "now1960msy",
    "now1960-2akc", "now1960-2msy",
    "now1960-3akc", "now1960-3msy",
    "nissin4-akc", "nissin4-msy",
    "nissin8-akc", "nissin8-msy",
    "je0000982akc", "je0000982msy",
    "14007001-ak", "14007001msy",
    "aj0001016akc", "aj0001016msy",
    "my20230867akc", "my20230867msy",
    "my20230867-3akc", "my20230867-3msy",
    "ma01082402akc", "ma01082402msy",
    "ma66593-akc", "ma66593-msy",
    "Mins-001akc", "Mins-001msy",
    "23007669akc", "23007669msy",
    "my031904270akc", "my031904270msy",
    "my20230438akc", "my20230438msy",
    "ry23014187akc", "ry23014187msy",
    "ry23014184akc", "ry23014184msy",
    "ry23014176akc", "ry23014176msy",
    "160515-055msy",
    "jm0000358akc", "jm0000358msy",
    "aj0000008akc", "aj0000008msy",
    "my20231997akc", "my20231997msy",
]


def check_rakuten():
    print(f"=== 楽天 再確認（{len(RAKUTEN_ITEMS)}件） ===")
    still_open = []
    for item in RAKUTEN_ITEMS:
        for store in get_rakuten_stores():
            headers = rakuten_auth_headers(store)
            url = f"{RMS_BASE}/{item}"
            try:
                res = requests.get(url, headers=headers, timeout=30)
            except Exception as e:
                print(f"  [ERR] {item} / {store['name']}: {e}")
                continue
            finally:
                time.sleep(1.0)
            if res.status_code == 404:
                continue
            if res.status_code >= 400:
                print(f"  [ERR] {item} / {store['name']}: status={res.status_code}")
                continue
            hidden = res.json().get("hideItem")
            if hidden is not True:
                still_open.append((item, store["name"]))
                print(f"  [!!] {item} / {store['name']}: hideItem={hidden}（非公開になっていません）")
    if not still_open:
        print("  → 全件、倉庫（非公開）状態を確認できました。")
    return still_open


def check_yahoo():
    print(f"\n=== Yahoo 再確認（{len(YAHOO_ITEMS)}件） ===")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    still_open = []
    for item_code in YAHOO_ITEMS:
        for store in get_yahoo_stores():
            try:
                item = yahoo_get_item(token, store, item_code)
            except Exception as e:
                print(f"  [ERR] {item_code} / {store['name']}: {e}")
                continue
            finally:
                time.sleep(1.0)
            if item is None:
                continue
            qty = item.get("Quantity")
            if qty != "0":
                still_open.append((item_code, store["name"], qty))
                print(f"  [!!] {item_code} / {store['name']}: 在庫={qty}（0になっていません）")
    if not still_open:
        print("  → 全件、在庫0を確認できました。")
    return still_open


def main():
    rakuten_issues = check_rakuten()
    yahoo_issues = check_yahoo()
    print(f"\n=== 総合結果: 楽天 未対応{len(rakuten_issues)}件 / Yahoo 未対応{len(yahoo_issues)}件 ===")


if __name__ == "__main__":
    main()
