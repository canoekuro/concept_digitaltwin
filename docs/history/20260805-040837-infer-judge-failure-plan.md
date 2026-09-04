# 計画: `infer` の判定失敗が「非通過」として確定する問題の修正

- **日付**: 2026/08/05 04:08
- **起票**: `docs/issues/20260805001.md`

## 目的

UI から実行した調査（`test_20260805021426_5803c0`）が2回連続で別々のエラーになり、
**どちらの表示理由も事実と違っていた**。実際の原因は「`infer` 判定の LLM 呼び出しが
全バッチ失敗したこと」1点。失敗がどこにも表面化せず、しかも失敗が「この候補は条件に
合わなかった」という判定結果として永続化されるため、再実行しても回復しない。ここを直す。

## 何が起きていたか

`docs/original.html` / `docs/retry_1st.html`（Databricks ノートブックのエクスポート）を
デコードして確認した。調査は `mode: infer` / `oversample_factor: 2` / `batch_size: 30`、
候補は `M_20_29` の10名。

1. **1度目**: 全バッチが `LLMError` で失敗 → `_infer_execution` が `aborted_reason` を
   立てるが、その前に `screener_responses` へ行を書き `return`（`finalize_panel` に
   到達しないのでパネルは `candidate` のまま）。`aborted_reason` は
   `persona_sim/cli.py` は見ているが `notebooks/run_survey.ipynb` は見ておらず、
   中断が握りつぶされて次のセルの `run_survey` が
   `role=main のパネルが1件も無い。先に screen を実行すること` で落ちた。
2. **2度目**: 1度目に書かれた10行が、現在の `config_hash` を持つ「判定済み・非通過」
   として `read_screener_codes` に読み戻される → `todo` が空 → **LLM を1回も呼ばないまま**
   全員 `screened_out` に確定 → `unreachable_cells` が通過率0と見て
   `ScreenerShortfallError: 実インシデンス 0.0% / 通過者が0名のため候補を何倍に
   増やしても届かない` で停止。

`assume` のインシデンスを 1.0 と記録しない（聞いていないので測れていないのであって、
全員が該当したわけではない）のと同じ性質の誤りが、判定失敗側で起きている。

## 変更内容

### 1. 判定できなかった候補を「判定済み・非通過」として残さない（根本原因）

- `persona_sim/run/flags.py`: スクリーナー専用フラグ `JUDGE_ERROR = "judge_error"` を追加。
  `INFERRED` と同様 `ALL_FLAGS` には入れない（`responses` には現れないため）。
- `persona_sim/panel/infer.py`
  - `_to_record`: バッチ失敗時のフラグを `PARSE_ERROR` から `JUDGE_ERROR` に変える。
    通信失敗はパース失敗ではない。行自体は残す（`answer_raw` にエラー文＝証跡）。
  - `_collect`: 失敗バッチの候補を `InferResult.codes` に載せない。
- `persona_sim/panel/screening.py` `read_screener_codes`: `flags` に `judge_error` を
  含む行を返さない。`screen_survey` の `todo` 算出と `load_premises` の両方が
  「未判定」として扱い、再実行で聞き直される（同じ主キーなので upsert で上書き）。

### 2. 中断をジョブの失敗にする

- `notebooks/run_survey.ipynb` の screen セル: 判定数（`sessions_ok / sessions_total`、
  スキップ数）を表示し、`aborted_reason` があれば `RuntimeError` を送出する。
  ノートブック冒頭の「どこかで失敗したら例外を送出してジョブを失敗させます」に揃える。

### 3. 中断理由に実際のエラー文を載せる

- `persona_sim/panel/infer.py`: `InferResult` に失敗バッチのエラー文を集める（`errors`）。
- `persona_sim/panel/screening.py` `_infer_execution`: `aborted_reason` に先頭のエラー文を
  含める。従来の「エンドポイントの設定を確認すること」だけでは何を直せばよいか分からない。

### 4. テスト

- `tests/test_infer.py`: 失敗バッチが `codes` に載らないこと、`judge_error` が立ち
  `parse_error` は立たないこと、エラー文が `answer_raw` に残ること。
- `tests/test_screening_spark.py`: 判定全滅でパネルを確定させないこと、
  **判定に失敗した候補が再実行で聞き直されること**（本 issue の回帰テスト）。
- `tests/test_run_survey_notebook.py`: screen セルが `aborted_reason` を見て raise すること。

## 手を入れない

- `screen_survey` の戻り値を例外に変えることはしない。CLI は集計表を出してから終了コード1を
  返す設計（`persona_sim/cli.py`）で、そこは正しく動いている。
- 判定が成功したのに番号が1つも取れなかった場合（「該当者なし」と出力不能の区別）は
  今回の経路では起きていないため、論点として issue に残す。

## 検証

- `./scripts/run-tests.sh`（非 Spark）
- `pytest -m spark`（`tests/test_screening_spark.py` を含む全件）
- 新しい回帰テストが**修正を戻すと失敗する**ことを確認する
