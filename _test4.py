"""（一時）/sales の絞り込み検索結果を Playwright + CSVダウンロードで取得できるか検証する。

検証する条件（ユーザー指定のURLと同じ）:
  start_date=2026-07-01 / end_date=2026-07-31
  SoHeads[sku_shop_id][]=1, 15 / SoHeads[so_status_id][]=7
  has_credit=0 / has_cs_request=1 / SoHeads[resend]=0

確認する3点:
  A) /sales?<条件> をbotで開いたとき、一覧が描画されるか・Downloadリンクがあるか
  B) 画面を開いた後に /sales/download?<条件> を叩いて200＋該当行が返るか
  C) 画面を一切開かずに /sales/download?<条件> を直接叩いても同じ結果になるか
     （＝セッションに検索条件を保存する必要があるかどうかの切り分け）
"""
import csv
import os
import requests
from urllib.parse import quote
from playwright.sync_api import sync_playwright

DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"

QUERY = (
    "start_date=2026-07-01"
    "&end_date=2026-07-31"
    "&SoHeads%5Bsku_shop_id%5D%5B%5D=1"
    "&SoHeads%5Bsku_shop_id%5D%5B%5D=15"
    "&SoHeads%5Bso_status_id%5D%5B%5D=7"
    "&has_credit=0"
    "&has_cs_request=1"
    "&SoHeads%5Bresend%5D=0"
)
SALES_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/sales?{QUERY}"
DOWNLOAD_URL = f"https://{DOMAIN}/sales/download?{QUERY}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def login(p):
    """Basic認証 → フォームログインを済ませた (browser, context, page) を返す"""
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1800, "height": 900},
        device_scale_factor=2,
        user_agent=UA,
    )
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    return browser, context, page


def try_download(context, url, label, save_as):
    """requests + cookie + Basic認証 でCSVを取得し、結果を要約して表示する"""
    cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
    res = requests.get(
        url,
        cookies=cookie_dict,
        headers={"User-Agent": UA},
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
        timeout=180,
    )
    print(f"[{label}] status={res.status_code} "
          f"content-type={res.headers.get('Content-Type')} bytes={len(res.content)}")
    if res.status_code != 200:
        return None

    text = res.content.decode("utf-8-sig", errors="replace")
    # HTMLが返ってきていないか（＝ログイン画面などにリダイレクトされていないか）を判定
    if text.lstrip()[:15].lower().startswith("<!doctype") or "<html" in text[:500].lower():
        print(f"[{label}] ！CSVではなくHTMLが返っています（先頭200文字）: {text[:200]!r}")
        return None

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        print(f"[{label}] 中身が空でした")
        return None

    header, data = rows[0], [r for r in rows[1:] if any(r)]
    print(f"[{label}] ヘッダー列数={len(header)} データ行数={len(data)}")
    print(f"[{label}] 列名: {header}")

    with open(save_as, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(data)
    print(f"[{label}] 保存しました: {save_as}")
    return data


with sync_playwright() as p:
    # ===== A) 画面を開いて一覧が描画されるか =====
    browser, context, page = login(p)
    print("ログイン完了")

    print("A) /sales を絞り込み条件付きで開きます")
    page.goto(SALES_URL, wait_until="networkidle")
    page.wait_for_timeout(3000)
    page.screenshot(path="sales_search_result.png", full_page=True)

    order_link_count = page.evaluate(
        "document.querySelectorAll('a[href*=\"/sales/view/\"]').length")
    download_href = page.evaluate("""
        (() => {
            const a = [...document.querySelectorAll('a')]
                .find(a => a.textContent.trim().toLowerCase().includes('download'));
            return a ? a.getAttribute('href') : null;
        })()
    """)
    print(f"A) 注文リンク数={order_link_count} / Downloadリンク href={download_href}")

    # ===== B) 画面を開いた後にダウンロード =====
    url_b = DOWNLOAD_URL
    if download_href:
        url_b = (f"https://{DOMAIN}{download_href}"
                 if download_href.startswith("/") else download_href)
    print(f"B) ダウンロードURL: {url_b}")
    rows_b = try_download(context, url_b, "B:画面経由", "sales_filtered_B.csv")

    browser.close()

    # ===== C) 画面を開かずに直接ダウンロード =====
    browser2, context2, page2 = login(p)
    print("C) ログイン直後に、画面を開かずダウンロードURLを直接叩きます")
    rows_c = try_download(context2, DOWNLOAD_URL, "C:直接", "sales_filtered_C.csv")
    browser2.close()

print("=== まとめ ===")
print(f"A) 一覧の描画: 注文リンク {order_link_count} 件 / Downloadリンク {'あり' if download_href else 'なし'}")
print(f"B) 画面経由のダウンロード: {len(rows_b) if rows_b is not None else '失敗'} 行")
print(f"C) 直接ダウンロード:       {len(rows_c) if rows_c is not None else '失敗'} 行")
