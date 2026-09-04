# run_survey ノートブックにトークン使用量の表示を追加する（計画）

- 日時: 2026-08-05 06:58:50
- 起票: `docs/issues/20260805003.md`
- 対象: `notebooks/run_survey.ipynb` と、その材料になるスクリーニングの結果オブジェクト

## 1. 目的

本番ジョブ用ノートブックの最後に、スクリーナーと回答生成それぞれの入力・出力トークンと
合計を表示する。単価はワークスペースごとに違うためシステム側は金額換算を持たない方針
（`SPEC.md` §9、`metadata.py` の `estimated_cost: None`）で、利用側が費用を把握する
唯一の手掛かりがトークン数なのに、ジョブ実行時はそれが目に入らない。

## 2. 現状と要求のギャップ

| # | 要求 | 現状 | 対応 |
|---|---|---|---|
| 1 | 回答生成のトークン | `RunResult.input_tokens` / `output_tokens` が既にある（`run/run.py`）。CLI は表示済み | そのまま使う |
| 2 | スクリーナーのトークン | `ScreeningResult` に**フィールドが無い**。行としては `screener_responses` にあるが結果が集計していない | 集計を新設 |
| 3 | 合計 | 無し | 表示セルで足す |

## 3. 実装上の落とし穴

`mode: infer` は1バッチ＝1呼び出しだが、`_to_record()` が候補1人につき1行を作り、
その全行に同じバッチのトークン数を載せている。回答生成側と同じ「レコード単純合算」を
スクリーナーに持ち込むと `batch_size` 倍に膨れる。**`infer` は呼び出し単位で足す。**

## 4. 決定事項

- 集計範囲は**この実行ぶんのみ**（`run_metadata.json` の `tokens_scope: "run"` と同じ）。
  再開でスキップしたセッション・判定済みで聞き直さなかった候補は含まない。
  調査全体の累計案は、上記3により `infer` のスクリーナー行をテーブルから正しく合算
  できないため採らない。
- CLI（`persona-sim screen`）の表示は変えない（起票の範囲に限る）。

## 5. 変更内容

| ファイル | 変更 |
|---|---|
| `persona_sim/run/executor.py` | `ExecutionResult` に `input_tokens` / `output_tokens` プロパティ（`records` 合算）を追加 |
| `persona_sim/run/run.py` | `_summarize()` の同じ式をプロパティ参照に置き換え（挙動不変） |
| `persona_sim/panel/infer.py` | `InferResult` にトークンを追加し、`run_inference()` のループで**バッチ単位**に加算 |
| `persona_sim/panel/screening.py` | `ScreeningResult` と `_InferExecution` にトークンを追加。`screen_survey()` の集計箇所で足し込む |
| `notebooks/run_survey.ipynb` | 末尾に markdown + code の2セルを追加 |

`_InferExecution` は `ExecutionResult` と同じ属性名で持たせ、呼び出し側を1本の経路に保つ
（既存 docstring の意図どおり）。加算は中断の early return より前に置き、**中断しても
呼んだぶんは計上済み**にする。再試行ループで複数回通るので `+=`。

## 6. テスト

- `tests/test_infer.py`: バッチのトークンがレコード数ぶんに膨れないこと（今回いちばん壊れやすい）。
- `tests/test_screening_spark.py`: 初回は正、再実行は 0（この実行ぶんのみ）。`assume` は 0。
- `tests/test_run_survey_notebook.py`: 末尾セルが両方の結果を参照していること（セルが消えたら落とす）。

## 7. 検証

`./scripts/run-tests.sh` と、`-m spark` でのスクリーニング／実行まわり。
ノートブックは Databricks 前提で CI 実行しないため、セルの実挙動はスタブを入れた
`exec` で表の整形を確認する。
