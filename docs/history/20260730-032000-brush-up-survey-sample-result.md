# `examples/survey_sample.yaml` を結合済みフォーマットにブラッシュアップ（結果）

計画（`20260730-032000-brush-up-survey-sample-plan.md`）どおりに実施した。

## 実施内容

1. **`examples/survey_sample.yaml`**
   - 冒頭コメントに、`model:` / `prompt:` は `config/ui_config.yaml` の内容を書き写した
     ものであり（UI 経由なら `build_survey_dict()` が自動生成する内容と同等）、
     `config/ui_config.yaml` を変更したらこのサンプルも見直すことを追記した。
   - `model:` に `max_tokens_open: 512` を追加し、`config/ui_config.yaml` の `model:` と
     完全一致させた。
   - `prompt:` ブロック（`system` / `infer_system` / `persona_fields` / `rules`）を新設し、
     `config/ui_config.yaml` の内容をそのまま書き写した。それまでは省略されており、
     `persona_sim/panel/schema.py` の `PromptConfig` 組み込みデフォルトに暗黙で依存していた
     （偶然 `config/ui_config.yaml` と同一内容だったため症状は出ていなかった）。
2. **`examples/survey_sample_smoke.yaml`**
   - `model:` は smoke test 専用の値のまま変更せず。冒頭コメントに、`prompt:` を意図的に
     省略している理由（`fake` エンドポイントは LLM を呼ばずプロンプトが実行結果に影響しない
     ため）と `survey_sample.yaml` とは方針が異なる旨を追記した。

## 検証結果

- `python -c "import yaml; yaml.safe_load(open('examples/survey_sample.yaml'))"` →
  パース成功。`model` に `max_tokens_open: 512` を含む全キーが揃い、`prompt` に
  `system` / `infer_system` / `persona_fields` / `rules` の4キーが揃っていることを確認。
- `./scripts/run-tests.sh tests/test_survey_loader.py -v` → 37件全件 PASSED
  （`test_sample_yaml_is_valid` / `test_smoke_sample_yaml_is_valid` を含む）。
- `config/ui_config.yaml` の `model:` / `prompt:`（`system`/`infer_system`/`persona_fields`/
  `rules`）と `examples/survey_sample.yaml` の該当ブロックを目視で突き合わせ、一致を確認した。

## 未対応・引き継ぎ事項

- `notebooks/quickstart.ipynb`（cell-14）にも `config/ui_config.yaml` の `model:` を模した
  インライン `survey_dict` があるが、今回のスコープ外（issue 起票者が対象と確認したのは
  `examples/survey_sample.yaml` のみ）として未対応。将来同様の乖離が問題になった場合は
  別タスクとして扱う。
