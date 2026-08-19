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

ANTHROPIC_API_KEY       = os.environ["ANTHROPIC_API_KEY"]
MODEL                   = "claude-haiku-4-5-20251001"   # コスト最小・十分な能力
MAX_TOKENS              = 16384                          # 500行規模のコードを途中で切らずに出力する余裕を確保

LOG_FILE                = "logs/simulation_log.csv"
SIMULATOR_FILE          = "src/simulator.py"
OPTIMIZER_LOG_FILE      = "logs/optimizer_log.md"
DESIGN_CONSTRAINTS_FILE = "docs/design_constraints.md"

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


def load_design_constraints() -> str:
    """設計制約ファイルを読み込む（存在しない場合は空文字列を返す）"""
    if not os.path.exists(DESIGN_CONSTRAINTS_FILE):
        return ""
    return read_file(DESIGN_CONSTRAINTS_FILE)


def build_system_prompt(constraints: str) -> str:
    """設計制約を埋め込んだシステムプロンプトを生成する"""
    base = """あなたはBTC自動売買シミュレーターの改善専門家です。

simulation_log.csv と simulator.py を受け取り、ログを分析してバグや改善点を見つけ、
修正済みの simulator.py を出力してください。

改善の方針:
- バグ修正を最優先（誤動作しているロジックを直す）
- パラメータ調整は根拠がある場合のみ
- コードの構造・コメント・スタイルは維持する
- 3戦略の基本的な枠組みは変えない
- リスク管理ルール（損切りライン等）は変えない
"""

    if constraints:
        base += f"""
【絶対に変更してはいけない設計原則】
以下はバグではなく意図した仕様です。絶対に変更しないでください：

{constraints}
"""

    base += """
出力形式:
1. まず「## ANALYSIS」セクションに変更内容と根拠を箇条書きで書く（日本語）
2. 次に「## CODE」セクションに改善済みの完全な simulator.py を```python で囲んで書く

必ず完全なコードを出力すること（省略・中断禁止）。"""

    return base


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
        with urllib.request.urlopen(req, timeout=120) as res:
            data = json.loads(res.read())
            stop_reason = data.get("stop_reason", "")
            text = data["content"][0]["text"]
            if stop_reason == "max_tokens":
                # 【安全対策】途中で切れた出力を採用すると壊れたコードを書き込むため、実行を中止する
                raise RuntimeError(
                    "出力が max_tokens で途中終了しました。壊れたコードを書き込まないため中止します。"
                    "（MAX_TOKENS を増やすか、コードを分割してください）"
                )
            return text
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API エラー {e.code}: {body}")


# ── 分析・最適化ロジック ──────────────────────────────────

def parse_response(response: str) -> tuple[str, str]:
    """ClaudeのレスポンスからANALYSISとCODEを抽出

    【重要な修正】
    以前は「最初に見つかった ```python ブロック」を無条件に採用していた。
    そのため、ANALYSIS の説明文中に置かれた短いスニペット（例: 数行の修正例）を
    完全な simulator.py と誤認し、本体を数行の断片で上書きして破壊する事故が起きた。

    対策:
    1. コードは可能な限り「## CODE」セクション以降からのみ抽出する
       （説明中のスニペットを拾わない）
    2. 複数のコードブロックがある場合は「最も長いブロック」を採用する
       （説明用の短い断片ではなく本体を選ぶ）
    実際に上書きしてよいかの最終判断は validate_new_code() が別途行う。
    """

    # デバッグ用：レスポンス冒頭200文字を表示
    print(f"--- Claude レスポンス冒頭 ---\n{response[:200]}\n---")

    # ANALYSIS セクション
    analysis_match = re.search(
        r"## ANALYSIS\s*(.*?)(?=## CODE|\Z)", response, re.DOTALL
    )
    analysis = analysis_match.group(1).strip() if analysis_match else "（分析なし）"

    # コード抽出対象の領域を決める：「## CODE」以降があればそこに限定する
    code_region = response
    code_header = re.search(r"## CODE\s*(.*)", response, re.DOTALL)
    if code_header:
        code_region = code_header.group(1)

    # 領域内の全コードブロックを取得し、最も長いものを採用
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", code_region, re.DOTALL)
    if not blocks:
        # フォールバック：レスポンス全体から全コードブロックを探す
        blocks = re.findall(r"```(?:python)?\s*(.*?)```", response, re.DOTALL)

    if not blocks:
        print(f"--- Claude レスポンス全文 ---\n{response}\n---")
        raise ValueError(
            "Claudeのレスポンスにコードブロックが見つかりませんでした。"
            "上のログでレスポンス全文を確認してください。"
        )

    # 最も長いブロック = 本体である可能性が高い
    code = max(blocks, key=len).strip()
    return analysis, code


def validate_new_code(new_code: str, old_code: str) -> None:
    """上書き前の安全ゲート：壊れた／不完全なコードで simulator.py を破壊しないための検査。

    どれか1つでも引っかかったら例外を送出し、書き込みを中止する。
    """
    old_lines = len(old_code.splitlines())
    new_lines = len(new_code.splitlines())

    # (1) 極端に短い出力は破損とみなす（断片の誤取得・途中切れ対策）
    #     旧コードの半分未満、または 100 行未満なら異常。
    min_lines = max(100, old_lines // 2)
    if new_lines < min_lines:
        raise ValueError(
            f"新コードが短すぎます（新{new_lines}行 / 旧{old_lines}行、下限{min_lines}行）。"
            "説明用スニペットの誤取得か出力の途中切れの可能性があるため、上書きを中止します。"
        )

    # (2) Python構文チェック：構文エラーのあるコードは絶対に書き込まない
    try:
        compile(new_code, SIMULATOR_FILE, "exec")
    except SyntaxError as e:
        raise ValueError(f"新コードに構文エラーがあります（{e}）。上書きを中止します。")

    # (3) 必須要素の存在チェック：本体の骨格が消えていないか
    required_tokens = ("def main(", "if __name__", "def load_state(", "def save_state(")
    missing = [t for t in required_tokens if t not in new_code]
    if missing:
        raise ValueError(
            f"新コードに必須要素が見つかりません: {missing}。"
            "本体が欠落している可能性があるため、上書きを中止します。"
        )


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
    constraints  = load_design_constraints()

    print(f"ログ行数: {len(log_csv.splitlines())}行")
    print(f"シミュレーター: {len(simulator_py.splitlines())}行")
    if constraints:
        print(f"設計制約: {DESIGN_CONSTRAINTS_FILE} を読み込みました")
    else:
        print(f"設計制約: {DESIGN_CONSTRAINTS_FILE} が存在しません（制約なし）")

    # システムプロンプトを動的に生成
    system_prompt = build_system_prompt(constraints)

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
    response = call_claude(system_prompt, user_message)

    # レスポンスをパース
    analysis, new_code = parse_response(response)

    # 変更がない場合はスキップ
    if new_code.strip() == simulator_py.strip():
        print("変更なし: ロジックは最適な状態です")
        log_entry = f"\n---\n## {ts}\n\n変更なし: ロジックは最適な状態と判断されました。\n"
        append_optimizer_log(log_entry)
        return

    # 【安全ゲート】壊れた／不完全なコードで simulator.py を破壊しないための最終検査
    # 検査に落ちた場合はここで例外送出 → 書き込み・コミットは一切行われない
    validate_new_code(new_code, simulator_py)

    # simulator.py を上書き
    write_file(SIMULATOR_FILE, new_code + "\n")
    print(f"simulator.py を更新しました（{len(new_code.splitlines())}行）")

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
