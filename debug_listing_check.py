from case_orders_auto_close import rakuten_get_current_prices

SKUS = ["296610-0010000018", "KTGT-B00016ATCC", "150608-016"]

for sku in SKUS:
    prices = rakuten_get_current_prices(sku)
    # 店舗名がSecrets登録されておりログがマスクされるため、店舗番号だけで表示する
    summary = {f"store{i+1}": p for i, p in enumerate(prices.values())}
    print(f"{sku}: 店舗数={len(prices)} {summary}")
