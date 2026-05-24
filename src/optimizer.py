"""
BTC戦略オプティマイザー
- simulation_log.csv を分析
- Claude API（Haiku 4.5）でロジック改善案を生成
- simulator.py を自動更新
- optimizer_log.md に変更履歴を追記
"""

import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
MODEL               = "claude-haiku-4-5-20251001"   # コスト最小・十分な能力
MAX_TOKENS          = 4096

LOG_FILE            = "logs/simulation_log.csv"
SIMULATOR_FILE      = "src/simulator.py"
OPTIMIZER_LOG_FILE  = "logs/optimizer_log.md"

JST = timezone(timedelta(hours=9))


# ── ヘルパー ─────────────────────────────────────────────

def get_jst_now() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def append_optimizer_log(entry: str) -> None:
    os.makedirs(os.path.dirname(OPTIMIZER_LOG_FILE), exist_ok=True)
    with open(OPTIMIZER_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def call_claude(system_prompt: str, user_message: str) -> str:
    """Claude APIを呼び出してテキストを返す"""
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            data = json.loads(res.read())
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API エラー {e.code}: {body}")


# ── 分析・最適化ロジック ──────────────────────────────────

SYSTEM_PROMPT = """あなたはBTC自動売買シミュレーターの改善専門家です。

# 役割
simulation_log.csv と現在の simulator.py を受け取り、
戦略ロジックを分析して改善済みの simulator.py を出力します。

# 改善の方針
- バグ修正を最優先にする（誤動作しているロジックを直す）
- パラメータ調整は根拠のある場合のみ行う
- コードの構造・コメント・スタイルは維持する
- 3戦略の基本的な枠組みは変えない
- リスク管理ルール（損切りライン等）は変えない

# 出力形式（厳守）
以下の2つのセクションを必ず出力すること。

## ANALYSIS
変更内容と根拠を箇条書きで（日本語・簡潔に）

## CODE
改善済み simulator.py の完全なコード
コードは ```python と ``` で囲む

## ANALYSIS と ## CODE 以外のテキストは出力しないこと。"""


def parse_response(response: str) -> tuple[str, str]:
    """ClaudeのレスポンスからANALYSISとCODEを抽出"""
    # ANALYSIS セクション
    analysis_match = re.search(
        r"## ANALYSIS\s*(.*?)(?=## CODE|\Z)", response, re.DOTALL
    )
    analysis = analysis_match.group(1).strip() if analysis_match else "（分析なし）"

    # CODE セクション
    code_match = re.search(
        r"```python\s*(.*?)```", response, re.DOTALL
    )
    if not code_match:
        raise ValueError("Claudeのレスポンスにコードブロックが見つかりませんでした")

    code = code_match.group(1).strip()
    return analysis, code


def summarize_log(csv_content: str) -> str:
    """ログが長い場合に末尾200行に絞る（トークン節約）"""
    lines = csv_content.strip().splitlines()
    header = lines[0]
    data_lines = lines[1:]

    if len(data_lines) <= 200:
        return csv_content

    # 先頭5行（初期状態）＋末尾195行（直近）を渡す
    selected = [header] + data_lines[:5] + ["...（中略）..."] + data_lines[-195:]
    return "\n".join(selected)


# ── メイン処理 ───────────────────────────────────────────

def main():
    ts = get_jst_now()
    print(f"[{ts}] オプティマイザー起動")

    # ファイル読み込み
    if not os.path.exists(LOG_FILE):
        print(f"ログファイルが存在しません: {LOG_FILE}")
        return
    if not os.path.exists(SIMULATOR_FILE):
        print(f"シミュレーターが存在しません: {SIMULATOR_FILE}")
        return

    log_csv      = summarize_log(read_file(LOG_FILE))
    simulator_py = read_file(SIMULATOR_FILE)

    print(f"ログ行数: {len(log_csv.splitlines())}行")
    print(f"シミュレーター: {len(simulator_py.splitlines())}行")

    # Claude APIへ投げるメッセージを組み立て
    user_message = f"""## simulation_log.csv（直近データ）
```
{log_csv}
```

## 現在の simulator.py
```python
{simulator_py}
```

上記を分析して改善済みの simulator.py を出力してください。"""

    print("Claude API 呼び出し中...")
    response = call_claude(SYSTEM_PROMPT, user_message)

    # レスポンスをパース
    analysis, new_code = parse_response(response)

    # 変更がない場合はスキップ
    if new_code.strip() == simulator_py.strip():
        print("変更なし: ロジックは最適な状態です")
        log_entry = f"\n---\n## {ts}\n\n変更なし: ロジックは最適な状態と判断されました。\n"
        append_optimizer_log(log_entry)
        return

    # simulator.py を上書き
    write_file(SIMULATOR_FILE, new_code + "\n")
    print(f"simulator.py を更新しました")

    # optimizer_log.md に追記
    log_entry = f"""
---
## {ts}

### 分析・変更内容
{analysis}

### 変更前コード行数
{len(simulator_py.splitlines())} 行

### 変更後コード行数
{len(new_code.splitlines())} 行
"""
    append_optimizer_log(log_entry)
    print(f"optimizer_log.md に追記しました")
    print("\n=== 変更内容 ===")
    print(analysis)


if __name__ == "__main__":
    main()
