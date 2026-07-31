"""
Founder店舗の「5,0の付く日クーポン」（毎月5・10・15・20・25・30日にそれぞれ1日だけ有効、
計6件）を、毎月自動更新する。

運用ルール（ヒアリング内容）:
  - 6件とも完全に同名のクーポンで、有効期間（開始日の「日」）で区別する
  - 次周期は「月だけ+1、日・時刻はそのまま」で計算する
    （例: 2026/07/20 00:00〜2026/07/20 23:59 → 2026/08/20 00:00〜2026/08/20 23:59）
  - 作成は、各グループの現在の終了日が属する月の20日以降
  - 既に次周期分（年月日まで一致）を作成済みなら二重発行しない

rakuten_coupon_review_batch.py（自社製品レビュークーポン、Americana店舗、隔月、名前ごとに1件）
とは、店舗・周期計算・「同名6件を日付で区別する」点が異なる別物のため、独立したスクリプトにしている。

このスクリプトは発行のみを行う。Chatworkへの結果報告まで自動で行う。
"""

import os
import sys
import calendar
from datetime import datetime, timezone, timedelta

import requests

from rakuten_coupon_api import auth_headers, search_all, get_coupon, build_issue_xml, issue_coupon

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
# 手動実行で日付の判定を飛ばしたいとき用（20日判定を無視する）
FORCE = os.environ.get("FORCE", "false").lower() == "true"

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_2"]
SHOP_NAME = os.environ["RAKUTEN_SHOP_NAME_2"]

CW_TOKEN = os.environ.get("CW_TOKEN", "")
# テスト実行時に本番ルームへ誤報告しないよう、環境変数で上書きできるようにする（空文字なら本番既定値）
CW_ROOM_ID = os.environ.get("CW_ROOM_ID") or "60101971"
CW_ASSIGNEE_ID = "2158846"  # Yoko Matsusakaさん（[To:2158846]と同じアカウントID）
CW_MENTION = "[To:2158846]Yoko Matsusakaさん"
CW_TITLE_PREFIX = "楽天クーポン更新（5,0の付く日）"
CW_INTRO = "下記クーポンを更新しました。"
CW_TASK_DUE_DAYS = 7  # タスクの期限：発行日から何日後か

# 実際のクーポン名は「（店舗名）で使える5,0の付く日クーポン」。店舗名を直書きしないよう
# GitHub Secretsの店舗名から組み立てる。
TARGET_COUPON_NAME = f"{SHOP_NAME}で使える5,0の付く日クーポン"
TARGET_DAYS = [5, 10, 15, 20, 25, 30]

CREATE_FROM_DAY = 20  # 現在の終了日が属する月の、何日以降に作成するか


