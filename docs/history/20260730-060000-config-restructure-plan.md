# 設定の再編成（`ui_config.yaml` と調査定義）— 計画

## 背景

`docs/issues/202607301208.md` に追加された4件に対応する。元の1件（結合済み yaml を
survey ボリュームに置く）は PR #13 で完了済み（「※対応済み」と追記されている）。

追加4件はいずれも**設定ファイルの構造が実態と噛み合っていない**ことに起因する。

1. `ui_config.yaml` に「調査のデフォルト値」と「画面としての設定」が混ざっている。
2. `output` は本当に必要か（結果は画面上で集計・閲覧するので）。
3. 本調査のペルソナカードが制御できない。スクリーニング側（`screener.infer`）には
   `include_attributes` / `include_summary` があるのに本調査側には無い。属性行も
   属性ごとに細かく制御したい。
4. `prompt:` にスクリーニングと本調査が混在している（`system` と `infer_system`）。
   全体を「スクリーニング→本調査」の流れに直し、それぞれ model → prompt →
   ペルソナカード → その他の順にしたい。**`ui_config.yaml` だけでなく survey
   ボリュームに置かれる調査定義も同様に。**

4番目は調査定義スキーマの破壊的変更で、`survey.model` / `survey.prompt` を参照する
約10モジュール、`survey.panel.screener` の約20箇所、仕様書3本、全 example yaml、
quickstart notebook、テスト約10ファイルが連動する。

## 起票者との確認で決まったこと

- **4件すべてを1つの PR で仕上げる。**
- **セクション名**: 識別情報の `survey: {id, name, type}` は残し、最上位に `screening:` と
  `main_survey:` を新設。最上位 `model:` / `prompt:` は `main_survey:` 配下へ、
  `prompt.infer_system` は `screening.prompt.system` へ。
- **`output`**: `base_segments` は残す（画面の集計軸そのもの。`aggregate.py` の集計軸と
  `storage/warehouse.py` が SELECT する列を決めており、外すと画面から性年代別・
  `cell_id` 別の内訳が消える）。`formats` は **`ui_config.yaml` からだけ**外す
  （結果画面は SQL Warehouse を直接引き、ダウンロード用 xlsx/csv はその場で再生成して
  いるため、ジョブが書き出したファイルは誰も読んでいない）。調査定義のスキーマとしては
  `formats` を残す。
- **後方互換**: 黙って無視せず移動先を添えたエラーで停止（既存踏襲）。

## 計画から外れた判断（実装時に決めたこと）

**`include_attributes` を廃止し `attributes:` リストに置き換える。** 要望は「本調査でも
`include_attributes` を選べるように」＋「属性行を属性ごとに制御したい」だが、属性ごとの
リストは真偽値を包含する（`attributes: []` = 属性行なし）。両方持つと
`include_attributes: false` と非空の `attributes:` が矛盾しうるため、リスト1本に寄せた。
`include_summary` は対応するリストが無いので真偽値のまま残す。スクリーニング側の既存
`include_attributes` も同時に廃止し、移行ヒントを出す。

**`prompt.rules.infer` も移動対象に追加した（起票者の指摘で判明）。** 現在の `PromptRules`
は設問タイプごとの回答指示文（`for_type()` が引く）と、スクリーニング判定の指示行 `infer`
（`panel/infer.py` だけが引く）を1つの block に持っていた。dataclass のコメント自身が
「設問タイプではないので `for_type` からは引かない」と書いており、`system` /
`infer_system` とまったく同型の混在。`screening.prompt.rule` へ移す。

**`headings` は `main_survey.prompt` に残す。** `ask_premise` / `assume_premise` /
`infer_premise` はスクリーニング由来の名前だが、描画されるのは本調査のペルソナカード
末尾の前提ブロック（§6.1）。スクリーニング側へは動かさない。

## 変更対象

### 調査定義（`persona_sim/panel/`）

