"""Live Qty ÷ Daily Med(MED) が閾値日数以下になったら Chatwork (Purchase Team) に通知する。
通知は「閾値を下回った最初の回」だけ。閾値を超えて回復したら状態をリセットし、次回また通知する。
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright

DOMAIN = "app.jrcreators.com"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_URL = f"https://{quote(LOGIN_ID_1, safe='')}:{quote(LOGIN_PASS_1, safe='')}@{DOMAIN}/"

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "136865637"  # Purchase Team
MENTIONS = "[To:2129162]Ayano Cindi Ikeda\n[To:4915089]Marin Ihara"

PRODUCT_ID = "40650"
PRODUCT_URL = f"https://{DOMAIN}/products/view/{PRODUCT_ID}"
THRESHOLD_DAYS = 100

STATE_FILE = Path(__file__).with_name("stock_days_alert_state.json")


def to_number(text):
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        raise ValueError(f"数値を取得できません: {text!r}")
    return float(m.group())


def fetch_product():
    """商品ページから 商品名 / Live Qty / MED を取得する"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900})
        page = context.new_page()

        page.goto(LOGIN_URL, wait_until="networkidle")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(PRODUCT_URL, wait_until="networkidle")

        # 見出し行(th)と値行(td)を突き合わせる。"Live Qty" は見出しが完全一致のものだけ使う
        # （"Japan Live Qty" など別の表と混ざらないようにする）。
        data = page.evaluate(
            """() => {
                const pick = (label) => {
                    for (const tr of document.querySelectorAll('tr')) {
                        const ths = [...tr.querySelectorAll('th')];
                        const i = ths.findIndex(th => th.innerText.trim() === label);
                        if (i < 0) continue;
                        const next = tr.nextElementSibling;
                        if (!next) continue;
                        const tds = [...next.querySelectorAll('td')];
                        if (tds[i]) return tds[i].innerText.trim();
                    }
                    return null;
                };
                const h = document.querySelector('h1, h2, h3');
                return { live: pick('Live Qty'), med: pick('MED'), title: h ? h.innerText.trim() : '' };
            }"""
        )
        browser.close()

    if data["live"] is None or data["med"] is None:
        raise RuntimeError(f"Live Qty / MED が見つかりません: {data}")
    return data["title"], to_number(data["live"]), to_number(data["med"])


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"notified": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_chatwork(message):
    r = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data={"body": message},
    )
    print(f"Chatwork送信ステータス: {r.status_code}")
    if r.status_code not in (200, 201):
        raise Exception(f"Chatwork送信失敗: {r.status_code} {r.text}")


def main():
    title, live_qty, med = fetch_product()
    print(f"商品: {title} / Live Qty={live_qty:g} / MED={med:g}")

    if med <= 0:
        print("MEDが0以下のため判定をスキップします。")
        return

    days = live_qty / med
    print(f"在庫日数 = {days:.1f} 日 (閾値 {THRESHOLD_DAYS} 日)")

    state = load_state()
    if days <= THRESHOLD_DAYS:
        if state.get("notified"):
            print("通知済みのためスキップします。")
            return
        message = (
            f"{MENTIONS}\n"
            f"[info][title]Stock Alert: {days:.0f} days left[/title]"
            f"{title} (ID {PRODUCT_ID})\n"
            f"Live Qty {live_qty:g} / Daily Med {med:g} = {days:.1f} days "
            f"(threshold: {THRESHOLD_DAYS} days)\n"
            f"{PRODUCT_URL}[/info]"
        )
        send_chatwork(message)
        save_state({"notified": True, "days": round(days, 1)})
    else:
        if state.get("notified"):
            print("閾値を超えて回復したため、通知状態をリセットします。")
        save_state({"notified": False, "days": round(days, 1)})


if __name__ == "__main__":
    main()
