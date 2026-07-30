"""
自社製品レビュークーポン（リポソーム型ビタミンC・アルロース、計12件）を、隔月（2ヶ月ごと）で
自動更新する。

運用ルール（ヒアリング内容）:
  - 各クーポンの「現在の終了日」を起点に、次の2ヶ月間（終了日の翌日〜2ヶ月後の末日）を作成する
    （「今日から2ヶ月後」という固定計算ではなく、実際の期限を起点にすることで、1件だけ更新が
    遅れても自己修復的に正しい周期へ戻る）
  - 作成は、現在の終了日が属する月の20日以降
  - ただし週末・セールイベント期間中（クーポン名に「先着」を含むものが有効期間中）は避ける
  - 既に次周期分を作成済みなら二重発行しない

rakuten_coupon_monthly.py（LINE登録限定・レビュー投稿の2つの常設クーポン、毎月更新）とは
周期・対象数・画像URL上書きの要否が異なる別物のため、独立したスクリプトにしている。

このスクリプトは発行のみを行う。Chatworkへの結果報告まで自動で行う。
"""

import os
import sys
import calendar
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree

import requests

from rakuten_coupon_api import auth_headers, search_all, get_coupon, build_issue_xml, issue_coupon

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
# 手動実行で日付の判定を飛ばしたいとき用（週末・イベント・20日前後の判定を無視する）
FORCE = os.environ.get("FORCE", "false").lower() == "true"

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]

CW_TOKEN = os.environ.get("CW_TOKEN", "")
CW_ROOM_ID = "60101971"
CW_MENTION = "[To:2618849]Ryo Higuchiさん"
CW_TITLE_PREFIX = "楽天クーポン更新"
CW_INTRO = "下記自社製品レビュークーポンを更新しました。\nテンプレートの更新をお願いします。"

# コピー先の画像URL。元のcouponImageは「couponImage.not_available_url」エラーになるため、
# RMS管理画面のR-Cabinet（cabinet/フォルダ）に登録し直した画像に統一して差し替える。
IMAGE_URL = "https://image.rakuten.co.jp/americana/cabinet/logo1.jpg"

PERIOD_MONTHS = 2      # 何ヶ月分の期間を作成するか
CREATE_FROM_DAY = 20   # 現在の終了日が属する月の、何日以降に作成するか

# 対象12件のクーポン名（コピー元検索・次周期の既存チェックに使う。名前は周期をまたいで固定）
TARGET_COUPON_NAMES = [
    "リポソーム型ビタミンC（3個）の写真付きレビュー投稿+200円OFF",
    "Dr.Plusリポソーム型ビタミンC（3個）のレビュー投稿で600円OFF",
    "リポソーム型ビタミンC（2個）の写真付きレビュー投稿+200円OFF",
    "Dr.Plusリポソーム型ビタミンC（2個）のレビュー投稿で300円OFF",
    "リポソーム型ビタミンC（1個）の写真付きレビュー投稿+100円OFF",
    "Dr.Plusリポソーム型ビタミンC（1個）のレビュー投稿で300円OFF",
    "アルロース（5個セット）の写真付きレビュー投稿で+200円OFF",
    "アルロース（5個セット）のレビュー投稿で1000円OFF",
    "アルロース（3個セット）の写真付きレビュー投稿で+200円OFF",
    "アルロース（3個セット）のレビュー投稿で500円OFF",
    "アルロース（1個）の写真付きレビュー投稿で+100円OFF",
    "アルロース（1個）のレビュー投稿で300円OFF",
]

# イベント（セール）中かどうかを判定するためのクーポン名の目印（rakuten_coupon_monthly.pyと同じ）
EVENT_NAME_MARKERS = ["先着"]