- `schema.py`: `PersonaAttribute` / `DEFAULT_PERSONA_ATTRIBUTES` / `PersonaCardConfig`
  （`column_names()` 付き）を新設。`PromptRules` から `infer` を外し `DEFAULT_INFER_RULE`
  へ。`ScreeningPromptConfig`（`system` / `rule`）を新設。`Screener` + `InferConfig` を
  `ScreeningConfig` に統合（並びは model → prompt → persona_card → その他）。
  `PanelConfig` から `screener` を外し、`SurveyDefinition` に `persona_card` と
  `screening` を追加。移行ヒント（`RENAMED_TOP_LEVEL_KEYS` に `model` / `prompt`、
  `MOVED_PANEL_FIELDS` / `MOVED_PROMPT_RULE_FIELDS` / `REMOVED_PERSONA_CARD_FIELDS` を新設、
  `REMOVED_PROMPT_FIELDS` に `persona_fields` / `infer_system`）。
- `loader.py`: 最上位 allow-list を差し替え、`_screening()` / `_screening_prompt()` /
  `_persona_card()` / `_persona_attribute()` / `_persona_field()` を新設。`_model()` /
  `_prompt()` はパスを引数化してエラーメッセージが `main_survey.*` を指すようにする。
- `validate.py`: `_check_persona_card()` を新設し、本調査・スクリーニング両方の
  `attributes` / `persona_fields` を `PERSONAS_BASE_COLUMNS` に対して検証。パス文字列を
  新構造に合わせる。

### プロンプト組み立て

- `run/prompt.py`: `attribute_row()` を新設（純関数）。`persona_card()` は
  `PersonaCardConfig` を受け取る形にし、総括が空なら行ごと落とす（従来は空行が残っていた）。
  `build_user_message()` / `initial_messages()` に `card_config` を通す。
- `panel/infer.py`: `persona_card_for_judge()` が `PersonaCardConfig` を受け取り、
  属性行は `attribute_row()` を使う。**整形は共用しない**（判定側は1人1行に詰める。
  費用面の利点を保つため）。`build_judge_messages()` / `judge_batch()` / `run_inference()` /
  `infer_session_count()` は `ScreeningConfig` 1つで足りるので冗長な引数を落とす。
- `personas/load.py`: `required_persona_columns()` を新設。属性行が設定可能になったため
  `FIXED_PERSONA_COLUMNS` だけでは足りない（`region` などを指定しても列が来ず黙って空になる）。
- `metadata.py`: `_persona_card_summary()` を新設し、本調査・判定の両方でカードの内容を
  実行メタデータに残す（§9.1 の再現性）。

### UI 設定（`persona_sim/uiconfig/`, `app/`）

- `schema.py`: `ScreenerDefaults` を `ScreeningDefaults`（model / prompt / persona_card /
  batch_size 付き）に、`MainSurveyDefaults` を新設。`UIConfig` から `model` / `prompt` /
  `formats` を外す。
- `loader.py`: 最上位を `survey_defaults` / `ui` の2群に。旧フラット構造は移行先を添えて停止。
  死んだ設定 `screener.available_persona_fields`（読み手が1箇所も無い）を削除。
- `build.py`: `build_screener()` → `build_screening()`。`main_survey` block を組み立て、
  `output.formats` は書かない。`infer` 以外では判定用の設定を書かない。
- `app/config/ui_config.yaml`: 全面的に書き換え。
- `app/views/survey_design.py`: `ui.model` → `ui.main_survey.model`、
  `ui.screener` → `ui.screening`。

### サンプル・ノートブック・仕様書

- `examples/survey_sample.yaml` / `examples/survey_sample_smoke.yaml`
- `notebooks/quickstart.ipynb`（実行セルとコメントアウトされた本番規模セルの両方）
- `SPEC.md` §3 / §4.2 / §6.1 / §10.3、`docs/SPEC_UI.md` §2 / §3.2 / §3.4、
  `README.md`、`deploy/README.md`

## 検証方法

1. `./scripts/run-tests.sh`（非 Spark 全件）。
2. Spark テスト（`-m spark`）。ローカルに pyspark が無いので `--with pyspark` で入れて実行し、
   走らなければ CI に委ねる。
3. 両サンプル yaml が `load_survey()` → `validate_static()` を通ること。
4. **旧形式が黙って通らないこと**をテストで固定する。移行ヒントの各パターンに1件ずつ。
5. notebook の JSON 健全性（`python -m json.tool`）。
6. Streamlit アプリを起動し、調査設計ページと「組み立てた調査定義を見る」プレビューを確認。
