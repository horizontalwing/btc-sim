"""
日足トレンド追随 フォワード紙トレード（本番判断用データ収集）
=================================================================
目的: 実弾を入れる前に、凍結版の日足トレンド追随がこれから先どう振る舞うかを
      "紙トレード"で前向きに記録する（アウトオブサンプル検証）。

設計方針（旧simulator.pyの反省を反映）:
  - 日次で1回だけ実行（6時間おきは不要）。
  - 実際の注文は出さない（紙トレード。安全）。
  - 会計は正しく: 現金⇄BTCを全額スイッチする複利。手数料Taker込み。
  - オプティマイザー等に一切依存しない自己完結スクリプト。
  - SMA30/40/50 の3本 と buy&hold を同じログに並記（半年後の比較用）。
    ※検証で頑健だった帯は「中期SMA 30〜50日」。単一値に賭けない。

初回起動時:
  GMO日足からSMA計算用の過去終値だけを自動補完（ウォームアップ）。
  ただし紙トレードのスタート地点は"デプロイした今日"（過去成績は混ぜない＝純粋な前向き検証）。

出力:
  logs/trend_log.csv   … 日次の価格・各SMA・各ポートフォリオ評価額
  logs/trend_state.json … 継続状態
"""

import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────
SMA_DAYS = [30, 40, 50]          # 記録する中期SMAトレンド追随（頑健帯 30〜50日）
START_CAPITAL = 10_000           # 紙トレードの開始資金（円・仮想）
TAKER_FEE = 0.0005               # 成行 0.05%
SYMBOL = "BTC"
JST = timezone(timedelta(hours=9))

LOG_FILE = "logs/trend_log.csv"
STATE_FILE = "logs/trend_state.json"
GMO_TICKER = "https://api.coin.z.com/public/v1/ticker?symbol=BTC"
GMO_KLINES = "https://api.coin.z.com/public/v1/klines?symbol=BTC&interval=1day&date={year}"

LOG_FIELDS = ["date", "price", "sma30", "sma40", "sma50",
              "t30_pos", "t30_equity", "t40_pos", "t40_equity",
              "t50_pos", "t50_equity", "buyhold_equity", "note"]


# ── GMO ─────────────────────────────────────────────────
def http_json(url, retries=3):
    for a in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if a == retries - 1:
                raise RuntimeError(f"取得失敗: {url} :: {e}")
            time.sleep(1.5 * (a + 1))


def fetch_price():
    d = http_json(GMO_TICKER)
    return int(float(d["data"][0]["last"]))


def backfill_daily_closes(need=60):
    """SMAウォームアップ用に直近の日足終値を取得（今年＋必要なら前年）。"""
    now = datetime.now(JST)
    closes = {}
    for year in (now.year - 1, now.year):
        body = http_json(GMO_KLINES.format(year=year))
        if body.get("status") == 0:
            for c in body.get("data", []) or []:
                closes[int(c["openTime"])] = int(float(c["close"]))
    ordered = [closes[k] for k in sorted(closes)]
    return ordered[-need:] if len(ordered) > need else ordered


# ── SMA ─────────────────────────────────────────────────
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


# ── 状態 ─────────────────────────────────────────────────
def new_state(price, history):
    pf = {}
    for n in SMA_DAYS:
        pf[f"t{n}"] = {"cash": START_CAPITAL, "btc": 0.0, "pos": 0, "entry": None}
    pf["buyhold"] = {"cash": 0.0, "btc": START_CAPITAL * (1 - TAKER_FEE) / price}
    return {
        "price_history": history,      # SMA計算用の日次終値（ウォームアップ含む）
        "last_date": None,
        "start_date": None,
        "start_price": price,
        "portfolios": pf,
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_log(row):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(LOG_FIELDS)
        w.writerow(row)


# ── トレンド追随の1日更新（全額 現金⇄BTC）──────────────────
def step_trend(pf, price, ma):
    """price>SMAでロング保持、price<SMAで現金退避。maがNoneなら待機。"""
    if ma is None:
        return "warmup"
    if price > ma and pf["pos"] == 0:
        pf["btc"] = pf["cash"] * (1 - TAKER_FEE) / price
        pf["cash"] = 0.0
        pf["pos"] = 1
        pf["entry"] = price
        return "BUY"
    if price < ma and pf["pos"] == 1:
        pf["cash"] = pf["btc"] * price * (1 - TAKER_FEE)
        pf["btc"] = 0.0
        pf["pos"] = 0
        pf["entry"] = None
        return "SELL"
    return "hold"


def equity(pf, price):
    return pf["cash"] + pf["btc"] * price


def main():
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    price = fetch_price()

    state = load_state()
    if state is None:
        # 初回: SMAウォームアップ用に過去終値を補完（紙トレは今日から開始）
        history = backfill_daily_closes(need=max(SMA_DAYS) + 10)
        state = new_state(price, history)
        state["start_date"] = today
        print(f"初回起動: 過去終値{len(history)}本で補完・紙トレ開始 @ {price:,}円")

    # 同一日の二重実行を防止（1日1サンプル）
    if state.get("last_date") == today:
        print(f"{today} は既に記録済み。スキップ。")
        return

    # 当日終値を履歴に追加
    state["price_history"].append(int(price))
    if len(state["price_history"]) > max(SMA_DAYS) * 3:
        state["price_history"] = state["price_history"][-(max(SMA_DAYS) * 3):]

    ma_vals = {n: sma(state["price_history"], n) for n in SMA_DAYS}

    notes = []
    for n in SMA_DAYS:
        sig = step_trend(state["portfolios"][f"t{n}"], price, ma_vals[n])
        if sig in ("BUY", "SELL"):
            notes.append(f"SMA{n}:{sig}")

    eqs = {n: equity(state["portfolios"][f"t{n}"], price) for n in SMA_DAYS}
    bh_eq = equity(state["portfolios"]["buyhold"], price)

    row = [
        today, price,
        int(ma_vals[30]) if ma_vals[30] else "",
        int(ma_vals[40]) if ma_vals[40] else "",
        int(ma_vals[50]) if ma_vals[50] else "",
        state["portfolios"]["t30"]["pos"], round(eqs[30]),
        state["portfolios"]["t40"]["pos"], round(eqs[40]),
        state["portfolios"]["t50"]["pos"], round(eqs[50]),
        round(bh_eq),
        " / ".join(notes) if notes else "",
    ]
    append_log(row)
    state["last_date"] = today
    save_state(state)

    print(f"[{today}] 価格 {price:,}円")
    for n in SMA_DAYS:
        p = state["portfolios"][f"t{n}"]
        print(f"  SMA{n}追随: {'ロング' if p['pos'] else '現金 '} 評価額 {round(eqs[n]):,}円")
    print(f"  buy&hold : 評価額 {round(bh_eq):,}円")
    if notes:
        print("  シグナル:", " / ".join(notes))


if __name__ == "__main__":
    main()
