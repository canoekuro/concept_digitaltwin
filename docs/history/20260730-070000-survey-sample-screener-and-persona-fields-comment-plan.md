# `examples/survey_sample.yaml` にスクリーナーを追加 / `ui_config.yaml` に定性フィールド一覧コメントを追記 — 計画

## 背景

利用者から2件の指摘・要望があった。

1. `examples/survey_sample.yaml` に `screening:` が無い。`examples/survey_sample_smoke.yaml`
   （`mode: assume`）や `app/config/ui_config.yaml`（`mode: infer`）には既にあり、
   本番規模サンプルだけがスクリーニングなしの調査定義になっている。
2. `main_survey.persona_card.persona_fields` に選べる定性（ナラティブ）フィールドの
   一覧が設定ファイル上に見当たらない。今後見直すときに `personas_base` の列一覧
   （`SPEC_PHASE1.md` §2.1）まで遡らないと選択肢が分からない。

## 対応方針

### 1. `examples/survey_sample.yaml` に `screening:` を追加

- ファイル冒頭のコメントで宣言済みの方針どおり、`screening.model` / `.prompt` /
  `.persona_card` は `app/config/ui_config.yaml` の `survey_defaults.screening` を
  そのまま書き写す（`mode: infer`）。
- `conditions` だけは UI では画面入力のため `ui_config.yaml` に実体が無い。このサンプルの
  題材（RTD アルコール飲料のコンセプト調査）に合わせ、`examples/survey_sample_smoke.yaml`
  で使われている条件文 `"缶チューハイ・缶ハイボールを月1回以上飲む"` を流用する。
- 置き場所は `panel:` の直後・`stimuli:` の前（`survey_sample_smoke.yaml` と同じ並び、
  `SPEC_PHASE1.md` のスキーマ順 `survey → panel → screening → stimuli → design →
  questions → main_survey → output` にも合う）。
- 冒頭コメントに「`screening:` も `ui_config.yaml` 由来だが `conditions` だけこのサンプル
  固有」と追記する。

### 2. `app/config/ui_config.yaml` に定性フィールドの一覧をコメントで残す

- `main_survey.persona_card.persona_fields` の直前に、`personas_base`
  （`SPEC_PHASE1.md` §2.1）にあるナラティブ列を全て列挙するコメントを追加する。
  既定で選択中の4項目にはその旨を注記する。
- 設定の挙動は変えない（コメントのみ）。過去の履歴（#18 `config-restructure`）で
  読み手の無い `screener.available_persona_fields` を実フィールドとして削除した
  経緯があるため、今回は実行時に読まれるフィールドとしては追加せず、あくまで
  人間が見直すためのコメントに留める。

## 検証方法

- `./scripts/run-tests.sh`（非 Spark 全件）で `test_sample_yaml_is_valid` を含む既存
  テストが崩れないこと。
- 変更が `examples/survey_sample.yaml` / `app/config/ui_config.yaml` のみで、
  スキーマ・パーサ・アプリケーションコードには手を入れないこと。

## 記録

コード・スキーマの変更は伴わないが、`config/ui_config.yaml` は「重要な設定ファイル」に
該当するため `docs/history/` に plan/result を残し、`CHANGELOG.md` に追記する。
