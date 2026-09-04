# 結果: `infer` の判定失敗が「非通過」として確定する問題の修正

- **日付**: 2026/08/05 04:08
- **計画**: [20260805-040837-infer-judge-failure-plan.md](20260805-040837-infer-judge-failure-plan.md)
- **起票**: `docs/issues/20260805001.md`

## 調査で分かったこと

`docs/original.html` / `docs/retry_1st.html` は Databricks ノートブックのエクスポートで、
本文は `__DATABRICKS_NOTEBOOK_MODEL`（base64 + URL エンコードの JSON）に入っている。
デコードして各セルの `state` / `errorSummary` を読んだ結果、**2つのエラーは同じ1つの
原因の症状**であり、表示されていた理由はどちらも事実と違っていた。

実行は `survey_id=test_20260805021426_5803c0`、`mode: infer` /
`oversample_factor: 2` / `batch_size: 30`、候補は `M_20_29` の10名（要求5名×2）。

| | 表示されたエラー | 実際に起きていたこと |
|---|---|---|
| 1度目 | `role=main のパネルが1件も無い。先に screen を実行すること` | 判定が全バッチ失敗し、中断が握りつぶされた |
| 2度目 | `実インシデンス 0.0% / 通過者が0名のため候補を何倍に増やしても届かない` | 1度目の失敗が「判定済み・非通過」として残り、LLM を1回も呼んでいない |

- 1度目: 全バッチ `LLMError` → `_infer_execution` が `aborted_reason` を立てるが、その前に
  `screener_responses` へ行を書いて `return`（`finalize_panel` に到達せずパネルは
  `candidate` のまま）。`aborted_reason` は `persona_sim/cli.py` は見ているのに
  `notebooks/run_survey.ipynb` の screen セルが見ていなかった。
- 2度目: 1度目の10行が現在の `config_hash` を持つ「判定済み」として
  `read_screener_codes` に読み戻され `todo` が空 → 判定を呼ばないまま全員 `screened_out`
  に確定 → `unreachable_cells` が通過率0と見て E2 で停止。

**通信の失敗が「この人たちは条件に合わなかった」という判定結果として永続化され、
再実行しても二度と聞き直されない**状態だった。`assume` のインシデンスを 1.0 と記録
しない（聞いていないので測れていないのであって、全員が該当したわけではない）のと
同じ性質の誤りが、判定失敗側で起きていた。

## 変更内容

### 1. 判定できなかった候補を「判定済み・非通過」として残さない（根本原因）

- `persona_sim/run/flags.py`: スクリーナー専用フラグ `JUDGE_ERROR = "judge_error"` を追加。
  `INFERRED` と同じく `ALL_FLAGS` には入れない（`responses` には現れず、実行メタデータに
  常に0の項目を増やさないため）。
- `persona_sim/panel/infer.py`
  - `_to_record`: バッチ失敗時のフラグを `PARSE_ERROR` → `JUDGE_ERROR` に変更。
    通信失敗はパース失敗（返ってきたが番号が取れなかった＝非通過）ではない。
    行自体は残すので `answer_raw` にエラー文が証跡として残る。
  - `_collect`: 失敗バッチの候補を `InferResult.codes` に載せない。
  - `InferResult.errors` を追加し、失敗バッチのエラー文を集める。
- `persona_sim/panel/screening.py`
  - `read_screener_codes`: `flags` に `judge_error` を含む行を返さない
    （`flags` が null の古い行は落とさない）。`screen_survey` の `todo` 算出と
    `load_premises` の両方が「未判定」として扱い、再実行で聞き直される。
    再判定時は主キーが同じなので `merge_upsert` が上書きする。
  - `_infer_execution`: `aborted_reason` に実際のエンドポイントエラー文を含める。
    従来の「エンドポイントの設定を確認すること」だけでは、原因を
    `screener_responses.answer_raw` まで掘らないと読めなかった。

### 2. 中断をジョブの失敗にする

`notebooks/run_survey.ipynb` の screen セルで判定数（完了/全体、スキップ）を表示し、
`aborted_reason` があれば `RuntimeError` を送出する。ノートブック冒頭の
「どこかで失敗したら例外を送出してジョブを失敗させます」という方針と、
run セル（`sessions_failed` で raise）の書き方に揃えた。

## 検証

- `ruff check .` パス。
- 非 Spark: `./scripts/run-tests.sh` で **473件全件パス**（新規3件）。
- Spark: `pytest -m spark` で **72件全件パス**（新規2件）。
- **回帰テストが修正を戻すと落ちることを確認済み。**
  `read_screener_codes` のフィルタと `_collect` の変更を一時的に戻して
  `test_infer_rejudges_candidates_whose_judgement_failed` を実行すると、
  報告と同じ形（`M_20_40s: 不足 30 名（通過 0 / 判定 120）` /
  `実インシデンス: M_20_40s=0.0%` / `通過者が0名のため、候補を何倍に増やしても届かない`）で
  `ScreenerShortfallError` になる。

新規テスト:

| ファイル | テスト | 見るもの |
|---|---|---|
| `tests/test_infer.py` | `test_run_inference_leaves_failed_batches_unjudged` | 失敗バッチが `codes` に載らない／`errors` に理由が残る |
| `tests/test_infer.py` | `test_run_inference_marks_failed_records_as_judge_error` | `judge_error` が立ち `parse_error` は立たない／エラー文が残る |
| `tests/test_screening_spark.py` | `test_infer_endpoint_failure_does_not_finalize_the_panel` | 判定全滅でパネルを確定させない／中断理由にエラー文が入る |
| `tests/test_screening_spark.py` | `test_infer_rejudges_candidates_whose_judgement_failed` | 判定に失敗した候補を再実行で聞き直す（本 issue の回帰） |
| `tests/test_run_survey_notebook.py` | `test_screening_abort_fails_the_job` | screen セルが `aborted_reason` を見て raise する |

## 未対応事項

- **呼び出しがなぜ失敗したかは未確定。** エラー本文がノートブック出力にもジョブログにも
  出ておらず、`screener_responses.answer_raw` にしか残っていなかったため。修正版で
  再実行すれば screen セルがエラー文つきで止まるので、そこで判定エンドポイント側
  （存在・権限・レート制限）を切り分けること。**Databricks 実環境での確認は未実施。**
- **デプロイ済みアプリの設定が repo と食い違っている可能性。** 実行時の
  `ScreeningConfig` は `oversample_factor=2` / `persona_card` が既定の9項目だが、
  `app/config/ui_config.yaml` は `oversample_factor: 4` / 定性フィールド1項目。
  今回の失敗の原因ではないので触っていないが、アプリの再デプロイが要るかもしれない。
- **一部のバッチだけが失敗した場合は、同じ誤りが小さい規模で残っている。**
  全バッチ失敗（今回の事例）は中断して確定させないが、一部だけ失敗してパネルが
  必要数を満たしてしまうと、`finalize_panel` が「`passed` に居ない候補＝非通過」として
  判定できなかった候補も `screened_out` にする。実インシデンスがその分だけ低く出る。
  正すには `finalize_panel` に `verdict.tested`（判定できた候補）を渡し、未判定を
  `candidate` のまま残す必要があり、`_already_finalized` の意味づけにも及ぶ。
  今回の報告事例とは別経路なので、スコープ外として issue に残した。
- **判定は成功したのに番号が1つも取れなかった場合**（「該当者なし」と出力不能の区別）は
  今も付いていない。今回の経路では起きていないため、論点として issue に残した。
- `screen_survey` の戻り値は例外に変えていない。CLI は集計表を出してから終了コード1を
  返す設計で、そこは正しく動いているため。
