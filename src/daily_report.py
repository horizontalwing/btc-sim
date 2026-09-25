"""
BTC フォワード紙トレード 日次レポート送信（trend_log.csv 版）
- logs/trend_log.csv から最新日のデータを集計
- SMA30 / SMA40 / SMA50 の3戦略 + buy&hold を比較
- Gmail で HTML メールを送信

2026-09-09 のフォワード紙トレード切替に伴い、旧 simulation_log.csv 版から差し替え。
（旧版は simulate.yml 停止後に読むログが更新されなくなり、
  "対象日のログ無し" で送信スキップ＝メールが届かない状態になっていた）
"""

import csv
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── 設定 ───────────────────────────────────────────────

LOG_FILE = "logs/trend_log.csv"

INITIAL_CAPITAL = 10000  # 各戦略・buy&hold の開始資金（円）

GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
# 送信先。REPORT_EMAIL が無ければ自分宛にフォールバック。
REPORT_EMAIL   = os.environ.get("REPORT_EMAIL", GMAIL_ADDRESS)

JST = timezone(timedelta(hours=9))

# trend_log.csv の列
FIELDNAMES = [
    "date", "price",
    "sma30", "sma40", "sma50",
    "t30_pos", "t30_equity",
    "t40_pos", "t40_equity",
    "t50_pos", "t50_equity",
    "buyhold_equity", "note",
]

# 戦略メタ（キー接頭辞, ラベル, カード色）
STRATEGIES = [
    ("t30", "SMA30", "#3498db"),
    ("t40", "SMA40", "#9b59b6"),
    ("t50", "SMA50", "#e67e22"),
]

# ── ヘルパー ─────────────────────────────────────────────

def load_logs():
    """trend_log.csv を読み、日付昇順の行リストを返す。"""
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f, fieldnames=FIELDNAMES)
        rows = list(reader)
    # 1行目がヘッダーなら読み飛ばす
    if rows and rows[0]["date"] == "date":
        rows = rows[1:]
    # 念のため日付でソート（昇順）
    rows = [r for r in rows if r.get("date")]
    rows.sort(key=lambda r: r["date"])
    return rows


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def pct(numer, denom):
    if not denom:
        return 0.0
    return numer / denom * 100


# ── HTML 部品 ─────────────────────────────────────────────

def strategy_card(label, color, equity, prev_equity, pos):
    """戦略カード（評価額・損益率・前日比・ポジション）。"""
    pnl        = equity - INITIAL_CAPITAL
    pnl_pct    = pct(pnl, INITIAL_CAPITAL)
    pnl_sign   = "+" if pnl >= 0 else ""
    pnl_color  = "#2ecc71" if pnl >= 0 else "#e74c3c"

    day_change = equity - prev_equity
    day_sign   = "+" if day_change >= 0 else ""
    day_color  = "#2ecc71" if day_change >= 0 else "#e74c3c"

    if pos is None:
        pos_html = ""
    elif pos == 1:
        pos_html = ("<div style='display:inline-block;margin-top:6px;padding:2px 8px;"
                    "font-size:11px;border-radius:10px;background:#e8f5e9;color:#2e7d32'>"
                    "● ロング保有</div>")
    else:
        pos_html = ("<div style='display:inline-block;margin-top:6px;padding:2px 8px;"
                    "font-size:11px;border-radius:10px;background:#f0f0f0;color:#888'>"
                    "○ 現金（待機）</div>")

    return f"""
    <td style='padding:12px;text-align:center;background:#fafafa;
               border:1px solid #eee;border-top:3px solid {color};
               border-radius:8px;width:25%;vertical-align:top'>
      <div style='font-size:12px;color:#888;margin-bottom:4px'>{label}</div>
      <div style='font-size:19px;font-weight:bold;color:#333'>{equity:,}円</div>
      <div style='font-size:12px;color:{pnl_color};margin-top:2px'>
        {pnl_sign}{pnl:,}円（{pnl_sign}{pnl_pct:.2f}%）
      </div>
      <div style='font-size:11px;color:{day_color};margin-top:4px'>
        前日比 {day_sign}{day_change:,}円
      </div>
      {pos_html}
    </td>"""


