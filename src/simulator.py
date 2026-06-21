"""
BTC自動売買シミュレーター
- GMOコイン Public APIからBTC価格を取得
- 3戦略（グリッド・移動平均・複合）を同時シミュレーション
- 結果をCSVログに追記
"""

import csv
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

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

# 売買量（Satoshi単位で管理：100 Satoshi = 0.001 BTC）
TRADE_QTY_SATOSHI   = 100_000   # 0.001 BTC = 100,000 Satoshi
SATOSHI_PER_BTC     = 100_000_000

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
    """GMOコイン Public APIからBTC価格（円）を取得"""
    url = "https://api.coin.z.com/public/v1/ticker?symbol=BTC"
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            data = json.loads(res.read())
            # lastが最終取引価格
            return int(float(data["data"][0]["last"]))
    except Exception as e:
        raise RuntimeError(f"価格取得失敗: {e}")


def satoshi_to_btc(satoshi):
    """Satoshi → BTC（仮想表示用）"""
    return satoshi / SATOSHI_PER_BTC


def calc_jpy_cost(price_jpy, qty_satoshi):
    """
    購入コスト（円）を計算
    価格（円） × 数量（Satoshi） ÷ 1BTC当たりSatoshi数 = 円
    整数演算で誤差を最小化
    """
    return (price_jpy * qty_satoshi) // SATOSHI_PER_BTC


def load_state():
    """前回の状態をJSONから読み込む（型変換により浮動小数点誤差を防止）"""
    if not os.path.exists(STATE_FILE):
        return {
            "price_history": [],        # 価格履歴（MA計算用）
            "grid_last_price": None,    # グリッド: 前回約定価格
            "grid_pnl": 0,              # グリッド: 累計損益（円）
            "grid_position": 0,         # グリッド: 保有Satoshi数（仮想）
            "ma_position": 0,           # MA: ポジション（Satoshi単位、正=買い）
            "ma_pnl": 0,                # MA: 累計損益（円）
            "ma_entry_price": None,     # MA: エントリー価格
            "combined_last_price": None,  # 複合: グリッド基準価格
            "combined_position": 0,     # 複合: ポジション（Satoshi単位）
            "combined_pnl": 0,          # 複合: 累計損益（円）
            "combined_entry_price": None,
            "stopped": False,           # 全戦略停止フラグ
        }
    with open(STATE_FILE) as f:
        loaded = json.load(f)
    
    # 【型変換】JSON読み込み時の浮動小数点誤差を修正
    # 整数フィールドは明示的に int() で変換（損益と位置）
    loaded["grid_pnl"] = int(round(loaded["grid_pnl"]))
    loaded["grid_position"] = int(round(loaded["grid_position"]))
    loaded["ma_pnl"] = int(round(loaded["ma_pnl"]))
    loaded["ma_position"] = int(round(loaded["ma_position"]))
    loaded["combined_pnl"] = int(round(loaded["combined_pnl"]))
    loaded["combined_position"] = int(round(loaded["combined_position"]))
    
    # Optional フィールド（Noneまたは整数）
    if loaded["grid_last_price"] is not None:
        loaded["grid_last_price"] = int(round(loaded["grid_last_price"]))
    if loaded["ma_entry_price"] is not None:
        loaded["ma_entry_price"] = int(round(loaded["ma_entry_price"]))
    if loaded["combined_last_price"] is not None:
        loaded["combined_last_price"] = int(round(loaded["combined_last_price"]))
    if loaded["combined_entry_price"] is not None:
        loaded["combined_entry_price"] = int(round(loaded["combined_entry_price"]))
    
    return loaded


def save_state(state):
    """状態をJSONに保存（保存前に再度整数化して誤差を排除）"""
    # 【型保証・保存時】保存直前に再度整数化
    state["grid_pnl"] = int(round(state["grid_pnl"]))
    state["grid_position"] = int(round(state["grid_position"]))
    state["ma_pnl"] = int(round(state["ma_pnl"]))
    state["ma_position"] = int(round(state["ma_position"]))
    state["combined_pnl"] = int(round(state["combined_pnl"]))
    state["combined_position"] = int(round(state["combined_position"]))
    
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


