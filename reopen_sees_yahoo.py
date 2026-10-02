"""
一時実行用: YahooのSee's商品を公開（Display=1）に戻す。
Yahooの editItem は全置換型（送らなかった項目が消える）ため、getItemで取得した現在の値を
そのまま送り直し、①編集 → ②変更前後の全項目を比較 → ③差異が無い時だけ submitItem で反映、の
順で実行する。差異（消えた/変わった項目）があれば公開せず、変更前の全項目をログに出して止める。
ITEM_CODES（カンマ区切り）で対象を指定する。DRY_RUN=true（既定）では何も変更しない。
"""
import os
import time
from xml.etree import ElementTree

import requests

from case_orders_auto_close import DRY_RUN, YAHOO_BASE, get_spreadsheet, get_yahoo_access_token, get_yahoo_stores

ITEM_CODES = [s.strip() for s in os.environ.get("ITEM_CODES", "").split(",") if s.strip()]

# editItemで使えることを実機で確認済みの項目（yahoo_api_test.py の復元データと同じ名前）
ECHO_FIELDS = {
    "Path": "path", "Name": "name", "ProductCategory": "product_category", "Price": "price",
    "Delivery": "delivery", "LeadTimeInStock": "lead_time_in_stock", "SpCode": "sp_code",
    "Headline": "headline", "Caption": "caption", "Explanation": "explanation",
    "SpAdditional": "sp_additional",
}
IGNORE_DIFF = {"UpdateTime", "EditingFlag"}


def get_item_raw(token, store, code):
    res = requests.get(
        f"{YAHOO_BASE}/getItem", headers={"Authorization": f"Bearer {token}"},
        params={"seller_id": store["seller_id"], "item_code": code}, timeout=30,
    )
    time.sleep(1.0)
    if res.status_code >= 400:
        if "it-05002" in res.text:
            return None
        raise RuntimeError(f"getItem {res.status_code}: {res.text[:300]}")
    fields = {}
    for el in ElementTree.fromstring(res.content).iter():
        tag = el.tag.split("}")[-1]
        if tag not in fields and el.text is not None and el.text.strip() != "":
            fields[tag] = el.text
    return fields


def main():
    ss = get_spreadsheet()
    token = get_yahoo_access_token(ss)
    t0 = time.time()
    print(f"=== See's Yahoo公開（DRY_RUN={DRY_RUN}） 対象{len(ITEM_CODES)}コード ===")
    for code in ITEM_CODES:
        for store in get_yahoo_stores():
            if time.time() - t0 > 300:
                token, t0 = get_yahoo_access_token(ss), time.time()
            before = get_item_raw(token, store, code)
            if before is None:
                continue
            if before.get("Display") == "1":
                print(f"  [SKIP] {code}: すでに公開（Display=1）")
                continue
            if DRY_RUN:
                print(f"  [OK] {code}: 【DRY RUN】Display {before.get('Display')}→1 の対象（在庫{before.get('Quantity')}）")
                continue

            payload = {"seller_id": store["seller_id"], "item_code": code, "display": "1"}
            for tag, param in ECHO_FIELDS.items():
                if tag in before:
                    payload[param] = before[tag]
            res = requests.post(f"{YAHOO_BASE}/editItem", headers={"Authorization": f"Bearer {token}"}, data=payload, timeout=60)
            time.sleep(2)
            if res.status_code >= 400:
                print(f"  [NG] {code}: editItem失敗({res.status_code}) {res.text[:300]}")
                continue

            after = get_item_raw(token, store, code)
            lost = [t for t in before if t not in after and t not in IGNORE_DIFF]
            changed = [t for t in before if t in after and after[t] != before[t] and t not in IGNORE_DIFF | {"Display"}]
            if lost or changed or after.get("Display") != "1":
                print(f"  [STOP] {code}: 差異あり。公開せず停止します。Display={after.get('Display')} / 消えた={lost} / 変化={changed}")
                for t in lost + changed:
                    print(f"     変更前[{t}]: {before.get(t, '')[:200]}")
                continue

            sub = requests.post(f"{YAHOO_BASE}/submitItem", headers={"Authorization": f"Bearer {token}"},
                                data={"seller_id": store["seller_id"], "item_code": code}, timeout=60)
            time.sleep(2)
            final = get_item_raw(token, store, code)
            ok = sub.status_code < 400 and final.get("Display") == "1" and final.get("EditingFlag") == "0"
            print(f"  [{'OK' if ok else 'NG'}] {code}: 反映 status={sub.status_code} Display={final.get('Display')} EditingFlag={final.get('EditingFlag')}")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
