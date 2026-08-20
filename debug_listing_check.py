from case_orders_auto_close import rakuten_get_current_prices

SKUS = ["296610-0010000018", "KTGT-B00016ATCC", "150608-016"]

for sku in SKUS:
    prices = rakuten_get_current_prices(sku)
    print(f"{sku}: {prices}")