def format_ma_for_log(ma_value):
    """MA値をログ用にフォーマット（None時は空文字列）"""
    if ma_value is None:
        return ""
    return int(round(ma_value))


# ── 戦略ロジック ─────────────────────────────────────────

def strategy_grid(price, state, ts):
    """戦略①: グリッドトレード"""
    signal = "hold"
    action = "none"
    qty_btc = 0.0  # ログ出力用（BTC単位）
    notes  = ""

    last = state["grid_last_price"]

    # 初回はベースラインを記録するだけ
    if last is None:
        state["grid_last_price"] = price
        notes = "初回実行: ベースライン記録"
        return signal, action, qty_btc, state, notes

    diff = price - last

    # グリッド幅以上の下落で買い（レンジ下限以上）
    if diff < -GRID_STEP_JPY and GRID_LOWER_JPY <= price:
        signal = "buy"
        action = "would_buy"
        qty_satoshi = TRADE_QTY_SATOSHI  # Satoshi単位で管理
        qty_btc = satoshi_to_btc(qty_satoshi)  # ログ表示用
        
        cost = calc_jpy_cost(price, qty_satoshi)  # 整数演算で円コストを計算
        state["grid_pnl"] = int(state["grid_pnl"]) - cost
        state["grid_position"] = int(state["grid_position"]) + qty_satoshi
        state["grid_last_price"] = price  # 売買時のみ基準価格を更新 [CONSTRAINT-001準拠]
        notes = f"グリッド買い: {last:,}→{price:,}円（{diff:+,}円）"

    # グリッド幅以上の上昇で売り（レンジ上限以下）
    elif diff > GRID_STEP_JPY and price <= GRID_UPPER_JPY:
        signal = "sell"
        action = "would_sell"
        qty_satoshi = TRADE_QTY_SATOSHI  # Satoshi単位で管理
        qty_btc = satoshi_to_btc(qty_satoshi)  # ログ表示用
        
        revenue = calc_jpy_cost(price, qty_satoshi)  # 整数演算で円収益を計算
        state["grid_pnl"] = int(state["grid_pnl"]) + revenue
        state["grid_position"] = int(state["grid_position"]) - qty_satoshi
        state["grid_last_price"] = price  # 売買時のみ基準価格を更新 [CONSTRAINT-001準拠]
        notes = f"グリッド売り: {last:,}→{price:,}円（{diff:+,}円）"

    else:
        notes = f"待機: 前回{last:,}円 現在{price:,}円（差{diff:+,}円）"
        # ★ CONSTRAINT-001準拠: 待機時は grid_last_price を更新しない
        # 小刻みな価格変動でグリッドがリセットされることを防止

    # 【型保証】計算直後に整数化
    state["grid_pnl"] = int(state["grid_pnl"])
    state["grid_position"] = int(state["grid_position"])

    return signal, action, qty_btc, state, notes


