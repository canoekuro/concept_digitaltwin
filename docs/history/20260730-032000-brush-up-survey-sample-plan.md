# `examples/survey_sample.yaml` を結合済みフォーマットにブラッシュアップ（計画）

## 背景

`docs/issues/202607301208.md` の起票内容は「survey volume に置く調査 yaml と
`config/ui_config.yaml` の2つが notebook への入力になっており、`model` などが重複設定に
なっている。UI 上の設定と `ui_config.yaml` を結合したすべての情報入りの yaml を生成し、
それを survey volume に格納するべき」というもの。

調査の結果、UI から投稿する本番経路ではこれはすでに解消済みだった。

- `persona_sim/uiconfig/build.py` の `build_survey_dict()` が `SurveyForm`（画面入力）と
  `UIConfig`（`config/ui_config.yaml`）を `model` / `prompt` も含めて結合し、
  `survey_from_dict()` を通した検証済み1本の dict を作っている
  （`docs/SPEC_UI.md` §2「設定は2層に分ける」）。
- `persona_sim/uiconfig/jobs.py` の `upload_survey()` が、その結合済み yaml 1本だけを
  survey volume に保存する。
- `notebooks/run_survey.ipynb` は 2026-07-30 の `run_survey-standalone` 改修
  （`docs/history/20260730-010000-run-survey-standalone-*.md`）以降、`survey` ウィジェット
  1つだけを読む単一入力になっており、`config/ui_config.yaml` を読むコードは無い。

ユーザーに範囲を確認したところ、対象は UI 経由フローではなく、UI を経由しない手動運用、
具体的には `examples/survey_sample.yaml`（アプリを介さず notebook を単体実行する際の
調査定義サンプル）だと判明した。このファイルは `model:` を手書きで独立して持ち、
`config/ui_config.yaml` の `model:` と本来同じ値であるべきところが構造的に二重管理に
なっている（`max_tokens_open: 512` が漏れていた）。`prompt:` に至っては丸ごと省略されており、
`persona_sim/panel/schema.py` の `PromptConfig` の組み込みデフォルトに暗黙で依存していた
（現状はこのデフォルトが `config/ui_config.yaml` の `prompt:` と同一内容なので壊れて
見えないが、どちらか一方だけ編集すれば静かに乖離する）。

## 変更内容

1. `examples/survey_sample.yaml`
   - `model:` に `config/ui_config.yaml` の内容を過不足なく反映（`max_tokens_open: 512` 追加）。
   - `prompt:` を新設し、`config/ui_config.yaml` の `system` / `infer_system` /
     `persona_fields` / `rules` をそのまま書き写す。
   - 冒頭コメントに、`model:` / `prompt:` は `config/ui_config.yaml` を書き写したもの
     （UI 経由なら `build_survey_dict()` が自動生成する内容と同等）であり、
     `ui_config.yaml` 変更時はこのサンプルも見直す旨を追記。
2. `examples/survey_sample_smoke.yaml`
   - `model:` はスモークテスト専用の値のまま変更しない。
   - `prompt:` を意図的に省略している理由（`fake` エンドポイントは LLM を呼ばずプロンプトが
     結果に影響しない）を一言コメントで明示。
3. テストは既存の `tests/test_survey_loader.py::test_sample_yaml_is_valid` /
   `test_smoke_sample_yaml_is_valid` が値をハードアサートしていないため追加不要。
   既存テストが壊れないことのみ確認する。

## 検証方法

- `python -c "import yaml; yaml.safe_load(open('examples/survey_sample.yaml'))"`
- `./scripts/run-tests.sh tests/test_survey_loader.py -v`
- `config/ui_config.yaml` と `examples/survey_sample.yaml` の `model:` / `prompt:` を
  目視で突き合わせる。
