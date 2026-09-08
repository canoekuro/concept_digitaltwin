# run の進捗表示と、エンドポイント再試行の可視化（結果）

- **日付**: 2026/08/06 07:15
- **計画**: [20260806-071500-run-progress-and-retry-visibility-plan.md](20260806-071500-run-progress-and-retry-visibility-plan.md)

## やったこと

### 1. progress コールバックの契約を1つにした（既存バグの修正を含む）

進捗コールバックの引数の形が2種類あった。`executor.execute()` は `ExecutionResult` を渡し、
`infer.run_inference()` は整数 `1` を渡していた。CLI の `_progress` は前者しか受け取れないため、
**`persona-sim screen` を `mode: infer`（`ui_config.yaml` の既定）で実行すると
`AttributeError: 'int' object has no attribute 'sessions_ok'` で落ちる**状態だった。
進捗表示に手を入れる前提として、ここを揃えた。

`persona_sim/run/progress.py` に `ProgressUpdate(done, total, failed)` を新設し、両方がこれを渡す。
`ratio` は `total=0` を 1.0（＝やることが無い＝完了）として扱い、0除算と「100%超え」を潰している。
`panel` から `run` への import は既存（`screening.py` が `run.executor` を使っている）なので層は増えていない。

### 2. コンソール向けレポータ

同じモジュールの `console_reporter()` に表示を集約し、CLI とノートブックの両方から使う。
`cli.py` の `_progress` は削除して置き換えた。

- **間引きを件数から時間へ変えた**（既定30秒）。従来の「50件ごと」は、1セッション20秒級の状態では
  更新が5分に1回になり、生存確認の役に立たない。**完了行は間引かない**（最後の1行が出ないと
  完走したのか止まったのか分からないため）。ただし完了行は1回だけ。
- 出力は `10/100（10%） · 経過 01:00 · 残り 約09:00 · 10.0 件/分 · 失敗 1`。
  **残り時間は見積もり値ではなく実測スループットから出す**ので、エンドポイントが遅くなれば
  そのぶん残りも伸びる。まだ1件も終わっていないうちは残りを出さない（割り算できないため）。
- `isatty()` が真のときだけ `\r` で上書きし、偽（Databricks のセル出力・ジョブログ）では改行して積む。
  上書き時は前行の幅までパディングして残骸を消す。
- `client` を渡したときだけ、行末に再試行の状況を足す。**再試行が0のときは出さない**
  ——平常時に「再試行 0回」が毎行並ぶと、本当に増えたときに気づけないため。

### 3. HTTP 再試行の記録と警告

`DatabricksServingClient` は 429/5xx を最大5回・指数バックオフ（1→2→4→8秒、ジッタ付き）で
握って再試行するが、その回数も待機時間も**どこにも残っていなかった**。
`Completion.latency_ms` は `_invoke_with_backoff` を挟んで計測されるためバックオフ込みで、
`responses.attempt` はパース失敗の試行回数であって伝送層の再試行ではない。
結果として「モデルの応答が遅い」のか「429 で眠っていた」のかを実行後に切り分けられなかった。

- `llm/client.py` に `RetryStats`（`calls` / `retried_calls` / `retries` / `backoff_seconds` /
  理由別 `reasons`）と `SupportsRetryStats` プロトコル、`retry_stats_of()` / `retry_warning()` を追加。
  **`LLMClient` 本体には足していない**——`complete` だけを持つスタブが tests に5つあり、
  必須メソッドにすると巻き込むため。呼ぶ側は `isinstance` で判定する。
- `DatabricksServingClient` にロック付きカウンタを持たせた。計上は `finally` で行うので、
  成功・再試行不能・構造化出力の非対応・連続失敗のどの経路で抜けても数え落とさない
  （**全滅した呼び出しこそ再試行が多い**ので、ここを取りこぼすと「詰まっているのに統計上は綺麗」になる）。
  再試行回数は「余分に投げた回数」＝ `attempts - 1`、`reasons` は失敗した試行の内訳なので、
  全滅時は内訳の合計が再試行回数より1多い。理由キーは HTTP ステータスが取れればその値
  （429 と 503 は打つ手が違うのでまとめない）、無ければ例外クラス名。
