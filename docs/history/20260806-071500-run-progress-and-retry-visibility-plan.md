# run の進捗表示と、エンドポイント再試行の可視化（計画）

- **日付**: 2026/08/06 07:15
- **対象**: `persona_sim/run/progress.py`（新規）、`persona_sim/llm/{client,databricks,fake}.py`、
  `persona_sim/run/{run,executor}.py`、`persona_sim/panel/{screening,infer}.py`、
  `persona_sim/{cli,metadata}.py`、`notebooks/run_survey.ipynb`、`SPEC.md`

## 背景

Databricks 上で `run_survey()` が数十分〜1時間以上かかる状態になり、実行中に**一切の出力が無い**ため
遅いのか止まったのかを判別できない、という報告から始まった。

切り分けの過程で分かったこと:

- ノートブック（本番ジョブの入口）だけが `progress=` を渡していなかった。CLI は渡している。
- スクリーナーの実測は `avg_attempt=1.0 / p50 2,853ms / p95 22,902ms / max 25,316ms`。
  `attempt` はパース失敗の試行回数なので、この 8倍の裾はパース再試行では説明できない。
- 実行中に同じエンドポイントへ外から1発投げると 20,667ms（平常時 1,034ms）。
  つまり自分の `concurrency: 32` でエンドポイントを飽和させている。
- ところが**伝送層の再試行はどこにも記録されていない**。`DatabricksServingClient` は
  429/5xx を最大5回・指数バックオフ（1→2→4→8秒）で握って再試行するが、回数も待機時間も残らない。
  `Completion.latency_ms` はバックオフ込みで計測されるため、レイテンシだけ見ても
  「モデルの応答が遅い」のか「429 で眠っていた」のかを後から分けられない。

つまり、**今回いちばん必要だった情報が構造的に取得できない**状態だった。
`concurrency` の調整やエンドポイント割り当ての変更は、この可視化ができてから別途行う。

## 方針

観測できるようにすることだけを行う。実行の挙動（並列度・再試行回数・バックオフの計算）は変えない。

### 1. progress コールバックの契約を1つにする

進捗コールバックの引数の形が2種類あった:

| 経路 | 呼び出し |
|---|---|
| 本調査 / スクリーナー `ask` | `progress(ExecutionResult)` |
| スクリーナー `infer` | `progress(1)` |

CLI の `_progress` は前者しか想定していないため、**`persona-sim screen` を `infer` モード
（`ui_config.yaml` の既定）で実行すると `AttributeError` で落ちる**。表示を増やす前にここを揃える。

`persona_sim/run/progress.py` に `ProgressUpdate(done, total, failed)` を置き、
`executor.execute()` と `infer.run_inference()` の双方がこれを渡す。

### 2. コンソール向けレポータ

同じモジュールに `console_reporter()` を置き、CLI とノートブックの両方から使う。

- **時間で間引く**（既定30秒）。件数で間引く現行実装（50件ごと）では、1セッション20秒級の状態で
  更新が5分に1回になり、生存確認の役に立たない。完了時は必ず出す。
- 経過・残り（実測スループットからの ETA）・スループット・失敗数を出す。
- 端末では `\r` で上書き、ノートブック／ジョブログでは改行して積む
  （Databricks のセル出力は `\r` の上書きを期待できない）。
- `client` を渡した場合のみ、再試行の状況を行末に足す。

### 3. HTTP 再試行の記録と警告

- `llm/client.py` に `RetryStats`（呼び出し数・再試行した呼び出し数・再試行回数・
  バックオフ合計秒・理由別の内訳）と `SupportsRetryStats` プロトコルを追加する。
  **`LLMClient` 本体には足さない**。`complete` だけを持つスタブが tests に5つあり、
  必須メソッドにすると巻き込むため。呼ぶ側は `isinstance` で判定する。
- `DatabricksServingClient` にスレッドセーフなカウンタを持たせる。成功・失敗どちらで抜けても計上する。
- 本調査（`RunResult`）とスクリーニング（`ScreeningResult`）の両方に載せ、5%（E3・E4 と同じ閾値）を
  超えたら `W_ENDPOINT_RETRY` を `warnings` に積む。文面には内訳と次の一手を含める。
- `run_metadata.json` の `model.endpoint_retries` に残す。`SPEC.md` §9・§11 を同時に更新する
  （仕様が実装の根拠なので、書かずに実装するとドリフトになる）。

### 4. ノートブック

- run セル: `build_client(survey.model)` でクライアントを組み立て、`run_survey` と
  `console_reporter` で共有する（共有しないと進捗行に再試行を出せない）。
- screen セル: `progress=console_reporter()` を渡す。
- 両セルとも `warnings` を印字する（今は出していないため警告が画面に出ない）。

## 検証

1. `./scripts/run-tests.sh`（非 Spark）と `-m spark`
2. `ruff check`
3. `examples/survey_sample_smoke.yaml`（`endpoint: fake`）を `persona-sim run` で流し、
   進捗行と完了行が出ることを確認
4. `infer` モードの調査定義で `persona-sim screen` を流し、`AttributeError` が出ないことを確認

## スコープ外

- `concurrency` の既定値の変更、エンドポイント側の割り当て変更（可視化の結果を見てから）
- 呼び出しごとのタイムアウト設定と、`responses` の中間コミット（別途）
- `responses` スキーマへの再試行回数の追加（Delta のスキーマ移行が要るため、
  まずクライアント単位の集計で足りるかを見る）
