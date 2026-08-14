"""一時：Keepaのトークン残量・補充レートを確認する（読み取り専用）。"""

import os
import requests

KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]


def main():
    url = f"https://api.keepa.com/token?key={KEEPA_API_KEY}"
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    print(data)


if __name__ == "__main__":
    main()
