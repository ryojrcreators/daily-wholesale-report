"""
社内システム（app.jrcreators.com）の受注データから、Robot-in（楽天の受注管理ツール）の
「注文ステータス」画面のようなタイル型ダッシュボードをHTMLで生成する。

直近LOOKBACK_DAYS日分（created_time基準）のso-headsデータを取得し、対象モール
（TARGET_SHOPS）に絞ってorder_status列の値ごとに件数を集計する。各ステータスの
タイルをクリックすると、該当注文の明細テーブルが開く（データは静的HTMLに埋め込み
済みなので、サーバー側の追加処理なしでその場で開閉できる）。

生成したHTMLは自社サイトへの自動デプロイは行わない（ユーザーがWinSCPで手動
アップロードする運用のため）。このスクリプトはファイルを出力するところまでを担当する。
"""

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from rakuten_ship_notify import login, fetch_recent_orders

LA_TZ = ZoneInfo("America/Los_Angeles")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "21"))
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "order_status_dashboard.html")

# 表示対象のモール（社内システム上のshop_name）。それ以外（Amazon系・空欄・UK/DE/FR等）は除外
TARGET_SHOPS = ["アメリカーナ", "Founder", "LA Express", "American Kitchen", "Meta Store"]

COL_ORDER_NUMBER = 0
COL_CREATED_TIME = 1
COL_SHOP_NAME = 4
COL_ORDER_STATUS = 14
COL_SHIP_METHOD = 23
COL_TRACKING_NUM = 24

# ステータスの色分け（Robot-inの画面を参考に、注意が必要なものを強調）
STATUS_COLORS = {
    "Shipped": "green",
    "Cancelled": "gray",
    "Hold": "amber",
    "Unreceived": "coral",
    "Picking": "blue",
    "Packing": "blue",
    "Entered": "purple",
    "Imported": "purple",
    "Partially Shipped": "amber",
    "Japan Pending": "coral",
}
DEFAULT_COLOR = "gray"

COLOR_HEX = {
    "green":  ("#EAF3DE", "#27500A"),
    "gray":   ("#F1EFE8", "#444441"),
    "amber":  ("#FAEEDA", "#633806"),
    "coral":  ("#FAECE7", "#712B13"),
    "blue":   ("#E6F1FB", "#0C447C"),
    "purple": ("#EEEDFE", "#3C3489"),
}


def fetch_orders():
    end = datetime.now(LA_TZ)
    start = end - timedelta(days=LOOKBACK_DAYS)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, device_scale_factor=2)
        page = context.new_page()
        login(page)
        header, rows = fetch_recent_orders(
            page, context,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )
        browser.close()

    return rows or [], start, end


def build_order_records(rows):
    """
    order_number単位で重複除去した明細を返す（1注文に複数商品行があるため、
    注文単位の状況・代表商品名を1行にまとめる）。TARGET_SHOPS以外は除外する。
    戻り値: {status: [record, ...]}
    """
    seen = {}
    for row in rows:
        if len(row) <= COL_ORDER_STATUS:
            continue
        shop_name = row[COL_SHOP_NAME].strip() if len(row) > COL_SHOP_NAME else ""
        if shop_name not in TARGET_SHOPS:
            continue
        order_number = row[COL_ORDER_NUMBER].strip()
        if not order_number or order_number in seen:
            continue

        seen[order_number] = {
            "shop": shop_name,
            "order_number": order_number,
            "created_time": row[COL_CREATED_TIME].strip() if len(row) > COL_CREATED_TIME else "",
            "ship_method": row[COL_SHIP_METHOD].strip() if len(row) > COL_SHIP_METHOD else "",
            "tracking_num": row[COL_TRACKING_NUM].strip() if len(row) > COL_TRACKING_NUM else "",
            "status": (row[COL_ORDER_STATUS].strip() if len(row) > COL_ORDER_STATUS else "") or "(未設定)",
        }

    by_status = defaultdict(list)
    for record in seen.values():
        by_status[record["status"]].append(record)
    for records in by_status.values():
        records.sort(key=lambda r: r["created_time"], reverse=True)
    return by_status


