# `template_version` 廃止 と ジョブ実行の notebook 化 計画

- **日付**: 2026/07/29 14:00
- **テーマ**: `prompt-template-version-and-notebook-job`

## 経緯

`docs/issues/202607291302.md` に2件のフォローアップが追加された（PR #9 コンセプト調査
Web UI マージ後の確認を経て）。

1. 生成AIの回答には再現性が無く、`template_version` による版管理という前提自体が
   実態に合わない。かつ利用者がプロンプトを自由に書き換える運用を想定しているため、
   `template_version` による管理を廃止する。
2. アプリ構成（UI側で調査定義 → ジョブ側で実行 → UI側で確認）のうち、ジョブ側の実行を
   notebook 化し、調査定義が Volumes に格納されたらそれをキック＋入力として動くようにする。

## 1. `template_version` の廃止

### 方針

既存の「廃止フィールド」パターン（`REMOVED_MODEL_FIELDS` / `REMOVED_DESIGN_FIELDS`）を
踏襲する。黙って無視すると「版管理をしているつもり」の記録が残ってしまうため、
`REMOVED_PROMPT_FIELDS` を新設し、廃止理由つきのエラーで読み込みを止める。

### 対象ファイル

- `persona_sim/panel/schema.py`: `PromptConfig.template_version` を削除。
  `REMOVED_PROMPT_FIELDS` を新設。
- `persona_sim/panel/loader.py`: `_prompt()` で廃止フィールドを検出して停止。
- `persona_sim/panel/validate.py`: `W_PROMPT_TEMPLATE_VERSION` 警告と、それ専用だった
  `_raw_prompt_keys()` ヘルパを削除。
- `persona_sim/metadata.py`: `prompt_template_version` / `prompt_sample.template_version` /
  `prompt_sample.md` の該当行を削除。
- `config/ui_config.yaml`: `prompt.template_version` と、警告回避のための付随コメントを削除。
- `SPEC.md`: §3・§6.1（YAML例2箇所と説明文）・§9 の `run_metadata.json` 例を更新。
- `notebooks/quickstart.ipynb`: `template_version` を使うセル3箇所を修正。
- `tests/test_survey_loader.py` / `tests/test_validate.py` / `tests/test_run_spark.py`:
  `template_version` を前提にしたテストを削除・置き換え、廃止フィールドの回帰テストを追加。

## 2. ジョブ実行の notebook 化

### 現状の確認

`persona_sim/uiconfig/jobs.py::run()` は `client.jobs.run_now(job_id=..., job_parameters=...)`
を呼ぶだけで、ジョブのタスク種別（`python_wheel_task` / `notebook_task`）に依存しない。
したがって **このモジュールは変更不要**。

### 方針

`validate → panel → screen → run → aggregate`（`aggregate_survey()` が export も兼ねる）を
1本の notebook にまとめ、ジョブを単一の `notebook_task` として登録する。
途中で失敗したら例外を送出してジョブを失敗させる（黙って先へ進めると、実行できていないのに
`runs` に記録が残ったかのように見えてしまうため）。

### 対象ファイル

- `notebooks/run_survey.ipynb`（新規）: `dbutils.widgets.get("survey")` で Volumes 上の
  調査定義パスを受け取り、一連の処理を実行する本番実行用ノートブック。
  `personas_base` 構築やデモ用 `fake` エンドポイントへの切り替えは含めない。
- `deploy/README.md` §3: `notebook_task` 1本の構成に書き換え。
- `docs/SPEC_UI.md` §6.1: 単一 notebook 呼び出しの説明に更新。

## 検証方法

1. `./scripts/run-tests.sh`（非Spark）が全件パスすること。
2. `-m spark` のテストが全件パスすること。
3. `grep -rn template_version` で、残るのが廃止理由の文言・過去記録・issue ファイルのみであること。
4. `notebooks/run_survey.ipynb` が `json.tool` で読め、コードセルが `ast.parse` を通ること。
