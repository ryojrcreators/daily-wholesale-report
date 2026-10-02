"""
一時実行用: Americana（1店舗目）で公開中のSee's Candies商品のうち、Founder（2店舗目）で非公開のものを
Founderだけ再開（hideItem=false）する。Americana側は一切変更しない。
条件を満たさない商品（Americanaが非公開、Founderに無い、Founderが既に公開中）は触らない。
DRY_RUN=true（既定）では何も変更しない。
"""
import time

import requests

from case_orders_auto_close import DRY_RUN, RMS_BASE, get_rakuten_stores, rakuten_auth_headers

with open("sees_rakuten_reopen_items.txt", encoding="utf-8") as f:
    ITEMS = [line.strip() for line in f if line.strip()]


def get_hide(store, code):
    res = requests.get(f"{RMS_BASE}/{code}", headers=rakuten_auth_headers(store), timeout=30)
    time.sleep(1.0)
    if res.status_code == 404:
        return None
    res.raise_for_status()
    return res.json().get("hideItem")


def main():
    americana, founder = get_rakuten_stores()
    print(f"=== See's Founder再開（DRY_RUN={DRY_RUN}） 候補{len(ITEMS)}件 ===")
    done = skipped = failed = 0
    for code in ITEMS:
        a, f = get_hide(americana, code), get_hide(founder, code)
        if a is not False:
            print(f"  [SKIP] {code}: Americanaが公開中ではない（hideItem={a}）")
            skipped += 1
            continue
        if f is not True:
            print(f"  [SKIP] {code}: Founderが非公開ではない（hideItem={f}）")
            skipped += 1
            continue
        if DRY_RUN:
            print(f"  [OK] {code}: 【DRY RUN】Founderを再開する対象")
            done += 1
            continue
        res = requests.patch(
            f"{RMS_BASE}/{code}", headers=rakuten_auth_headers(founder), json={"hideItem": False}, timeout=30
        )
        time.sleep(1.0)
        if res.status_code == 204:
            print(f"  [OK] {code}: Founderを再開しました")
            done += 1
        else:
            print(f"  [NG] {code}: 失敗({res.status_code}) {res.text[:150]}")
            failed += 1
    print(f"=== 完了: 再開{done}件 / スキップ{skipped}件 / 失敗{failed}件 ===")


if __name__ == "__main__":
    main()
