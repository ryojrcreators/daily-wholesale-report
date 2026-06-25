"""
楽天 ASINなし商品 → DeepL翻訳 → Keepaキーワード検索でASIN補完スクリプト

処理概要:
- 「ASINなし（要調査）」シートの商品名を読み込む
- 日本語が含まれる場合はDeepL APIで英訳（残り文字数が少なくなったら自動停止）
- Keepa Search APIで英語キーワード検索してASINを取得
- ASIN候補と信頼度をD列・E列に書き込む
- 信頼度HIGHの商品は手動確認後「ASINあり」シートへ移動する運用想定

列構成（書き込み後）:
  A: 商品管理番号
  B: 商品名
  C: 通常購入販売価格
  D: ASIN候補（このスクリプトが書き込む）
  E: 信頼度（HIGH / LOW）（このスクリプトが書き込む）
"""

import os
import re
import time
import json
import requests
import gspread
from google.oauth2.service_account import Credentials

# ── 設定 ──────────────────────────────────────────
SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINなし（要調査）"

KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

SEARCH_PER_RUN = 150     # 1回の実行で処理する件数（1検索=10トークン、3回/日で4,500トークン消費）
REQUEST_INTERVAL = 25.0  # Keepaリクエスト間隔（秒）
RETRY_WAIT = 60.0        # 429エラー時の待機時間（秒）
TOKEN_THRESHOLD = 12     # この残量以下になったら処理停止（1検索≒10トークンのため余裕を持つ）

DEEPL_CHARS_SAFETY_MARGIN = 50_000  # 残り文字数がこれ以下になったら翻訳停止

# 列インデックス（0始まり）
COL_ITEM_ID    = 0
COL_NAME       = 1
COL_PRICE_JPY  = 2
COL_ASIN       = 3   # ASIN候補（書き込み先）
COL_CONFIDENCE = 4   # 信頼度（書き込み先）

# ── Keepa トークン残量確認 ────────────────────────
def get_keepa_tokens_remaining() -> int:
    """Keepa APIのトークン残量を返す。取得失敗時は-1を返す。"""
    url = f"https://api.keepa.com/token?key={KEEPA_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()
        tokens = data.get("tokensLeft", -1)
        print(f"Keepaトークン残量: {tokens}")
        return tokens
    except Exception as e:
        print(f"Keepaトークン残量取得失敗: {e}")
        return -1


# ── Google Sheets 認証 ────────────────────────────
def get_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


# ── DeepL 残り文字数取得 ──────────────────────────
def get_deepl_chars_remaining() -> int:
    """DeepL APIの残り利用可能文字数を返す。取得失敗時は0を返す。"""
    url = "https://api-free.deepl.com/v2/usage"
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        used = data.get("character_count", 0)
        limit = data.get("character_limit", 0)
        remaining = limit - used
        print(f"DeepL残り文字数: {remaining:,} / {limit:,}")
        return remaining
    except Exception as e:
        print(f"DeepL使用量取得失敗: {e}")
        return 0


# ── 日本語判定 ────────────────────────────────────
def contains_japanese(text: str) -> bool:
    """テキストに日本語（ひらがな・カタカナ・漢字）が含まれるか判定。"""
    return bool(re.search(r'[぀-鿿]', text))


# ── DeepL 翻訳 ────────────────────────────────────
def translate_to_english(text: str) -> str:
    """DeepL APIで日本語→英語に翻訳する。失敗時は元のテキストを返す。"""
    url = "https://api-free.deepl.com/v2/translate"
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    data = {
        "text": [text],
        "target_lang": "EN-US",
    }
    try:
        res = requests.post(url, headers=headers, json=data, timeout=15)
        res.raise_for_status()
        translated = res.json()["translations"][0]["text"]
        return translated
    except Exception as e:
        print(f"  DeepL翻訳エラー: {e}")
        return text


# ── 検索キーワード抽出（翻訳不要な場合） ────────────
def extract_english_keywords(product_name: str) -> str:
    """英語が多い商品名から主要キーワードを抽出する。"""
    name = re.sub(r'^\d+\s+', '', product_name.strip())
    english_parts = re.findall(r"[A-Za-z][A-Za-z0-9\-\.\'&]*", name)
    meaningful = [p for p in english_parts if len(p) >= 2 and not p.isdigit()]
    if len(meaningful) >= 3:
        return " ".join(meaningful[:5])
    return name[:40].strip()


# ── Keepa キーワード検索 ──────────────────────────
def search_keepa(term: str) -> tuple:
    """Keepa Search APIで検索。トークン不足時は即終了。429時は1回だけリトライ。"""
    url = "https://api.keepa.com/search"
    params = {
        "key": KEEPA_API_KEY,
        "domain": 1,
        "type": "product",
        "term": term,
        "limit": 2,  # 返却件数を2件に絞ってトークン節約（2件=1トークン）
    }
    for attempt in range(2):  # リトライは1回まで
        # リクエスト前にトークン残量確認（1検索≒10トークン消費）
        tokens = get_keepa_tokens_remaining()
        if tokens < TOKEN_THRESHOLD:
            print(f"  ⚠️ Keepaトークン残量不足 ({tokens})。処理を中断します。")
            raise SystemExit("Keepaトークン不足")

        try:
            res = requests.get(url, params=params, timeout=30)

            if res.status_code == 429:
                if attempt == 0:
                    print(f"  Keepaレート制限 (429)。{RETRY_WAIT:.0f}秒待機してリトライ...")
                    time.sleep(RETRY_WAIT)
                    continue
                else:
                    print(f"  Keepaレート制限 (429)。リトライ上限に達しました。")
                    return [], []

            res.raise_for_status()
            data = res.json()
            # レスポンス構造: {"products": [{asin, title, ...}, ...]}
            products = data.get("products") or []
            asin_list = [p["asin"] for p in products if "asin" in p]
            tokens_consumed = data.get("tokensConsumed", "不明")
            tokens_left = data.get("tokensLeft", "不明")
            print(f"  [ASIN件数]: {len(asin_list)} / トークン消費: {tokens_consumed} / 残り: {tokens_left}")
            return asin_list, products

        except SystemExit:
            raise
        except Exception as e:
            print(f"  Keepa Search APIエラー: {e}")
            return [], []

    return [], []