def build_price_chart(rows, n=7):
    """直近 n 日の価格推移を HTML バーチャートで返す。"""
    recent = rows[-n:] if len(rows) >= n else rows
    if not recent:
        return "<p style='color:#888'>データ不足</p>"

    prices = [to_int(r["price"]) for r in recent]
    dates  = [r["date"][5:] for r in recent]  # MM-DD

    min_p = min(prices)
    max_p = max(prices)
    rng   = max_p - min_p if max_p != min_p else 1
    first = prices[0]

    body = ""
    for d, p in zip(dates, prices):
        bar_pct = int((p - min_p) / rng * 80) + 10
        change  = p - first
        sign    = "+" if change >= 0 else ""
        chg_col = "#2ecc71" if change >= 0 else "#e74c3c"

        tag = ""
        if p == max_p:
            tag = "<span style='font-size:10px;color:#e74c3c;margin-left:4px'>▲高値</span>"
        elif p == min_p:
            tag = "<span style='font-size:10px;color:#3498db;margin-left:4px'>▼安値</span>"

        body += f"""
        <tr>
          <td style='padding:4px 8px 4px 0;font-size:12px;color:#888;white-space:nowrap'>{d}</td>
          <td style='padding:4px;width:100%'>
            <div style='background:#f0f0f0;border-radius:4px;height:18px'>
              <div style='background:linear-gradient(90deg,#3498db,#2980b9);
                          width:{bar_pct}%;height:100%;border-radius:4px'></div>
            </div>
          </td>
          <td style='padding:4px 0 4px 8px;font-size:12px;white-space:nowrap;text-align:right'>
            {p:,}円{tag}
          </td>
          <td style='padding:4px 0 4px 8px;font-size:11px;color:{chg_col};white-space:nowrap'>
            {sign}{change:,}
          </td>
        </tr>"""

    total  = prices[-1] - prices[0]
    tsign  = "+" if total >= 0 else ""
    tcolor = "#2ecc71" if total >= 0 else "#e74c3c"
    span   = f"{dates[0]}〜{dates[-1]}"

    return f"""
    <table style='width:100%;border-collapse:collapse'>{body}</table>
    <div style='margin-top:8px;font-size:12px;color:{tcolor}'>
      期間変動（{span}）: <strong>{tsign}{total:,}円</strong>
      （{tsign}{pct(total, first):.2f}%）
    </div>"""


def build_signal_rows(rows, n=8):
    """note が入っている（売買が発生した）行を新しい順に最大 n 件。"""
    active = [r for r in rows if (r.get("note") or "").strip()]
    active = active[-n:][::-1]  # 直近を上に
    if not active:
        return "<p style='color:#888'>直近で売買シグナルはありません（ポジション継続）</p>"

    body = ""
    for r in active:
        note  = r["note"].strip()
        price = to_int(r["price"])
        # BUY を緑、SELL を赤で軽く強調
        note_html = note.replace("BUY", "<span style='color:#2ecc71;font-weight:bold'>BUY</span>")
        note_html = note_html.replace("SELL", "<span style='color:#e74c3c;font-weight:bold'>SELL</span>")
        body += f"""
        <tr>
          <td style='padding:6px;border-bottom:1px solid #f0f0f0;font-size:12px;white-space:nowrap'>{r['date']}</td>
          <td style='padding:6px;border-bottom:1px solid #f0f0f0;font-size:12px'>{note_html}</td>
          <td style='padding:6px;border-bottom:1px solid #f0f0f0;font-size:12px;text-align:right;white-space:nowrap'>{price:,}円</td>
        </tr>"""

    return f"""
    <table style='border-collapse:collapse;width:100%'>
      <tr style='background:#f5f5f5'>
        <th style='padding:6px;text-align:left;font-size:12px'>日付</th>
        <th style='padding:6px;text-align:left;font-size:12px'>シグナル</th>
        <th style='padding:6px;text-align:right;font-size:12px'>価格</th>
      </tr>
      {body}
    </table>"""


