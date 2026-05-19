"""
BTC自動売買シミュレーション 日次レポート送信
- simulation_log.csvから昨日のデータを集計
- Gmailで自分宛にHTMLメールを送信
"""

import csv
import json
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── 設定 ────────────────────────────────────────────────

LOG_FILE    = "logs/simulation_log.csv"
STATE_FILE  = "logs/state.json"

GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]

JST = timezone(timedelta(hours=9))

# ── ヘルパー ─────────────────────────────────────────────

def get_yesterday_jst():
    yesterday = datetime.now(JST) - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def load_logs():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def summarize(rows, date_str):
    """指定日のログを戦略別に集計"""
    daily = [r for r in rows if r["timestamp"].startswith(date_str)]

    strategies = ["grid", "ma", "combined"]
    summary = {}

    for s in strategies:
        s_rows = [r for r in daily if r["strategy"] == s]
        buys   = [r for r in s_rows if r["signal"] == "buy"]
        sells  = [r for r in s_rows if r["signal"] == "sell"]

        # 最新の累計損益
        latest_pnl = int(s_rows[-1]["cumulative_pnl_jpy"]) if s_rows else 0

        summary[s] = {
            "checks" : len(s_rows),
            "buys"   : len(buys),
            "sells"  : len(sells),
            "pnl"    : latest_pnl,
            "signals": s_rows,
        }

    return summary, daily


def format_signal_rows(signals):
    """シグナルが出た行だけHTMLテーブルで返す"""
    active = [r for r in signals if r["signal"] != "hold"]
    if not active:
        return "<p style='color:#888'>シグナルなし（待機）</p>"

    rows_html = ""
    for r in active:
        color = "#2ecc71" if r["signal"] == "buy" else "#e74c3c"
        rows_html += f"""
        <tr>
          <td>{r['timestamp'][11:16]}</td>
          <td style='color:{color};font-weight:bold'>{r['signal'].upper()}</td>
          <td>{int(r['btc_price_jpy']):,}円</td>
          <td>{r['notes']}</td>
        </tr>"""

    return f"""
    <table style='border-collapse:collapse;width:100%;font-size:13px'>
      <tr style='background:#f5f5f5'>
        <th style='padding:6px;text-align:left'>時刻</th>
        <th style='padding:6px;text-align:left'>シグナル</th>
        <th style='padding:6px;text-align:left'>価格</th>
        <th style='padding:6px;text-align:left'>備考</th>
      </tr>
      {rows_html}
    </table>"""


def build_price_chart(all_rows):
    """直近4回分（約24時間）の価格推移をHTMLバーチャートで返す"""
    # strategyがgridの行だけ使う（3戦略で重複するため）
    grid_rows = [r for r in all_rows if r["strategy"] == "grid"]
    recent = grid_rows[-4:] if len(grid_rows) >= 4 else grid_rows

    if not recent:
        return "<p style='color:#888'>データ不足</p>"

    prices = [int(r["btc_price_jpy"]) for r in recent]
    times  = [r["timestamp"][5:16] for r in recent]  # MM-DD HH:MM

    min_p  = min(prices)
    max_p  = max(prices)
    rng    = max_p - min_p if max_p != min_p else 1

    first_price = prices[0]

    rows_html = ""
    for i, (t, p) in enumerate(zip(times, prices)):
        bar_pct  = int((p - min_p) / rng * 80) + 10  # 10〜90%
        change   = p - first_price
        sign     = "+" if change >= 0 else ""
        chg_color = "#2ecc71" if change >= 0 else "#e74c3c"

        # 最高値・最安値ラベル
        tag = ""
        if p == max_p:
            tag = "<span style='font-size:10px;color:#e74c3c;margin-left:4px'>▲ 高値</span>"
        elif p == min_p:
            tag = "<span style='font-size:10px;color:#3498db;margin-left:4px'>▼ 安値</span>"

        rows_html += f"""
        <tr>
          <td style='padding:4px 8px 4px 0;font-size:12px;color:#888;white-space:nowrap'>{t}</td>
          <td style='padding:4px;width:100%'>
            <div style='background:#f0f0f0;border-radius:4px;height:18px;position:relative'>
              <div style='background:linear-gradient(90deg,#3498db,#2980b9);
                          width:{bar_pct}%;height:100%;border-radius:4px'></div>
            </div>
          </td>
          <td style='padding:4px 0 4px 8px;font-size:12px;white-space:nowrap;text-align:right'>
            {p:,}円{tag}
          </td>
          <td style='padding:4px 0 4px 8px;font-size:11px;color:{chg_color};white-space:nowrap'>
            {sign}{change:,}
          </td>
        </tr>"""

    # 変動幅サマリー
    total_change = prices[-1] - prices[0]
    total_sign   = "+" if total_change >= 0 else ""
    total_color  = "#2ecc71" if total_change >= 0 else "#e74c3c"

    return f"""
    <table style='width:100%;border-collapse:collapse'>
      {rows_html}
    </table>
    <div style='margin-top:8px;font-size:12px;color:{total_color}'>
      24時間変動: <strong>{total_sign}{total_change:,}円</strong>
      （{total_sign}{total_change/prices[0]*100:.2f}%）
    </div>"""