def build_html(by_status, start, end, generated_at, memo_api_url):
    status_counts = Counter({status: len(records) for status, records in by_status.items()})
    total = sum(status_counts.values())

    tiles = []
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        color = STATUS_COLORS.get(status, DEFAULT_COLOR)
        bg, fg = COLOR_HEX[color]
        shop_breakdown = Counter(r["shop"] for r in by_status[status])
        breakdown_html = "".join(
            f'<div class="shop-row"><span>{shop}</span><span>{cnt:,}</span></div>'
            for shop, cnt in shop_breakdown.most_common()
        )
        tile_id = f"tile-{len(tiles)}"
        tiles.append(f'''
        <button type="button" class="tile" data-status="{status}" data-target="{tile_id}"
                style="background:{bg}; color:{fg};" onclick="toggleDetail(this)">
          <div class="tile-count">{count:,}</div>
          <div class="tile-label">{status}</div>
          <div class="tile-breakdown">{breakdown_html}</div>
        </button>''')

    data_json = json.dumps(by_status, ensure_ascii=False).replace("</", "<\\/")
    memo_api_url_json = json.dumps(memo_api_url or "")

    return f'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>受注ステータス ダッシュボード</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #F7F6F2;
    --text-primary: #2C2C2A;
    --text-secondary: #5F5E5A;
    --border: #E3E1D9;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2rem;
    background: var(--bg);
    color: var(--text-primary);
    font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic", sans-serif;
  }}
  h1 {{
    font-size: 22px;
    font-weight: 500;
    margin: 0 0 4px;
  }}
  .meta {{
    color: var(--text-secondary);
    font-size: 13px;
    margin: 0 0 1.5rem;
  }}
  .total {{
    font-size: 14px;
    color: var(--text-secondary);
    margin-bottom: 1.5rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }}
  .tile {{
    border: none;
    border-radius: 12px;
    padding: 1rem 1.25rem;
    text-align: left;
    cursor: pointer;
    font-family: inherit;
    position: relative;
  }}
  .tile.active {{
    outline: 2px solid rgba(0,0,0,0.35);
  }}
  .tile-count {{
    font-size: 28px;
    font-weight: 500;
    line-height: 1.2;
  }}
  .tile-label {{
    font-size: 14px;
    font-weight: 500;
    margin-top: 2px;
  }}
  .tile-breakdown {{
    margin-top: 10px;
    padding-top: 8px;
    border-top: 0.5px solid rgba(0,0,0,0.12);
    font-size: 12px;
  }}
  .shop-row {{
    display: flex;
    justify-content: space-between;
    opacity: 0.85;
    padding: 1px 0;
  }}
  .detail {{
    margin-top: 1.5rem;
    background: #fff;
    border: 0.5px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.25rem;
    display: none;
  }}
  .detail.open {{
    display: block;
  }}
  .detail h2 {{
    font-size: 16px;
    font-weight: 500;
    margin: 0 0 12px;
  }}
  .detail-table-wrap {{
    max-height: 480px;
    overflow: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  th, td {{
    text-align: left;
    padding: 6px 10px;
    border-bottom: 0.5px solid var(--border);
    white-space: nowrap;
  }}
  th {{
    position: sticky;
    top: 0;
    background: #fff;
    color: var(--text-secondary);
    font-weight: 500;
  }}
  td.memo-cell {{
    white-space: normal;
    width: 240px;
  }}
  .memo-input {{
    width: 100%;
    border: 0.5px solid var(--border);
    border-radius: 6px;
    padding: 4px 6px;
    font-size: 13px;
    font-family: inherit;
    background: #fff;
  }}
  .memo-input:focus {{
    outline: none;
    border-color: var(--text-secondary);
  }}
  .memo-saved {{
    font-size: 11px;
    color: var(--text-secondary);
    margin-left: 6px;
    opacity: 0;
    transition: opacity 0.2s;
  }}
  .memo-saved.show {{
    opacity: 1;
  }}
  .footer {{
    margin-top: 2rem;
    font-size: 12px;
    color: var(--text-secondary);
  }}
</style>
</head>
<body>
  <h1>受注ステータス ダッシュボード</h1>
  <p class="meta">対象期間: {start.strftime('%Y/%m/%d')}〜{end.strftime('%Y/%m/%d')}（created_time基準、直近{LOOKBACK_DAYS}日） / 対象モール: {', '.join(TARGET_SHOPS)}</p>
  <p class="total">対象注文数: {total:,}件</p>
  <div class="grid">
    {''.join(tiles)}
  </div>

  <div class="detail" id="detail">
    <h2 id="detail-title"></h2>
    <div class="detail-table-wrap">
      <table>
        <thead>
          <tr>
            <th>店舗</th><th>受注番号</th><th>受注日時</th>
            <th>配送方法</th><th>追跡番号</th><th>メモ</th>
          </tr>
        </thead>
        <tbody id="detail-body"></tbody>
      </table>
    </div>
  </div>

  <p class="footer">生成日時: {generated_at.strftime('%Y/%m/%d %H:%M')}（PT）</p>

<script id="order-data" type="application/json">{data_json}</script>
<script>
  const ORDER_DATA = JSON.parse(document.getElementById('order-data').textContent);
  const MEMO_API_URL = {memo_api_url_json};
  let activeStatus = null;
  let memoCache = {{}};
  let memoLoadFailed = false;

  // メモはGoogle Apps Script経由でスプレッドシートに保存する（全PC・全ブラウザで共有される）。
  // ページを開いた直後、この読み込みが終わる前にタイルをクリックすると空欄のまま表示されて
  // しまうため、memoLoadPromiseをtoggleDetail側でawaitして読み込み完了を待ってから描画する。
  async function loadAllMemos() {{
    if (!MEMO_API_URL) return;
    try {{
      const res = await fetch(MEMO_API_URL);
      memoCache = await res.json();
    }} catch (e) {{
      memoLoadFailed = true;
    }}
  }}

  async function saveMemo(orderNumber, value) {{
    if (!MEMO_API_URL) return false;
    try {{
      await fetch(MEMO_API_URL, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'text/plain;charset=utf-8' }},
        body: JSON.stringify({{ order_number: orderNumber, memo: value }}),
      }});
      memoCache[orderNumber] = value;
      return true;
    }} catch (e) {{
      return false;
    }}
  }}

  function escapeHtml(s) {{
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }})[c]);
  }}

  async function toggleDetail(btn) {{
    const status = btn.getAttribute('data-status');
    const detail = document.getElementById('detail');
    document.querySelectorAll('.tile').forEach(t => t.classList.remove('active'));

    if (activeStatus === status) {{
      detail.classList.remove('open');
      activeStatus = null;
      return;
    }}

    activeStatus = status;
    btn.classList.add('active');
    document.getElementById('detail-title').textContent = '読み込み中…';
    detail.classList.add('open');

    await memoLoadPromise;

    if (activeStatus !== status) return; // 待っている間に別のタイルへ切り替わった場合は何もしない

    document.getElementById('detail-title').textContent = status + '（' + (ORDER_DATA[status] || []).length.toLocaleString() + '件）'
      + (memoLoadFailed ? '　※メモの読み込みに失敗しました' : '');

    const rows = (ORDER_DATA[status] || []).map(r => {{
      const memo = escapeHtml(memoCache[r.order_number] || '');
      return '<tr>' +
        '<td>' + escapeHtml(r.shop) + '</td>' +
        '<td>' + escapeHtml(r.order_number) + '</td>' +
        '<td>' + escapeHtml(r.created_time) + '</td>' +
        '<td>' + escapeHtml(r.ship_method) + '</td>' +
        '<td>' + escapeHtml(r.tracking_num) + '</td>' +
        '<td class="memo-cell">' +
          '<input type="text" class="memo-input" data-order="' + escapeHtml(r.order_number) + '" value="' + memo + '">' +
          '<span class="memo-saved">保存済み</span>' +
        '</td>' +
        '</tr>';
    }}).join('');
    document.getElementById('detail-body').innerHTML = rows || '<tr><td colspan="6">データがありません</td></tr>';
    detail.classList.add('open');
    detail.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
  }}

  document.getElementById('detail-body').addEventListener('change', async e => {{
    if (!e.target.classList.contains('memo-input')) return;
    const orderNumber = e.target.getAttribute('data-order');
    const badge = e.target.parentElement.querySelector('.memo-saved');
    const ok = await saveMemo(orderNumber, e.target.value.trim());
    badge.textContent = ok ? '保存済み' : '保存失敗';
    badge.classList.add('show');
    setTimeout(() => badge.classList.remove('show'), 1500);
  }});

  const memoLoadPromise = loadAllMemos();
</script>
</body>
</html>
'''


def main():
    print("=== 受注ステータス ダッシュボード生成 開始 ===")
    rows, start, end = fetch_orders()
    by_status = build_order_records(rows)

    total = sum(len(records) for records in by_status.values())
    print(f"対象注文数（{', '.join(TARGET_SHOPS)}）: {total}件")
    for status, records in sorted(by_status.items(), key=lambda x: -len(x[1])):
        print(f"  {status}: {len(records)}件")

    memo_api_url = os.environ.get("ORDER_MEMO_API_URL", "")
    html = build_html(by_status, start, end, datetime.now(LA_TZ), memo_api_url)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n{OUTPUT_PATH} に出力しました。")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