# ══ Chatwork 通知 ═════════════════════════════════
def post_chatwork(room_id: str, body: str):
    if not room_id:
        print("  Chatworkルームidが未設定のため通知しません。")
        return
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のため通知しません。")
        return
    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data={"body": body},
            timeout=30,
        )
        print(f"  Chatwork通知: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
    except Exception as e:
        print(f"  Chatwork通知に失敗しました: {e}")


def format_period_label(start: str, end: str) -> str:
    def fmt(v):
        dt = parse_rms_datetime(v)
        return dt.strftime("%Y-%m-%d %H:%M") if dt else v
    return f"{fmt(start)} 〜 {fmt(end)}"


def build_report(results: list) -> str:
    """発行結果からChatwork報告メッセージを組み立てる。"""
    issued = [r for r in results if r["status"] == "issued"]
    periods = {format_period_label(r["start"], r["end"]) for r in issued}
    title = f"{CW_TITLE_PREFIX}（{'・'.join(sorted(periods))}）" if periods else CW_TITLE_PREFIX

    lines = [CW_MENTION, f"[info][title]{title}[/title]", CW_INTRO, ""]
    for r in issued:
        lines.append(f"■ {r['name']}")
        lines.append(f"クーポンコード: {r['code']}")
        lines.append(f"取得URL: {r['url']}")
        lines.append("")

    failed = [r for r in results if r["status"] == "error"]
    if failed:
        lines.append("---")
        lines.append("以下は発行に失敗しました。手動で確認してください：")
        for r in failed:
            lines.append(f"・{r['name']}: {r['message']}")
        lines.append("")

    lines.append("[/info]")
    return "\n".join(lines)


# ══ 日付まわり ════════════════════════════════════
def parse_rms_datetime(value: str):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def add_months(dt: datetime, months: int) -> datetime:
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    return dt.replace(year=year, month=month)


def next_period(current_end: datetime) -> tuple:
    """現在の終了日時の翌日00:00:00 〜 PERIOD_MONTHS ヶ月後の末日23:59:59 を返す。"""
    start = (current_end + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_month_base = add_months(start, PERIOD_MONTHS - 1)
    last_day = calendar.monthrange(end_month_base.year, end_month_base.month)[1]
    end = end_month_base.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0)
    return start, end


def to_rms_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def is_event_day(today: datetime, coupons: list) -> tuple:
    """今日がセールイベント期間中かを判定する。(判定, 理由) を返す。"""
    for c in coupons:
        name = c.get("couponName", "")
        if name in TARGET_COUPON_NAMES:
            continue
        if not any(marker in name for marker in EVENT_NAME_MARKERS):
            continue
        start = parse_rms_datetime(c.get("couponStartDate", ""))
        end = parse_rms_datetime(c.get("couponEndDate", ""))
        if start and end and start <= today <= end:
            return True, f"セールクーポン「{name}」が期間中（{c.get('couponStartDate')}〜{c.get('couponEndDate')}）"
    return False, ""


# ══ 1クーポン分の処理 ═════════════════════════════
def process_one(name: str, coupons: list, today: datetime, headers: dict,
                 event_today: bool, event_reason: str) -> dict:
    result = {"name": name, "status": "skipped", "message": "", "start": "", "end": "", "code": None, "url": None}

    matches = [c for c in coupons if c.get("couponName") == name]
    if not matches:
        result["status"] = "error"
        result["message"] = "同名のクーポンが見つかりません。名前が変わっていないか確認してください。"
        return result

    source = max(matches, key=lambda c: c.get("couponStartDate", ""))
    current_end = parse_rms_datetime(source.get("couponEndDate", ""))
    if current_end is None:
        result["status"] = "error"
        result["message"] = f"終了日時が読み取れません（{source.get('couponEndDate')}）"
        return result

    # 「現在の終了日が属する月のCREATE_FROM_DAY日」が発行トリガー。
    # 終了月より前ならまだ早い、終了月のCREATE_FROM_DAY日以降（＝期限を過ぎて追いついた場合も含む）ならOK。
    trigger_date = current_end.replace(day=CREATE_FROM_DAY, hour=0, minute=0, second=0, microsecond=0)
    if not FORCE and today < trigger_date:
        result["message"] = (
            f"まだ発行タイミングではありません（発行予定日: {trigger_date.strftime('%Y-%m-%d')}、"
            f"終了日: {current_end.strftime('%Y-%m-%d')}）"
        )
        return result

    new_start, new_end = next_period(current_end)

    # すでに次周期分を作っていないか確認（二重発行の防止）
    already = [c for c in matches if c.get("couponStartDate", "").startswith(new_start.strftime("%Y-%m"))]
    if already:
        result["status"] = "already"
        result["message"] = f"既に{new_start.strftime('%Y年%m月')}分が存在します（{already[0].get('couponCode')}）"
        return result

    if not FORCE:
        if today.weekday() >= 5:
            result["message"] = f"週末です（{'土曜' if today.weekday() == 5 else '日曜'}）"
            return result
        if event_today:
            result["message"] = f"イベント期間中です: {event_reason}"
            return result

    start_str, end_str = to_rms_str(new_start), to_rms_str(new_end)
    result["start"], result["end"] = start_str, end_str

    src, nested = get_coupon(headers, source["couponCode"])
    if src is None:
        result["status"] = "error"
        result["message"] = "コピー元の詳細取得に失敗しました"
        return result

    print(f"    対象商品: {nested.get('itemUrls')}")
    print(f"    クーポン画像を差し替えます（元の値: {src.get('couponImage')} → {IMAGE_URL}）")
    src["couponImage"] = IMAGE_URL

    xml = build_issue_xml(src, start_str, end_str, nested)

    if DRY_RUN:
        print(f"    【DRY RUN】発行対象: {name}")
        print(f"      新しい期間: {start_str} 〜 {end_str}")
        result["status"] = "issued"
        result["code"] = "（DRY RUN）"
        result["url"] = "（DRY RUN）"
        return result

    success, message, code, url = issue_coupon(headers, xml)
    print(f"    → {message}")
    if success:
        result["status"] = "issued"
        result["code"] = code
        result["url"] = url
    else:
        result["status"] = "error"
        result["message"] = message
    return result


# ══ メイン ════════════════════════════════════════
def main():
    print("=== 自社製品レビュークーポン 隔月自動発行 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際には発行しません")
    if FORCE:
        print("※ FORCE指定：日付・週末・イベントの判定を無視します")

    today = datetime.now(JST)
    print(f"今日: {today.strftime('%Y-%m-%d (%a) %H:%M')}")

    headers = auth_headers(SERVICE_SECRET, LICENSE_KEY)
    try:
        coupons = search_all(headers)
    except Exception as e:
        print(f"クーポン一覧の取得に失敗しました: {e}")
        sys.exit(1)
    print(f"既存クーポン: {len(coupons)}件")

    event_today, event_reason = is_event_day(today, coupons)
    if event_today:
        print(f"本日はイベント期間中: {event_reason}")

    results = []
    for name in TARGET_COUPON_NAMES:
        print(f"\n--- {name} ---")
        r = process_one(name, coupons, today, headers, event_today, event_reason)
        print(f"  → {r['status']}: {r['message']}")
        results.append(r)

    issued = [r for r in results if r["status"] == "issued"]
    errored = [r for r in results if r["status"] == "error"]
    print(f"\n=== 完了: 発行{len(issued)}件 / エラー{len(errored)}件 / "
          f"対象外{len(results) - len(issued) - len(errored)}件 ===")

    if issued or errored:
        post_chatwork(CW_ROOM_ID, build_report(results))
    else:
        print("発行対象・エラーとも無かったため、Chatworkへは通知しません。")

    print("=== 自社製品レビュークーポン 隔月自動発行 完了 ===")


if __name__ == "__main__":
    main()