def build_html(date_str, summary, state, all_rows):
    """HTMLメール本文を組み立てる"""

    # 最新価格
    latest_price = int(all_rows[-1]["btc_price_jpy"]) if all_rows else 0

    # 累計損益カード
    def pnl_card(name, label, color):
        pnl = summary[name]["pnl"]
        sign = "+" if pnl >= 0 else ""
        pnl_color = "#2ecc71" if pnl >= 0 else "#e74c3c"
        return f"""
        <td style='padding:12px;text-align:center;background:#fafafa;
                   border:1px solid #eee;border-radius:8px;width:33%'>
          <div style='font-size:12px;color:#888;margin-bottom:4px'>{label}</div>
          <div style='font-size:20px;font-weight:bold;color:{pnl_color}'>
            {sign}{pnl:,}円
          </div>
          <div style='font-size:11px;color:#aaa;margin-top:4px'>
            買:{summary[name]['buys']} 売:{summary[name]['sells']} 確認:{summary[name]['checks']}回
          </div>
        </td>"""

    strategy_sections = ""
    labels = {"grid": "① グリッド", "ma": "② 移動平均", "combined": "③ 複合"}
    for s, label in labels.items():
        strategy_sections += f"""
        <h3 style='margin:24px 0 8px;font-size:15px;color:#333'>{label}</h3>
        {format_signal_rows(summary[s]['signals'])}"""

    # 稼働日数
    dates = sorted(set(r["timestamp"][:10] for r in all_rows))
    days  = len(dates)

    return f"""
<!DOCTYPE html>
<html>
<body style='font-family:sans-serif;max-width:640px;margin:0 auto;color:#333'>

  <div style='background:#1a1a2e;color:white;padding:20px;border-radius:8px 8px 0 0'>
    <h2 style='margin:0;font-size:18px'>📊 BTC シミュレーション 日次レポート</h2>
    <p style='margin:4px 0 0;font-size:13px;color:#aaa'>{date_str}（稼働{days}日目）</p>
  </div>

  <div style='padding:20px;background:white;border:1px solid #eee'>

    <p style='margin:0 0 16px;font-size:14px'>
      BTC現在価格: <strong>{latest_price:,}円</strong>
    </p>

    <h3 style='margin:0 0 12px;font-size:15px'>累計損益（シミュレーション）</h3>
    <table style='width:100%;border-spacing:8px'>
      <tr>
        {pnl_card('grid',     '① グリッド', '#3498db')}
        {pnl_card('ma',       '② 移動平均', '#9b59b6')}
        {pnl_card('combined', '③ 複合',     '#e67e22')}
      </tr>
    </table>

    <h3 style='margin:24px 0 8px;font-size:15px'>直近24時間の価格推移</h3>
    {build_price_chart(all_rows)}

    <h3 style='margin:24px 0 8px;font-size:15px'>本日のシグナル詳細</h3>
    {strategy_sections}

    <hr style='margin:24px 0;border:none;border-top:1px solid #eee'>
    <p style='font-size:11px;color:#aaa;margin:0'>
      このメールはGitHub Actionsにより自動送信されています。<br>
      ログ: https://github.com/horizontalwing/btc-sim/blob/main/logs/simulation_log.csv
    </p>
  </div>

</body>
</html>"""


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        smtp.send_message(msg)


# ── メイン ───────────────────────────────────────────────

def main():
    jst_now    = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    date_str   = get_yesterday_jst()

    print(f"[{jst_now}] 日次レポート生成開始: 対象日={date_str}")

    all_rows          = load_logs()
    state             = load_state()
    summary, daily    = summarize(all_rows, date_str)

    if not daily:
        print(f"{date_str} のログがありません。スキップします。")
        return

    html    = build_html(date_str, summary, state, all_rows)
    subject = f"【BTC-sim】{date_str} 日次レポート"

    send_email(subject, html)
    print(f"メール送信完了: {GMAIL_ADDRESS}")


if __name__ == "__main__":
    main()
