# 結果: 出力長超過（`max_tokens` 到達）のリトライ対応

- **日付**: 2026/08/07 07:19
- **計画**: [20260807-071924-output-limit-retry-plan.md](20260807-071924-output-limit-retry-plan.md)
- **起票**: `docs/issues/20260807001.md`

## 変更内容

### 1. 例外の分離（`persona_sim/llm/client.py`）

`OutputLimitExceeded` を `LLMError` の下に新設した。`max_tokens`（実際に送った予算）と
`partial_text`（途中まで返っていた本文）を持つ。

`RetryableLLMError` と分けたのは打つ手が違うため。あちらは待てば直るのでバックオフするが、
こちらは待っても変わらず**枠を広げること**が対処になる。まとめると、広げれば通る呼び出しを
バックオフの待ち時間だけ使って捨てることになる。

### 2. 引き上げ再送（`persona_sim/llm/budget.py`、新規）

`budgets(max_tokens)` が試す予算の並びを返す。先頭は必ず設定値そのまま、以降 4倍・16倍で、
天井 `OUTPUT_LIMIT_CEILING = 4096` で頭打ちにして重複を畳む。設定値が既に天井以上なら
1回しか試さない（同じ枠で投げ直すだけの呼び出しを作らないため）。

`complete_within_budget(send, *, max_tokens)` は**送信そのものを呼び出し側の closure に
委ねる**。`_ask` は構造化出力のフォールバックを内側に持っており、クライアントをここへ渡すと
2つの方針が絡んで組み合わせが増えるため。このモジュールは予算の決め方だけを持つ。

天井まで広げても通らなければ `(None, outcome)` を返し、**送出はしない**。1設問の予算超過で
調査を止めないため。呼び出し側が `outcome` を見て、記録して続けるのか（本調査）判定失敗として
残すのか（`infer`）を決める。

### 3. 検出の2表面（`persona_sim/llm/databricks.py`）

**(a) 400 のメッセージ。** `_looks_like_output_limit()` を追加し、`_invoke_with_backoff` の
判定を**構造化出力より前**に置いた。誤分類のコストが非対称なため——出力長超過を
`StructuredOutputUnsupported` と取り違えると、`StructuredOutputState` は実行全体で共有される
1個なので**調査まるごとが正規表現パースに落ちる**。逆向きの取り違えは1呼び出しで済む。
観測された文面は "max_tokens" を含み構造化出力のヒントを1つも含まないので現時点で衝突はせず、
順序は将来の保険。

**(b) `finish_reason`。** `_to_completion` で `finish_reason == "length"` を検査し、
**`content is None` チェックより前**に置いた。推論だけで予算を使い切った 200 応答は本文が
空で返るので、後に置くと「応答の message.content が空」という原因を取り違えた失敗になる。
部分テキストの取り出しは `_partial_text()` で包み、`_content_to_text` が送出する場合は
空文字に落とす（完成した応答では黙って劣化させないが、打ち切られた応答では
「本文がまだ無い」のが正常なため）。

### 4. フラグ（`persona_sim/run/flags.py`）

`OUTPUT_LIMIT = "output_limit"` を `ALL_FLAGS` と `QUALITY_FLAGS` の両方に追加した。

**広げて通った回答には立てない。** `RETRIED` を `QUALITY_FLAGS` から外す論拠——
「リトライは経緯であって、最終的に得られた回答の質ではない」——がそのまま当てはまる。
回答が実際に劣化するのは天井まで使い切ったときだけで、それは `QUALITY_FLAGS` が
表すべきものそのもの。

`responses` のスキーマは `flags array<string>` なので**移行は不要**。

### 5. 本調査（`persona_sim/run/session.py`、`run.py`）

`_ask` の `client.complete` 呼び出しを `send` closure に切り出し、`complete_within_budget`
経由にした。引き上げは**パース試行を消費しない**——構造化出力のフォールバックと同じ扱いで、
`responses.attempt` は「パース失敗を何回やり直したか」の意味を保つ。混ぜると
`attempt` からパース失敗の頻度が読めなくなる。

使い切った場合は**パースループを短絡する**。短絡しないと残りの試行も同じように天井まで
広げて使い切り、1設問あたりの呼び出しが3倍に膨れる（`_needs_retry` は選択式で番号が
取れなければ真を返すので、確実にこうなる）。得られていた本文から `Completion` を組み立てて
通常の経路に流すので、選択式・本文なしなら `parse_answer("")` が `codes=()` を返して
`parse_error` も併せて立つ。

`run_session` が送出しなくなったので `executor._collect` は `records.extend` に進み
`sessions_ok` になる——**これが今回の主目的**。