def strategy_ma(price, ma_short, ma_long, state, ts):
    """
    戦略②: 移動平均クロス
    
    ゴールデンクロス（短期MA > 長期MA）で買い
    デッドクロス（短期MA < 長期MA）で売り
    """
    signal = "hold"
    action = "none"
    qty_btc = 0.0  # ログ出力用（BTC単位）
    notes  = ""

    if ma_short is None or ma_long is None:
        notes = "MA計算中（データ蓄積待ち）"
        return signal, action, qty_btc, state, notes

    prev_position_satoshi = state["ma_position"]

    if ma_short > ma_long and prev_position_satoshi == 0:
        # ゴールデンクロス → 買い
        signal = "buy"
        action = "would_buy"
        qty_satoshi = TRADE_QTY_SATOSHI  # Satoshi単位で管理
        qty_btc = satoshi_to_btc(qty_satoshi)  # ログ表示用
        
        state["ma_position"] = qty_satoshi  # Satoshi単位で記録
        state["ma_entry_price"] = price
        
        cost = calc_jpy_cost(price, qty_satoshi)  # 整数演算で円コストを計算
        state["ma_pnl"] = int(state["ma_pnl"]) - cost
        notes = f"ゴールデンクロス: 短期MA{ma_short:,.0f} > 長期MA{ma_long:,.0f}"

    elif ma_short < ma_long and prev_position_satoshi > 0:
        # デッドクロス → 売り
        signal = "sell"
        action = "would_sell"
        qty_satoshi = prev_position_satoshi  # 保有量を売却
        qty_btc = satoshi_to_btc(qty_satoshi)  # ログ表示用
        
        state["ma_position"] = 0
        state["ma_entry_price"] = None
        
        revenue = calc_jpy_cost(price, qty_satoshi)  # 整数演算で円収益を計算
        state["ma_pnl"] = int(state["ma_pnl"]) + revenue
        notes = f"デッドクロス: 短期MA{ma_short:,.0f} < 長期MA{ma_long:,.0f}"

    else:
        cross = "短期>長期" if ma_short > ma_long else "短期<長期"
        pos_btc = satoshi_to_btc(prev_position_satoshi) if prev_position_satoshi > 0 else 0
        notes = f"待機: {cross} ポジション{pos_btc:.3f}"

    # 【型保証】計算直後に整数化
    state["ma_pnl"] = int(state["ma_pnl"])
    state["ma_position"] = int(state["ma_position"])

    return signal, action, qty_btc, state, notes


def strategy_combined(price, ma_short, ma_long, state, ts):
    """
    戦略③: 複合（移動平均でレンジ判定→グリッド実行）
    
    短期MAと長期MAの乖離が1%以内ならレンジ相場と判定
    レンジ相場で、かつグリッド条件を満たせば売買実行
    """
    signal = "hold"
    action = "none"
    qty_btc = 0.0  # ログ出力用（BTC単位）
    notes  = ""

    if ma_short is None or ma_long is None:
        notes = "MA計算中（データ蓄積待ち）"
        return signal, action, qty_btc, state, notes

    # レンジ判定
    diff_pct = abs(ma_short - ma_long) / ma_long
    is_range = diff_pct <= RANGE_THRESHOLD_PCT

    if not is_range:
        notes = f"トレンド相場: MA乖離{diff_pct*100:.2f}% → グリッド停止"
        return signal, action, qty_btc, state, notes

    # レンジ相場 → グリッドロジックを適用（combined用state）
    last = state.get("combined_last_price")
    if last is None:
        state["combined_last_price"] = price
        notes = "レンジ相場: ベースライン記録"
        return signal, action, qty_btc, state, notes

    diff = price - last

    # グリッド幅以上の下落で買い（レンジ下限以上）
    if diff < -GRID_STEP_JPY and GRID_LOWER_JPY <= price:
        signal = "buy"
        action = "would_buy"
        qty_satoshi = TRADE_QTY_SATOSHI  # Satoshi単位で管理
        qty_btc = satoshi_to_btc(qty_satoshi)  # ログ表示用
        
        cost = calc_jpy_cost(price, qty_satoshi)  # 整数演算で円コストを計算
        state["combined_pnl"] = int(state["combined_pnl"]) - cost
        state["combined_position"] = int(state["combined_position"]) + qty_satoshi
        state["combined_last_price"] = price  # 売買時のみ基準価格を更新 [CONSTRAINT-002準拠]
        notes = f"複合買い（レンジMA乖離{diff_pct*100:.2f}%）: {diff:+,}円"

    # グリッド幅以上の上昇で売り（レンジ上限以下）
    elif diff > GRID_STEP_JPY and price <= GRID_UPPER_JPY:
        signal = "sell"
        action = "would_sell"
        qty_satoshi = TRADE_QTY_SATOSHI  # Satoshi単位で管理
        qty_btc = satoshi_to_btc(qty_satoshi)  # ログ表示用
        
        revenue = calc_jpy_cost(price, qty_satoshi)  # 整数演算で円収益を計算
        state["combined_pnl"] = int(state["combined_pnl"]) + revenue
        state["combined_position"] = int(state["combined_position"]) - qty_satoshi
        state["combined_last_price"] = price  # 売買時のみ基準価格を更新 [CONSTRAINT-002準拠]
        notes = f"複合売り（レンジMA乖離{diff_pct*100:.2f}%）: {diff:+,}円"

    else:
        notes = f"レンジ相場・待機: MA乖離{diff_pct*100:.2f}% 差{diff:+,}円"
        # ★ CONSTRAINT-002準拠: 待機時は combined_last_price を更新しない
        # グリッド部分も CONSTRAINT-001 と同じ原則に従う

    # 【型保証】計算直後に整数化
    state["combined_pnl"] = int(state["combined_pnl"])
    state["combined_position"] = int(state["combined_position"])

    return signal, action, qty_btc, state, notes


