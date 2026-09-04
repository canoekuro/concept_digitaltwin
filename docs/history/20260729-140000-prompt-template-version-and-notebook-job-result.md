# `template_version` 廃止 と ジョブ実行の notebook 化 結果

- **日付**: 2026/07/29 14:00
- **テーマ**: `prompt-template-version-and-notebook-job`

## 実施内容

### 1. `template_version` の廃止

- `persona_sim/panel/schema.py`: `PromptConfig.template_version` を削除。`REMOVED_MODEL_FIELDS`
  と同じ形で `REMOVED_PROMPT_FIELDS = {"template_version": "..."}` を新設。
- `persona_sim/panel/loader.py`: `_prompt()` の冒頭で `REMOVED_PROMPT_FIELDS` を検出し、
  廃止理由つきの `SurveyDefinitionError` で停止するようにした。
- `persona_sim/panel/validate.py`: `W_PROMPT_TEMPLATE_VERSION` 警告ブロックと、それ専用の
  `_raw_prompt_keys()` ヘルパを削除。上書き自体は警告なく通ることを確認するテストに
  差し替えた。
- `persona_sim/metadata.py`: `build_metadata()` の `prompt_template_version`、
  `_prompt_sample()` の `template_version`、`render_prompt_sample()` の表の該当行を削除。
- `config/ui_config.yaml`: `prompt.template_version: "ui-v1"` と、版据え置き警告を避けるための
  付随コメント3行を削除。
- `SPEC_PHASE1.md`: §3 のスキーマ例、§6.1 の2つの YAML 例（`persona_fields` 上書き例／
  `system` 上書き例）から該当行を削除。§6.1 の版管理の説明を「廃止した。理由: 生成AIの回答に
  再現性が無く、版による比較が成立しにくいこと。利用者がプロンプトを自由に書き換える前提
  であること」に置き換え。§9 の `run_metadata.json` 例から `prompt_template_version` と
  `prompt_sample.template_version` を削除。
- `notebooks/quickstart.ipynb`: 調査定義を組み立てる本体セルと、コメントアウトされた
  本番規模の参考セルの両方から `template_version` の指定とその説明コメントを削除。
  実行メタデータを表示するセルから `metadata["prompt_template_version"]` の print 行を削除。
- テスト:
  - `tests/test_survey_loader.py`: `test_prompt_overrides_are_loaded` から `template_version`
    の入出力を除去し、新規 `test_removed_prompt_template_version_is_rejected_with_migration_hint`
    を追加（`prompt.template_version` を含む「廃止」理由つきエラーで停止することを確認）。
  - `tests/test_validate.py`: `W_PROMPT_TEMPLATE_VERSION` に依存していた3テストを、
    版管理を前提にしない `test_prompt_override_validates_ok`（上書きが警告なく通ることの
    確認）に整理した。
  - `tests/test_run_spark.py`: `test_run_metadata_is_written` / `test_prompt_sample_is_written_as_markdown`
    / `test_prompt_sample_reflects_prompt_overrides` から `template_version` 関連の
    入力・アサーションを削除。

### 2. ジョブ実行の notebook 化

- `persona_sim/uiconfig/jobs.py::run()` は `run_now(job_parameters=...)` を呼ぶだけで
  タスク種別に依存しないことを確認し、**無変更**とした。
- `notebooks/run_survey.ipynb`（新規）: `dbutils.widgets` で `survey`（Volumes 上の調査定義
  パス）を受け取り、`load_survey` → `validate_static`（エラーがあれば例外を送出してジョブを
  失敗させる）→ `build_panel` → `screen_survey` → `run_survey`（セッション失敗があれば例外を
  送出）→ `aggregate_survey`（`output.formats` に従い export も兼ねる）→ `write_metadata` を
  順に実行する。`quickstart.ipynb` と違い、`personas_base` 構築やデモ用 `fake` エンドポイント
  への切り替えは含めない。
- `deploy/README.md` §3: `python_wheel_task` の6タスクチェーンから、単一の `notebook_task`
  （`notebooks/run_survey.ipynb`、`base_parameters` で `survey` を渡す）に書き換え。
  全体像の図も notebook_task 1本の構成に更新。
- `docs/SPEC_UI.md` §6.1: 「`panel` → `screen` → `run` → `aggregate` → `export` を順に
  実行させる」という複数タスクの説明を、単一 notebook_task 呼び出しの説明に更新。

## 検証結果

- `./scripts/run-tests.sh`（非Spark）: 391 passed, 63 deselected
- `-m spark`: 63 passed（`test_run_metadata_is_written` / `test_prompt_sample_*` を含む）
- `notebooks/run_survey.ipynb`: `json.tool` で読めることを確認。コードセル（`dbutils` を
  含むセルを除く）はすべて `ast.parse` を通過
- `grep -rn template_version`: 残るのは廃止理由の文言、過去の `docs/history/` 記録、
  `CHANGELOG.md` の過去エントリ、`docs/issues/202607291302.md`、および新設テストのみ

## 差分の要点（レビュー観点）

- `template_version` の廃止は**破壊的変更**（既存の調査定義に `prompt.template_version` が
  書かれているとエラーで停止する）。既存 Delta テーブルの作り直しは不要。
- ジョブ側の notebook 化はアプリ側コード（`persona_sim/uiconfig/jobs.py`）に変更が無い。
  実環境でのジョブ再構成（`python_wheel_task` → `notebook_task` への切り替え）は運用側の
  対応が要る（`deploy/README.md` 参照）。実環境での動作確認は未実施。