引き上げ回数は `OutputBudgetState` に貯め、`RunResult` → `run_metadata.json` の
`model.output_limit` と `[W_OUTPUT_LIMIT]` 警告に出す。警告は `_summarize` の**早期 return
より前**に置いた（1件も書き出せなかった実行で消えると最も知りたい場面で何も残らない。
`retry_warning` と同じ理由）。

### 6. スクリーニング（`persona_sim/panel/infer.py`、`screening.py`）

`mode: ask` は `_ask` を素通しで使うので自動的に対応済み。`mode: infer` は
`judge_batch` の `client.complete` をヘルパ経由にした。

**`complete_within_budget` は `except LLMError` の内側で呼んでいる。** `OutputLimitExceeded`
は `LLMError` のサブクラスなので、直に `client.complete` を囲んだままだと引き上げが働く前に
握りつぶされ、1バッチまるごと未判定になる。

使い切った場合は現行どおり `JUDGE_ERROR`。この経路は証跡行を書き、`read_screener_codes` が
判定済みから除くので再実行で聞き直される——良い劣化の仕方なので変えていない。`error` 文言に
「予算を N まで広げても完了しなかった。`screening.model.max_tokens` を見直すか
`batch_size` を下げること」と入れて、`screener_responses.answer_raw` から直し方が読めるようにした。

警告はスクリーニングの単一集約点である `_finish()` に足した。

### 7. 仕様（`SPEC_PHASE1.md`）

§6.3 に引き上げ再送の3項目、§8 のフラグ表に `output_limit`、§9 のメタデータ例に
`model.output_limit` を追記した。

## 検証結果

- `ruff check persona_sim tests` — パス
- 非 Spark テスト **565件全件パス**（変更前538件、**27件追加**）
- Spark テスト **81件全件パス**（増減なし）

追加したテストのうち、意図的に「なぜその不変条件が要るのか」を固定したもの:

| テスト | 固定した不変条件 |
|---|---|
| `test_an_output_limit_400_is_classified_apart_from_a_bad_request` | 400 だが枠を広げれば通る。バックオフはしない（`slept == []`） |
| `test_a_structured_output_400_is_still_a_structured_output_rejection` | 新しい判定が構造化出力を横取りしない |
| `test_a_retryable_failure_is_never_read_as_an_output_limit` | 429 の本文に `max_tokens` が出ても待つほうを選ぶ |
| `test_finish_reason_length_with_no_content_reports_the_limit_not_an_empty_body` | 原因を取り違えない |
| `test_finish_reason_stop_is_a_normal_completion` | 過剰発火の番人 |
| `test_a_raised_budget_does_not_count_as_a_parse_retry` | `attempt` の意味を保つ |
| `test_an_exhausted_budget_records_a_flagged_answer_instead_of_failing_the_session` | **今回の主目的** |
| `test_an_exhausted_budget_stops_the_parse_loop` | 呼び出しが3倍に膨れない |
| `test_an_exhausted_judge_batch_is_unjudged_not_rejected` | 判定できていない候補を非通過にしない |

`FakeClient` の失敗注入はプロンプトのハッシュなので「予算を広げたら通る」を作れず、
`test_infer.py` の `_ScriptedClient` は `**kwargs` を捨てて `max_tokens` を見られない。
予算そのものが分岐条件なので `_BudgetClient` を新設した（`tests/test_session.py` と
`tests/test_infer.py` に各1つ）。

既存ヘルパは `_Boom` に任意メッセージ、`_complete` に `finish_reason` / `max_tokens` を
足した（400 の中身を分ける手がかりが文面しかないため）。

## 未対応事項

- **`model.thinking` が payload に載っていない**（`docs/issues/202607281100.md`）。**裾の真因。**
  エンドポイントは既定どおり思考し推論トークンが予算を食うので、**予算超過の発生頻度そのものは
  これを直すまで下がらない。** 今回の変更は起きたときに失われないようにするだけ。
  抑止パラメータ名と受け付けられる値は実機確認が要るため着手していない
- `config/ui_config.yaml` の `max_tokens` は変更していない（引き上げ再送が自己修復するため）
- 引き上げ方針（倍率・天井）の設定項目化。`ModelConfig` に足すと dataclass・`loader.py` の
  重複したリテラル既定値・2つの allow-tuple・`_model_fingerprint`・メタデータまで波及する
- `flags.py` の死んだヘルパ2つ（`straightline_personas` / `flag_rates`）はそのまま
- **Databricks 実環境での確認は未実施。** 失敗した4セッションは同じコマンドの再実行で埋まる
  （`run.py` の `_session_completed` により未完了の4件だけが走り、3596件は `sessions_skipped`）
