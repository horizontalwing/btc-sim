"""
BTC自動売買シミュレーター
- CoinGecko APIからBTC価格を取得
- 3戦略（グリッド・移動平均・複合）を同時シミュレーション
- 結果をCSVログに追記
"""

import csv
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────

# グリッド設定
GRID_STEP_JPY       = 500_000   # グリッド幅（50万円）
GRID_LOWER_JPY      = 11_500_000  # レンジ下限（1,150万円）
GRID_UPPER_JPY      = 14_000_000  # レンジ上限（1,400万円）

# 移動平均設定
MA_SHORT_PERIOD     = 6         # 短期（6期間 = 約6時間）
MA_LONG_PERIOD      = 24        # 長期（24期間 = 約24時間）

# 複合戦略：レンジ判定の閾値
RANGE_THRESHOLD_PCT = 0.01      # 短期MAと長期MAの差が1%以内ならレンジ相場

# 売買量
TRADE_QTY_BTC       = 0.001     # 1回あたりの仮想売買量（BTC）

# リスク管理
STOP_PRICE_JPY      = 11_000_000  # 全戦略停止ライン（1,100万円）

# ファイルパス
LOG_FILE            = "logs/simulation_log.csv"
STATE_FILE          = "logs/state.json"

# ── ヘルパー関数 ─────────────────────────────────────────

def get_jst_now():
    """現在時刻をJSTで返す"""
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")


def fetch_btc_price():
    """CoinGecko APIからBTC価格（円）を取得"""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=jpy"
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            data = json.loads(res.read())
            return int(data["bitcoin"]["jpy"])
    except Exception as e:
        raise RuntimeError(f"価格取得失敗: {e}")


