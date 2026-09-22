"""
Alt Offer自動化の検証用: 指定ASINについてKeepaから商品名・画像・特徴（feature bullets）が
取得できるか確認する（読み取りのみ、書き込みは一切行わない）。

背景（2026-09-22、ユーザー確認済み）:
  楽天/WowmaのAlt Offerケース対応メールを半自動化するにあたり、代替商品（Amazon.comの
  US商品）の商品名・画像・特徴を取得する必要がある。SP-API（Shop LA!=JP、
  US International Service=EU）はUS本体のカタログ情報を取得する権限を持たない可能性が
  高いため、既存契約のKeepa APIで代替できるか確認する。
"""
import os

import requests

KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]
ASIN = os.environ.get("TEST_ASIN", "B0DT4ZZXD2")


def print_deepl_usage() -> None:
    url = "https://api-free.deepl.com/v2/usage"
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        print(f"DeepL usage status: {res.status_code}")
        print(f"DeepL usage body: {res.text}")
    except Exception as e:
        print(f"DeepL使用量取得失敗: {e}")


def translate_to_japanese(text: str) -> str:
    """DeepL APIで英語→日本語に翻訳する。失敗時は元のテキストを返す。"""
    url = "https://api-free.deepl.com/v2/translate"
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    data = {"text": [text], "target_lang": "JA"}
    try:
        res = requests.post(url, headers=headers, json=data, timeout=15)
        if res.status_code != 200:
            print(f"  DeepL翻訳エラー: status={res.status_code} body={res.text[:500]}")
            return text
        return res.json()["translations"][0]["text"]
    except Exception as e:
        print(f"  DeepL翻訳エラー(例外): {e}")
        return text


def main():
    key = DEEPL_API_KEY
    masked = f"{key[:6]}...{key[-6:]} (長さ{len(key)}文字)" if len(key) > 12 else "(短すぎて表示できません)"
    print(f"使用中のDEEPL_API_KEY: {masked}\n")

    print(f"=== Keepa商品情報確認: {ASIN} ===\n")
    url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain=1&asin={ASIN}&stats=1&images=1"
    res = requests.get(url, timeout=60)
    res.raise_for_status()
    data = res.json()
    products = data.get("products", [])
    print(f"tokensLeft: {data.get('tokensLeft')}")
    if not products:
        print("商品が見つかりませんでした")
        return

    p = products[0]
    print(f"title: {p.get('title')}")
    print(f"brand: {p.get('brand')}")
    print(f"imagesCSV: {p.get('imagesCSV')}")
    if p.get("imagesCSV"):
        first_image = p["imagesCSV"].split(",")[0]
        print(f"画像URL(推定・imagesCSVから): https://images-na.ssl-images-amazon.com/images/I/{first_image}")
    print(f"images: {p.get('images')}")

    title = p.get("title") or ""
    features = (p.get("features") or [])[:3]

    print_deepl_usage()

    print("\n=== DeepL最小テスト（'hello'を翻訳） ===")
    print(f"結果: {translate_to_japanese('hello')}")

    print("\n=== DeepL翻訳結果 ===")
    print(f"商品名(日本語): {translate_to_japanese(title)}")
    print("特徴(日本語、上位3個):")
    for i, f in enumerate(features, 1):
        print(f"  {i}. {translate_to_japanese(f)}")


if __name__ == "__main__":
    main()
