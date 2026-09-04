# `examples/survey_sample.yaml` にスクリーナーを追加 / `ui_config.yaml` に定性フィールド一覧コメントを追記 — 結果

## 変更内容

### `examples/survey_sample.yaml`

- `panel:` と `stimuli:` の間に `screening:` ブロックを追加した。`model` / `prompt` /
  `persona_card` / `mode` / `oversample_factor` / `logic` / `batch_size` は
  `app/config/ui_config.yaml` の `survey_defaults.screening` と同一内容
  （`mode: "infer"`）。`conditions` はこのサンプルの題材（RTD アルコール飲料の
  コンセプト調査）に合わせ `"缶チューハイ・缶ハイボールを月1回以上飲む"` とした
  （`examples/survey_sample_smoke.yaml` と同じ文言を流用）。
- 冒頭コメントを更新し、`screening:` も `main_survey:` と同様に `ui_config.yaml` の
  内容を書き写したものであること、`conditions` だけはこのサンプル固有の具体例で
  あることを明記した。

### `app/config/ui_config.yaml`

- `main_survey.persona_card.persona_fields` の直前に、`personas_base`
  （`SPEC_PHASE1.md` §2.1）で選べる定性（ナラティブ）フィールドを全9項目列挙する
  コメントを追加した（`cultural_background` / `professional_persona` /
  `sports_persona` / `arts_persona` / `travel_persona` / `culinary_persona` /
  `skills_and_expertise` / `hobbies_and_interests` / `career_goals_and_ambitions`）。
  既定で選んでいる4項目には「既定で選択中」と注記した。設定の実挙動は変えていない
  （コメントのみの追加）。

## 検証結果

- `./scripts/run-tests.sh`: 非 Spark 429件全件パス（既存 `test_sample_yaml_is_valid` /
  `test_smoke_sample_yaml_is_valid` / `tests/test_uiconfig.py` を含む）。
- 変更ファイルは `examples/survey_sample.yaml` と `app/config/ui_config.yaml` の2つのみ。
  スキーマ・パーサ・アプリケーションコードへの変更なし。

## 未対応事項

- 特になし。`conditions` の文言（対象カテゴリの飲用頻度条件）が実際の調査意図と
  異なる場合は、このサンプルを使う際に書き換えること。