def load_state():
    """前回の状態をJSONから読み込む"""
    if not os.path.exists(STATE_FILE):
        return {
            "price_history": [],        # 価格履歴（MA計算用）
            "grid_last_price": None,    # グリッド: 前回約定価格
            "grid_pnl": 0,              # グリッド: 累計損益
            "grid_position": 0,         # グリッド: 保有BTC数（仮想）
            "ma_position": 0,           # MA: ポジション（1=買い, 0=なし）
            "ma_pnl": 0,                # MA: 累計損益
            "ma_entry_price": None,     # MA: エントリー価格
            "combined_position": 0,     # 複合: ポジション
            "combined_pnl": 0,          # 複合: 累計損益
            "combined_entry_price": None,
            "stopped": False,           # 全戦略停止フラグ
        }
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    """状態をJSONに保存"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_log(rows):
    """CSVログに行を追記"""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "timestamp", "btc_price_jpy", "strategy", "signal",
                "simulated_action", "simulated_qty_btc", "cumulative_pnl_jpy",
                "ma_short", "ma_long", "notes"
            ])
        writer.writerows(rows)


def calc_ma(prices, period):
    """単純移動平均を計算（データ不足時はNone）"""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


# ── 戦略ロジック ─────────────────────────────────────────

def strategy_grid(price, state, ts):
    """戦略①: グリッドトレード"""
    signal = "hold"
    action = "none"
    qty    = 0
    notes  = ""

    last = state["grid_last_price"]

    # 初回はベースラインを記録するだけ
    if last is None:
        state["grid_last_price"] = price
        notes = "初回実行: ベースライン記録"
        return signal, action, qty, state, notes

    diff = price - last

    if diff <= -GRID_STEP_JPY and GRID_LOWER_JPY <= price:
        # 50万円以上下落 → 仮想買い
        signal = "buy"
        action = "would_buy"
        qty    = TRADE_QTY_BTC
        cost   = price * qty
        state["grid_pnl"]      -= cost   # 買いコスト
        state["grid_position"] += qty
        state["grid_last_price"] = price
        notes = f"グリッド買い: {last:,}→{price:,}円（{diff:+,}円）"

    elif diff >= GRID_STEP_JPY and price <= GRID_UPPER_JPY:
        # 50万円以上上昇 → 仮想売り
        signal = "sell"
        action = "would_sell"
        qty    = TRADE_QTY_BTC
        revenue = price * qty
        state["grid_pnl"]      += revenue  # 売り収益
        state["grid_position"] -= qty
        state["grid_last_price"] = price
        notes = f"グリッド売り: {last:,}→{price:,}円（{diff:+,}円）"

    else:
        notes = f"待機: 前回{last:,}円 現在{price:,}円（差{diff:+,}円）"
        # 待機時も基準価格を更新（次回との差分を正しく計算するため）
        state["grid_last_price"] = price

    return signal, action, qty, state, notes


def strategy_ma(price, ma_short, ma_long, state, ts):
    """戦略②: 移動平均クロス"""
    signal = "hold"
    action = "none"
    qty    = 0
    notes  = ""

    if ma_short is None or ma_long is None:
        notes = f"MA計算中（データ蓄積待ち）"
        return signal, action, qty, state, notes

    prev_position = state["ma_position"]

    if ma_short > ma_long and prev_position == 0:
        # ゴールデンクロス → 買い
        signal = "buy"
        action = "would_buy"
        qty    = TRADE_QTY_BTC
        state["ma_position"]    = 1
        state["ma_entry_price"] = price
        state["ma_pnl"]        -= price * qty
        notes = f"ゴールデンクロス: 短期MA{ma_short:,.0f} > 長期MA{ma_long:,.0f}"

    elif ma_short < ma_long and prev_position == 1:
        # デッドクロス → 売り
        signal = "sell"
        action = "would_sell"
        qty    = TRADE_QTY_BTC
        state["ma_position"]    = 0
        state["ma_entry_price"] = None
        state["ma_pnl"]        += price * qty
        notes = f"デッドクロス: 短期MA{ma_short:,.0f} < 長期MA{ma_long:,.0f}"

    else:
        cross = "短期>長期" if ma_short > ma_long else "短期<長期"
        notes = f"待機: {cross} ポジション{prev_position}"

    return signal, action, qty, state, notes


def strategy_combined(price, ma_short, ma_long, state, ts):
    """戦略③: 複合（移動平均でレンジ判定→グリッド実行）"""
    signal = "hold"
    action = "none"
    qty    = 0
    notes  = ""

    if ma_short is None or ma_long is None:
        notes = "MA計算中（データ蓄積待ち）"
        return signal, action, qty, state, notes

    # レンジ判定
    diff_pct = abs(ma_short - ma_long) / ma_long
    is_range = diff_pct <= RANGE_THRESHOLD_PCT

    if not is_range:
        notes = f"トレンド相場: MA乖離{diff_pct*100:.2f}% → グリッド停止"
        return signal, action, qty, state, notes

    # レンジ相場 → グリッドロジックを適用（combined用state）
    last = state.get("combined_last_price")
    if last is None:
        state["combined_last_price"] = price
        notes = "レンジ相場: ベースライン記録"
        return signal, action, qty, state, notes

    diff = price - last

    if diff <= -GRID_STEP_JPY and GRID_LOWER_JPY <= price:
        signal = "buy"
        action = "would_buy"
        qty    = TRADE_QTY_BTC
        state["combined_pnl"]          -= price * qty
        state["combined_position"]     += qty
        state["combined_last_price"]    = price
        notes = f"複合買い（レンジ相場MA乖離{diff_pct*100:.2f}%）: {diff:+,}円"

    elif diff >= GRID_STEP_JPY and price <= GRID_UPPER_JPY:
        signal = "sell"
        action = "would_sell"
        qty    = TRADE_QTY_BTC
        state["combined_pnl"]          += price * qty
        state["combined_position"]     -= qty
        state["combined_last_price"]    = price
        notes = f"複合売り（レンジ相場MA乖離{diff_pct*100:.2f}%）: {diff:+,}円"

    else:
        notes = f"レンジ相場・待機: MA乖離{diff_pct*100:.2f}% 差{diff:+,}円"
        # 待機時も基準価格を更新
        state["combined_last_price"] = price

    return signal, action, qty, state, notes


# ── メイン処理 ───────────────────────────────────────────

def main():
    print(f"[{get_jst_now()}] シミュレーター起動")

    # 価格取得
    price = fetch_btc_price()
    print(f"BTC価格: {price:,}円")

    # 状態読み込み
    state = load_state()

    # 停止チェック
    if state.get("stopped"):
        print("⚠️  損切りラインに達したため全戦略停止中")
        return

    if price <= STOP_PRICE_JPY:
        state["stopped"] = True
        save_state(state)
        print(f"🛑 損切りライン（{STOP_PRICE_JPY:,}円）到達。全戦略を停止します。")
        return

    # 価格履歴に追加
    state["price_history"].append(price)
    # 最大MA_LONG_PERIOD × 2 件だけ保持（メモリ節約）
    if len(state["price_history"]) > MA_LONG_PERIOD * 2:
        state["price_history"] = state["price_history"][-(MA_LONG_PERIOD * 2):]

    # 移動平均計算
    ma_short = calc_ma(state["price_history"], MA_SHORT_PERIOD)
    ma_long  = calc_ma(state["price_history"], MA_LONG_PERIOD)

    ts = get_jst_now()
    log_rows = []

    # 戦略① グリッド
    sig1, act1, qty1, state, note1 = strategy_grid(price, state, ts)
    log_rows.append([
        ts, price, "grid", sig1, act1, qty1,
        round(state["grid_pnl"]),
        round(ma_short) if ma_short else "",
        round(ma_long)  if ma_long  else "",
        note1
    ])

    # 戦略② 移動平均
    sig2, act2, qty2, state, note2 = strategy_ma(price, ma_short, ma_long, state, ts)
    log_rows.append([
        ts, price, "ma", sig2, act2, qty2,
        round(state["ma_pnl"]),
        round(ma_short) if ma_short else "",
        round(ma_long)  if ma_long  else "",
        note2
    ])

    # 戦略③ 複合
    sig3, act3, qty3, state, note3 = strategy_combined(price, ma_short, ma_long, state, ts)
    log_rows.append([
        ts, price, "combined", sig3, act3, qty3,
        round(state["combined_pnl"]),
        round(ma_short) if ma_short else "",
        round(ma_long)  if ma_long  else "",
        note3
    ])

    # ログ書き込み
    append_log(log_rows)
    save_state(state)

    # サマリー表示
    print(f"戦略① グリッド  : {sig1:4s} | 累計損益 {state['grid_pnl']:+,.0f}円 | {note1}")
    print(f"戦略② 移動平均  : {sig2:4s} | 累計損益 {state['ma_pnl']:+,.0f}円 | {note2}")
    print(f"戦略③ 複合      : {sig3:4s} | 累計損益 {state['combined_pnl']:+,.0f}円 | {note3}")
    print(f"ログ保存完了: {LOG_FILE}")


if __name__ == "__main__":
    main()
