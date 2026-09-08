# 計画: 出力長超過（`max_tokens` 到達）のリトライ対応

- **日付**: 2026/08/07 07:19
- **起票**: `docs/issues/20260807001.md`

## 目的

3600セッションの本番実行で4セッション（0.11%）が、出力長超過を「何度投げても同じ 400」と
扱われて丸ごと失われた。失敗したセッションは1行も書き出さないので、そのペルソナが既に
答え終えていた設問の回答まで消える。欠けたのは4回答ではなく4セッション分の全設問。

3596セッションが同じ `max_tokens` で通っているので、これは設定不足による決定論的な失敗では
ない。出力長が非決定的に揺れ、稀に予算を超えるという**分布の裾**の事象であり、枠を広げた
再送に意味がある。分類が事実と合っていないのを直す。

## 何が起きていたか

`persona_sim/llm/databricks.py` の `_is_retryable()` は 429 と 5xx しか再試行対象にしない。
出力長超過は HTTP 400 で返るため非リトライ分岐に落ち、即座に `LLMError` として送出される。
これが `_ask` → `run_session` を素通りし、`executor.py` の `_collect` がセッション失敗として
計上する。

`_ask` は既にパース失敗に対して「同じ入力を再送する。揺れを作るのはエンドポイント側の
非決定性だけ」という理屈で再送しているのに、同じ非決定性が原因の出力長超過だけが1回で
諦めていた。

併せて、`finish_reason` をリポジトリのどこでも読んでいなかった。200 + `finish_reason: "length"`
で途中までの本文を返すエンドポイントでは、切り詰められた回答がフラグなしで `answer_raw` に
流れる。

## 方針（ユーザー確認済み）

1. 予算を段階的に**引き上げて**再送する（同じ枠でのバックオフではない）
2. 専用の品質フラグを立てる
3. 本調査とスクリーニング両方を対象にする
4. `finish_reason` の未検査も併せて直す
5. 天井まで広げても通らなかった場合、**セッション失敗にはしない**

## 対象ファイルと変更内容

| ファイル | 変更 |
|---|---|
| `persona_sim/llm/client.py` | `OutputLimitExceeded` を `LLMError` の下に新設。`max_tokens` と `partial_text` を持つ |
| `persona_sim/llm/budget.py` | **新規**。`budgets()` / `complete_within_budget()` / `output_budget_warning()` と定数 |
| `persona_sim/llm/databricks.py` | `_looks_like_output_limit()` 追加、`_invoke_with_backoff` の判定順、`_to_completion` の `finish_reason` 検査と `_partial_text()` |
| `persona_sim/run/flags.py` | `OUTPUT_LIMIT` を `ALL_FLAGS` と `QUALITY_FLAGS` に追加 |
| `persona_sim/run/session.py` | `OutputBudgetState`、`SessionContext.output_budget`、`_ask` の closure 化と短絡、`_to_record` |
| `persona_sim/run/run.py` | 状態の生成、`RunResult` のカウンタ、`_summarize` の警告 |
| `persona_sim/panel/infer.py` | `judge_batch` をヘルパ経由に、`InferBatchResult` / `InferResult` のカウンタ |
| `persona_sim/panel/screening.py` | 状態の生成、`ScreeningResult` のカウンタ、`_finish` の警告 |
| `persona_sim/metadata.py` | model ブロックに `output_limit` |
| `SPEC.md` §6.3 | 新挙動を記載 |
| `tests/test_budget.py` | **新規** |
| `tests/test_databricks_client.py` / `test_session.py` / `test_infer.py` | 追加 |

## 設計上の判断

### 判定順は出力長超過を先に

`_invoke_with_backoff` で構造化出力の判定より前に置く。誤分類のコストが非対称なため。
出力長超過を `StructuredOutputUnsupported` と取り違えると、`StructuredOutputState` は
実行全体で共有される1個なので、**調査まるごとが正規表現パースに落ちる**。逆向きの
取り違えは1呼び出しで済む。

観測された文面は "max_tokens" を含み構造化出力のヒントを1つも含まないので現時点で衝突は
しない。順序は将来の保険。

### 引き上げはパース試行を消費しない

構造化出力のフォールバックと同じ扱いにする。混ぜると `responses.attempt` から
パース失敗の頻度が読めなくなる。

### 使い切ったらパースループを短絡する

短絡しないと残りのパース試行も同じように天井まで広げて使い切り、1設問あたりの呼び出しが
3倍に膨れる。

### フラグは使い切ったときだけ立てる

`flags.py` が `RETRIED` を `QUALITY_FLAGS` から外す論拠——「リトライは経緯であって、
最終的に得られた回答の質ではない」——がそのまま当てはまる。広げて通った回答は劣化していない。
引き上げが起きた回数そのものは実行メタデータと警告で表面化させる。

### 引き上げ方針は設定項目にしない

`ModelConfig` にフィールドを足すと、dataclass・`loader.py` の重複したリテラル既定値・
2つの allow-tuple・`_model_fingerprint`・メタデータまで波及する。リポジトリは再試行方針を
既にモジュール定数で持っている（`PARSE_MAX_ATTEMPTS` 等）のでそれに倣う。

## 検証方法

- `./scripts/run-tests.sh`（非Spark既定）
- `-m spark` で Spark 側
- 実機の最終確認は、失敗した4セッションを同じコマンドの再実行で埋めること

## 対象外

- **`model.thinking` が payload に載っていない**（`docs/issues/202607281100.md`）。裾の真因だが、
  抑止パラメータの実機確認が要る。**予算超過の頻度はこれを直すまで下がらない**
- `config/ui_config.yaml` の `max_tokens` 引き上げ（引き上げ再送が自己修復する）
- 引き上げ方針の設定項目化