# ══ Chatwork 通知 ═════════════════════════════════
def post_chatwork_task(room_id: str, to_ids: str, body: str, due_days: int = None):
    """
    普通のメッセージではなく、Chatworkの「タスク」として作成する
    （担当者に割り当てられ、Chatwork上のタスク一覧にも表示される）。
    """
    if not room_id or not to_ids:
        print("  Chatworkルームid/担当者idが未指定のためタスクを作成しません。")
        return
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のためタスクを作成しません。")
        return

    data = {"body": body, "to_ids": to_ids}
    if due_days is not None:
        # 時刻を含めたまま計算すると、limit_type=date側でのタイムゾーン解釈次第で
        # 日付が前後にずれることがあるため、対象日のJST 0時ちょうどに正規化する
        due = (datetime.now(JST) + timedelta(days=due_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        data["limit"] = int(due.timestamp())
        data["limit_type"] = "date"
        print(f"    タスク期限: {due.strftime('%Y-%m-%d')}（limit={data['limit']}）")

    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/tasks",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data=data,
            timeout=30,
        )
        print(f"  Chatworkタスク作成: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
    except Exception as e:
        print(f"  Chatworkタスク作成に失敗しました: {e}")


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
        lines.append(f"■ {r['day']}日分")
        lines.append(f"クーポンコード: {r['code']}")
        lines.append(f"取得URL: {r['url']}")
        lines.append("")

    failed = [r for r in results if r["status"] == "error"]
    if failed:
        lines.append("---")
        lines.append("以下は発行に失敗しました。手動で確認してください：")
        for r in failed:
            lines.append(f"・{r['day']}日分: {r['message']}")
        lines.append("")

    lines.append("[/info]")
    return "\n".join(lines)


# ══ 日付まわり ════════════════════════════════════
def parse_rms_datetime(value: str):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def add_one_month_same_day(dt: datetime) -> datetime:
    """年月だけ+1し、日・時・分・秒はそのまま維持する。翌月にその日が存在しない場合のみ
    （30日始まりが2月に当たる場合など）月末日にクランプする。"""
    total = dt.month - 1 + 1
    year = dt.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def next_period(start: datetime, end: datetime) -> tuple:
    return add_one_month_same_day(start), add_one_month_same_day(end)


def to_rms_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def group_by_day(coupons: list) -> dict:
    """対象クーポン名のものを、開始日の「日」でグループ化する。"""
    groups = {}
    for c in coupons:
        if c.get("couponName") != TARGET_COUPON_NAME:
            continue
        start = parse_rms_datetime(c.get("couponStartDate", ""))
        if start is None:
            continue
        groups.setdefault(start.day, []).append(c)
    return groups


# ══ 1グループ（1日分）の処理 ══════════════════════
def process_one(day: int, group: list, today: datetime, headers: dict) -> dict:
    result = {"day": day, "status": "skipped", "message": "", "start": "", "end": "", "code": None, "url": None}

    if not group:
        result["status"] = "error"
        result["message"] = f"{day}日分の既存クーポンが見つかりません。"
        return result

    source = max(group, key=lambda c: c.get("couponStartDate", ""))
    current_start = parse_rms_datetime(source.get("couponStartDate", ""))
    current_end = parse_rms_datetime(source.get("couponEndDate", ""))
    if current_start is None or current_end is None:
        result["status"] = "error"
        result["message"] = f"開始/終了日時が読み取れません（{source.get('couponStartDate')} 〜 {source.get('couponEndDate')}）"
        return result

    # 「現在の終了日が属する月のCREATE_FROM_DAY日」が発行トリガー
    trigger_date = current_end.replace(day=CREATE_FROM_DAY, hour=0, minute=0, second=0, microsecond=0)
    if not FORCE and today < trigger_date:
        result["message"] = (
            f"まだ発行タイミングではありません（発行予定日: {trigger_date.strftime('%Y-%m-%d')}、"
            f"終了日: {current_end.strftime('%Y-%m-%d')}）"
        )
        return result

    new_start, new_end = next_period(current_start, current_end)

    # すでに次周期分を作っていないか確認（年月日まで一致するかで判定＝二重発行の防止）
    target_date_str = new_start.strftime("%Y-%m-%d")
    already = [c for c in group if c.get("couponStartDate", "").startswith(target_date_str)]
    if already:
        result["status"] = "already"
        result["message"] = f"既に{target_date_str}分が存在します（{already[0].get('couponCode')}）"
        return result

    start_str, end_str = to_rms_str(new_start), to_rms_str(new_end)
    result["start"], result["end"] = start_str, end_str

    src, nested = get_coupon(headers, source["couponCode"])
    if src is None:
        result["status"] = "error"
        result["message"] = "コピー元の詳細取得に失敗しました"
        return result

    print(f"    対象商品: {nested.get('itemUrls')}")

    xml = build_issue_xml(src, start_str, end_str, nested)

    if DRY_RUN:
        print(f"    【DRY RUN】発行対象: {day}日分")
        print(f"      新しい期間: {start_str} 〜 {end_str}")
        result["status"] = "issued"
        # 実際には発行しないので、確認しやすいようコピー元（＝現在有効なクーポン）の
        # コード・URL・期間をそのまま表示する
        result["code"] = src.get("couponCode")
        result["url"] = src.get("pcGetUrl")
        result["start"] = src.get("couponStartDate", start_str)
        result["end"] = src.get("couponEndDate", end_str)
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
    print("=== 5,0の付く日クーポン 自動発行 開始（Founder店舗） ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際には発行しません")
    if FORCE:
        print("※ FORCE指定：日付の判定を無視します")

    today = datetime.now(JST)
    print(f"今日: {today.strftime('%Y-%m-%d (%a) %H:%M')}")

    headers = auth_headers(SERVICE_SECRET, LICENSE_KEY)
    try:
        coupons = search_all(headers)
    except Exception as e:
        print(f"クーポン一覧の取得に失敗しました: {e}")
        sys.exit(1)
    print(f"既存クーポン: {len(coupons)}件")

    groups = group_by_day(coupons)

    results = []
    for day in TARGET_DAYS:
        print(f"\n--- {day}日分 ---")
        r = process_one(day, groups.get(day, []), today, headers)
        print(f"  → {r['status']}: {r['message']}")
        results.append(r)

    issued = [r for r in results if r["status"] == "issued"]
    errored = [r for r in results if r["status"] == "error"]
    print(f"\n=== 完了: 発行{len(issued)}件 / エラー{len(errored)}件 / "
          f"対象外{len(results) - len(issued) - len(errored)}件 ===")

    if issued or errored:
        post_chatwork_task(CW_ROOM_ID, CW_ASSIGNEE_ID, build_report(results), due_days=CW_TASK_DUE_DAYS)
    else:
        print("発行対象・エラーとも無かったため、Chatworkへは通知しません。")

    print("=== 5,0の付く日クーポン 自動発行 完了 ===")


if __name__ == "__main__":
    main()