- `FakeClient` にも `retry_stats()`（常に0、`calls` だけ実数）を足した。fake 経路でも本番と同じ形で通る。
- `RunResult.retry_stats` と `ScreeningResult.retry_stats` に載せ、閾値 5%（E3・E4 と同じ）を超えたら
  `W_ENDPOINT_RETRY` を `warnings` に積む。文面に内訳と次の一手（`model.concurrency` を下げる／
  エンドポイント側の割り当てを確認する）を入れた。
  警告は `_summarize()` の**早期 return より前**に出している——1件も書き出せなかった実行こそ
  再試行が多いはずで、そこで消えると最も知りたい場面で何も残らない。
- `ScreeningResult.warnings` は**フィールドだけあって誰も書き込んでいなかった**ので、ここが最初の書き手。
  あわせて CLI の screen 経路で `result.warnings` を印字するようにした（従来は run 経路のみ）。
  `screen_survey()` は戻り口が3箇所あるので `_finish()` に切り出して全経路で同じものを載せている。
- `run_metadata.json` の `model.endpoint_retries` に残す。`SPEC.md` §9 の JSON 例と
  §11 の注記を同時に更新した。

### 4. ノートブック（`notebooks/run_survey.ipynb`）

- run セル: `build_client(survey.model)` でクライアントを作り、`run_survey` と `console_reporter` で
  共有する。共有しないと進捗行に再試行を出せない。**モデルは調査定義から引く**
  （`run_survey` が内部で行うのと同じ呼び出し）ので、実際に叩く先は従来と変わらない。
- screen セル: `progress=console_reporter()` を渡す。
- 両セルとも `warnings` を印字する。従来は印字しておらず、`W_STRUCTURED_OUTPUT` も
  今回の `W_ENDPOINT_RETRY` も画面に出ないままだった。

計画外の変更が1点ある。`main`（`b3025f2`）の panel セルに入っていたデバッグ用の
`importlib.reload` 3行が `ruff` の I001（import の並び）に引っかかっており、**`ruff check .` は
このコミットの時点で既に失敗していた**（CI の lint ジョブが赤い状態）。このままでは今回の変更も
CI を通せないため、**import の並びだけ**直した。セルの挙動は変えていないし、行そのものも消していない。
なお `ruff --fix` は使っていない——ノートブックに掛けると Databricks が書いたウィジェット定義の
JSON キー順まで並べ替えられ、35行ぶんの無関係な差分が出るため、その1箇所だけ手で直している。

## 検証

- `ruff check persona_sim tests app` パス
- 非 Spark テスト **536件全件パス**（変更前 505件、31件追加）
- Spark テスト **81件全件パス**（変更前 76件、5件追加）
- 表示と警告文は実際に出力させて確認した（非 TTY・TTY・平常時の各パターン）

追加したテストのうち、`test_run_inference_reports_progress_in_the_shared_shape` は
変更前のコード（`progress(1)`）で落ちることを確認済み。

## 未対応（意図的に外したもの）

- **`concurrency` の調整とエンドポイント側の割り当て変更**。今回は観測できるようにしただけで、
  実行の挙動（並列度・再試行回数・バックオフの計算）は一切変えていない。
  実測（実行中の外部プローブで 20,667ms、平常時 1,034ms、スクリーナー p95 22,902ms）からは
  `concurrency: 32` がエンドポイントの処理能力を超えている可能性が高いが、まず記録を取ってから判断する。
- **呼び出しごとのタイムアウト**。現状 `api_client.do` に timeout の指定が無く、エンドポイントが
  無応答になるとワーカースレッドが永久にブロックする。閾値の決めが要るので別途。
- **`responses` の中間コミット**。`run_survey()` は完走まで書かないので、手動中断でその実行ぶんが
  丸ごと消える（E5 中断は結果として返るため保存される）。Delta への書き出し単位の設計が要るので別途。
- **`responses` スキーマへの再試行回数の追加**。Delta のスキーマ移行が要るため、まずクライアント単位の
  集計で足りるかを見る。
- **Databricks 実環境での確認は未実施。**

## 報告事項（今回は触っていない）

`origin/main` の `b3025f2`（手元での動作確認のコミット）に、デバッグの痕跡が2点残っている。

- `notebooks/run_survey.ipynb` の panel セル冒頭の `importlib.reload(...)` 3行
- `persona_sim/panel/sampling.py` の `except Exception: pass`（serverless でキャッシュ不可を握りつぶす）。
  キャッシュ以外の失敗も飲み込むので、対象例外を絞るか、握りつぶしたことを1回だけ出す形が安全