# ── 信頼度判定 ────────────────────────────────────
def judge_confidence(original_name: str, keepa_title: str) -> str:
    """元の商品名（英訳後）とKeepaタイトルを比較して信頼度を返す。"""
    if not keepa_title:
        return "LOW"

    def extract_tokens(text):
        tokens = re.findall(r'[A-Za-z0-9]+', text.upper())
        return set(t for t in tokens if len(t) > 2)

    orig_tokens = extract_tokens(original_name)
    keepa_tokens = extract_tokens(keepa_title)

    if not orig_tokens:
        return "LOW"

    matched = orig_tokens & keepa_tokens
    match_ratio = len(matched) / len(orig_tokens)

    if match_ratio >= 0.4 and len(matched) >= 2:
        return "HIGH"
    return "LOW"


# ── メイン処理 ────────────────────────────────────
def main():
    print("=== ASIN補完スクリプト開始 ===")

    sheet = get_sheet()
    all_rows = sheet.get_all_values()
    rows = all_rows[1:]
    print(f"総行数: {len(rows)}")

    unchecked = []
    for i, row in enumerate(rows):
        asin_val = row[COL_ASIN].strip() if len(row) > COL_ASIN else ""
        name_val = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        if asin_val == "" and name_val != "":
            unchecked.append((i + 1, row))

    print(f"未処理件数: {len(unchecked)}")

    if not unchecked:
        print("未処理商品なし。終了。")
        return

    # Keepaトークン残量を事前確認
    keepa_tokens = get_keepa_tokens_remaining()
    if keepa_tokens == 0:
        print("⚠️ Keepaトークンが不足しています。次回実行まで待機してください。")
        return

    # DeepL残り文字数を事前確認
    deepl_remaining = get_deepl_chars_remaining()
    deepl_available = deepl_remaining > DEEPL_CHARS_SAFETY_MARGIN
    if not deepl_available:
        print(f"⚠️ DeepL残り文字数が{DEEPL_CHARS_SAFETY_MARGIN:,}文字以下のため翻訳をスキップします（英語部分のみで検索）")

    target = unchecked[:SEARCH_PER_RUN]
    print(f"今回処理: {len(target)}件\n")

    translated_count = 0
    high_count = 0
    processed_count = 0

    for sheet_row_idx, row in target:
        product_name = row[COL_NAME].strip()

        # 翻訳 or 英語抽出
        if deepl_available and contains_japanese(product_name):
            search_term = translate_to_english(product_name)
            translated_count += 1
            print(f"  [翻訳] {product_name[:30]}...")
            print(f"       → {search_term[:50]}")

            # 翻訳後に残り文字数を再チェック（安全マージン到達で以降の翻訳を停止）
            deepl_remaining -= len(product_name)
            if deepl_remaining <= DEEPL_CHARS_SAFETY_MARGIN:
                print(f"⚠️ DeepL残り文字数が{DEEPL_CHARS_SAFETY_MARGIN:,}文字以下になりました。以降は翻訳をスキップします。")
                deepl_available = False
        else:
            search_term = extract_english_keywords(product_name)
            print(f"  [英語抽出] {search_term[:50]}")

        asin_list, product_list = search_keepa(search_term)

        if not asin_list:
            asin_result = "NOT FOUND"
            confidence = "LOW"
        else:
            asin_result = asin_list[0]
            keepa_title = product_list[0].get("title", "") if product_list else ""
            if keepa_title:
                print(f"  [Keepaタイトル]: {keepa_title[:60]}")
            confidence = judge_confidence(search_term, keepa_title)

        if confidence == "HIGH":
            high_count += 1

        print(f"    → ASIN: {asin_result} / 信頼度: {confidence}")

        # 1件処理するたびに即書き込み（途中停止しても結果を無駄にしない）
        asin_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_ASIN + 1)
        conf_cell = gspread.utils.rowcol_to_a1(sheet_row_idx + 1, COL_CONFIDENCE + 1)
        sheet.batch_update([
            {"range": asin_cell, "values": [[asin_result]]},
            {"range": conf_cell, "values": [[confidence]]},
        ])

        # NOT FOUNDの場合はASIN候補セルを赤背景にする
        if asin_result == "NOT FOUND":
            sheet.format(asin_cell, {
                "backgroundColor": {"red": 0.96, "green": 0.78, "blue": 0.78}
            })

        processed_count += 1

        time.sleep(REQUEST_INTERVAL)

    # フォントをArialに設定（ASIN候補・信頼度列）
    if processed_count > 0:
        sheet.format("D2:E20000", {"textFormat": {"fontFamily": "Arial"}})

    print(f"\n=== ASIN補完スクリプト完了 ===")
    print(f"処理件数: {processed_count}件（うち翻訳: {translated_count}件、HIGH: {high_count}件）")
    if processed_count < len(target):
        print(f"トークン不足により{len(target) - processed_count}件は次回に持ち越しました")
    print(f"次回実行で続きの{len(unchecked) - processed_count}件を処理します")


if __name__ == "__main__":
    main()