def build_html(rows):
    """HTML メール本文を組み立てる。rows は日付昇順・1件以上。"""
    latest = rows[-1]
    prev   = rows[-2] if len(rows) >= 2 else latest

    date_str = latest["date"]
    days     = len(rows)

    price      = to_int(latest["price"])
    prev_price = to_int(prev["price"])
    p_change   = price - prev_price
    p_sign     = "+" if p_change >= 0 else ""
    p_color    = "#2ecc71" if p_change >= 0 else "#e74c3c"

    # カード群（3戦略 + buy&hold）
    cards = ""
    for key, label, color in STRATEGIES:
        cards += strategy_card(
            label, color,
            to_int(latest[f"{key}_equity"]),
            to_int(prev[f"{key}_equity"]),
            to_int(latest[f"{key}_pos"]),
        )
    cards += strategy_card(
        "buy&hold", "#95a5a6",
        to_int(latest["buyhold_equity"]),
        to_int(prev["buyhold_equity"]),
        None,
    )

    # 戦略 vs buy&hold の一言サマリー
    bh = to_int(latest["buyhold_equity"])
    best_key, best_label, _ = max(
        STRATEGIES, key=lambda s: to_int(latest[f"{s[0]}_equity"])
    )
    best_eq  = to_int(latest[f"{best_key}_equity"])
    diff     = best_eq - bh
    diff_txt = (f"最良は <strong>{best_label}</strong>（{best_eq:,}円）。"
                f"buy&hold との差は {'+' if diff >= 0 else ''}{diff:,}円。")

    return f"""
<!DOCTYPE html>
<html>
<body style='font-family:sans-serif;max-width:640px;margin:0 auto;color:#333'>

  <div style='background:#1a1a2e;color:white;padding:20px;border-radius:8px 8px 0 0'>
    <h2 style='margin:0;font-size:18px'>📈 BTC フォワード紙トレード 日次レポート</h2>
    <p style='margin:4px 0 0;font-size:13px;color:#aaa'>{date_str}（フォワード{days}日目）</p>
  </div>

  <div style='padding:20px;background:white;border:1px solid #eee'>

    <p style='margin:0 0 16px;font-size:14px'>
      BTC現在価格: <strong>{price:,}円</strong>
      <span style='color:{p_color};font-size:13px'>（前日比 {p_sign}{p_change:,}円 / {p_sign}{pct(p_change, prev_price):.2f}%）</span>
    </p>

    <h3 style='margin:0 0 12px;font-size:15px'>評価額（開始 {INITIAL_CAPITAL:,}円）</h3>
    <table style='width:100%;border-spacing:6px'>
      <tr>{cards}</tr>
    </table>
    <p style='margin:8px 0 0;font-size:12px;color:#666'>{diff_txt}</p>

    <h3 style='margin:24px 0 8px;font-size:15px'>直近の価格推移</h3>
    {build_price_chart(rows)}

    <h3 style='margin:24px 0 8px;font-size:15px'>直近の売買シグナル</h3>
    {build_signal_rows(rows)}

    <hr style='margin:24px 0;border:none;border-top:1px solid #eee'>
    <p style='font-size:11px;color:#aaa;margin:0'>
      このメールは GitHub Actions により自動送信されています。<br>
      ログ: https://github.com/horizontalwing/btc-sim/blob/main/logs/trend_log.csv
    </p>
  </div>

</body>
</html>"""


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = REPORT_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        smtp.send_message(msg)


# ── メイン ───────────────────────────────────────────────

def main():
    jst_now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    rows    = load_logs()

    if not rows:
        print(f"[{jst_now}] {LOG_FILE} にデータがありません。スキップします。")
        return

    latest_date = rows[-1]["date"]
    print(f"[{jst_now}] 日次レポート生成開始: 最新日={latest_date} / 全{len(rows)}日分")

    html    = build_html(rows)
    subject = f"【BTC-trend】{latest_date} 日次レポート"

    send_email(subject, html)
    print(f"メール送信完了: {REPORT_EMAIL}")


if __name__ == "__main__":
    main()