# ── メイン処理 ───────────────────────────────────────────

def main():
    print(f"[{get_jst_now()}] シミュレーター起動")

    # 価格取得
    price = fetch_btc_price()
    print(f"BTC価格: {price:,}円")

    # 状態読み込み
    state = load_state()

    # 停止チェック（売買シグナルのみ停止・価格記録は継続）
    is_stopped = state.get("stopped", False)

    if price <= STOP_PRICE_JPY and not is_stopped:
        state["stopped"] = True
        save_state(state)
        print(f"🛑 損切りライン（{STOP_PRICE_JPY:,}円）到達。売買シグナルを停止します。")
        is_stopped = True

    if is_stopped:
        print("⚠️  損切りライン到達中: 売買停止・価格記録のみ継続")

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
    if is_stopped:
        sig1, act1, qty1, note1 = "hold", "none", 0, "損切りライン到達中: 売買停止"
    log_rows.append([
        ts, price, "grid", sig1, act1, qty1,
        state["grid_pnl"],
        format_ma_for_log(ma_short),
        format_ma_for_log(ma_long),
        note1
    ])

    # 戦略② 移動平均
    sig2, act2, qty2, state, note2 = strategy_ma(price, ma_short, ma_long, state, ts)
    if is_stopped:
        sig2, act2, qty2, note2 = "hold", "none", 0, "損切りライン到達中: 売買停止"
    log_rows.append([
        ts, price, "ma", sig2, act2, qty2,
        state["ma_pnl"],
        format_ma_for_log(ma_short),
        format_ma_for_log(ma_long),
        note2
    ])

    # 戦略③ 複合
    sig3, act3, qty3, state, note3 = strategy_combined(price, ma_short, ma_long, state, ts)
    if is_stopped:
        sig3, act3, qty3, note3 = "hold", "none", 0, "損切りライン到達中: 売買停止"
    log_rows.append([
        ts, price, "combined", sig3, act3, qty3,
        state["combined_pnl"],
        format_ma_for_log(ma_short),
        format_ma_for_log(ma_long),
        note3
    ])

    # ログ書き込み・状態保存
    append_log(log_rows)
    save_state(state)

    # サマリー表示
    print(f"戦略① グリッド  : {sig1:4s} | 累計損益 {state['grid_pnl']:+,.0f}円 | {note1}")
    print(f"戦略② 移動平均  : {sig2:4s} | 累計損益 {state['ma_pnl']:+,.0f}円 | {note2}")
    print(f"戦略③ 複合      : {sig3:4s} | 累計損益 {state['combined_pnl']:+,.0f}円 | {note3}")
    print(f"ログ保存完了: {LOG_FILE}")


if __name__ == "__main__":
    main()
