# 設定の再編成（`ui_config.yaml` と調査定義）— 結果

計画（`20260730-060000-config-restructure-plan.md`）どおりに実施した。
`docs/issues/202607301208.md` の追加4件すべてに対応している。

## 実施内容

### 1. 調査定義スキーマの再編成（破壊的変更）

最上位を `survey` / `panel` / `screening` / `main_survey` / `stimuli` / `design` /
`questions` / `output` の8つにした。旧 `model:` / `prompt:` は `main_survey:` 配下へ、
旧 `panel.screener:` は最上位 `screening:` へ。両ブロックの並びは要望どおり
model → prompt → persona_card → その他。

`persona_sim/panel/schema.py`:

- `PersonaAttribute`（`field` / `suffix`）と `DEFAULT_PERSONA_ATTRIBUTES`（従来ハード
  コードしていた6項目）を新設。
- `PersonaCardConfig`（`attributes` / `include_summary` / `persona_fields`）を新設。
  `column_names()` を持たせ、カードを描くのに要る列を自分で答えられるようにした。
- `PromptRules` から `infer` を外し、`DEFAULT_INFER_RULE` として切り出した。
- `ScreeningPromptConfig`（`system` / `rule`）を新設。
- `Screener` + `InferConfig` を `ScreeningConfig` に統合。`infer.*` の入れ子が消え、
  `batch_size` / `model` / `persona_card` が直下に並ぶ。
- `PanelConfig` から `screener` を外し、`SurveyDefinition` に `persona_card` と
  `screening` を追加。

`persona_sim/panel/loader.py`: `_screening()` / `_screening_prompt()` / `_persona_card()` /
`_persona_attribute()` / `_persona_field()` を新設。`_model()` / `_prompt()` はパスを
引数化し、エラーメッセージが `main_survey.model` / `main_survey.prompt` を指すようにした。

**内部の属性名について**: `survey.model` / `survey.prompt.system|headings|rules` は
現状の名前を維持した（`SurveyDefinition.model` は自然に「この調査のモデル」と読めるため）。
実際に改名したのは `survey.panel.screener` → `survey.screening`、
`survey.prompt.persona_fields` → `survey.persona_card.persona_fields`、
`survey.prompt.infer_system` → `survey.screening.prompt.system` に限られる。

### 2. 移行ヒント（旧形式は黙って通さない）

既存の仕組み（`RENAMED_TOP_LEVEL_KEYS` / `REMOVED_*_FIELDS`）を踏襲した。

| 旧キー | 行き先 |
|---|---|
| 最上位 `model:` | `main_survey.model:` |
| 最上位 `prompt:` | `main_survey.prompt:`（`persona_fields` と `infer_system` の行き先も併記） |
| `panel.screener:` | `screening:`（`infer.*` の行き先も併記） |
| `prompt.persona_fields` | `main_survey.persona_card.persona_fields` |
| `prompt.infer_system` | `screening.prompt.system` |
| `prompt.rules.infer` | `screening.prompt.rule` |
| `include_attributes` | `persona_card.attributes`（従来の既定と同じ書き方も併記） |

`ui_config.yaml` 側も同様に、旧フラット構造（`model` / `prompt` / `default_questions` /
`screener` / `allocation` / `output` / `estimation_benchmarks`）を移行先つきで停止させる。

### 3. ペルソナカードの設定化（項目4）

- `run/prompt.py` に純関数 `attribute_row()` を新設し、`persona_card()` と
  `panel/infer.py` の `persona_card_for_judge()` の両方から呼ぶようにした。属性行の
  並びが2箇所にハードコードされていた状態を解消した。
- **整形は共用していない。** 判定カードは1人1行に詰める（`" / "` 連結）。
  `panel/infer.py` の docstring が明記しているとおり、回答生成用のカードを流用すると
  判定プロンプトが長くなり `infer` 方式の費用面の利点が消えるため。共通化したのは
  「どの属性をどの接尾辞で載せるか」の設定だけ。
- `include_attributes` は廃止し `attributes:` のリストに置き換えた（計画の「実装時に
  決めたこと」参照）。`include_summary` は真偽値のまま残した。
- **`FIXED_PERSONA_COLUMNS` の罠に対応した。** `personas/load.py` に
  `required_persona_columns()` を新設し、本調査・スクリーニング両方の `persona_card` が
  要求する列を必ず集めるようにした。これが無いと `region` や `employment_status` を
  属性行に入れてもエラーにならず、その項目が抜けたカードでモデルに聞いてしまう。
- `validate.py` に `_check_persona_card()` を新設し、`attributes` / `persona_fields` の
  `field` を `PERSONAS_BASE_COLUMNS` に対して検証する。
- **既存の癖を1つ直した。** `persona_card()` は総括が空でも空行を出していた
  （`str(persona.get("persona") or "")`）。`include_summary` を導入したので行ごと落とす
  ようにした。空行だけが残ると、何かを載せ忘れたようにプロンプトが読める。
- 再現性（§9.1）のため `metadata.py` に `_persona_card_summary()` を新設し、本調査・判定の
  両方でカードの実際の内容（属性の並び・総括の有無・ナラティブ列）を実行メタデータに残す。

### 4. `prompt.rules.infer` の移動（起票者の指摘で追加）

`PromptRules` は設問タイプごとの回答指示文（`for_type()` が引く）とスクリーニング判定の
指示行 `infer`（`panel/infer.py` だけが引く）を同じ block に持っていた。dataclass の
コメント自身が「設問タイプではないので `for_type` からは引かない」と書いており、
`system` / `infer_system` とまったく同型の混在だったため `screening.prompt.rule` へ移した。

`headings` は `main_survey.prompt` に残した。`ask_premise` / `assume_premise` /
`infer_premise` はスクリーニング由来の名前だが、描画されるのは本調査のペルソナカード
末尾の前提ブロックであるため（`SPEC_PHASE1.md` §6.1）。

### 5. `ui_config.yaml` の2群化（項目2・3）

最上位を `survey_defaults`（そのまま調査定義になるもの）と `ui`（画面を描くためだけの
もの）の2つだけにした。

- `survey_defaults`: `survey_type` / `screening` / `main_survey` / `questions` /
  `output.base_segments`
- `ui`: `allocation_patterns` / `estimation_benchmarks`

**起票者の分類と変えた点（根拠つき）**: `default_questions` は「画面の初期値」だが編集後の
設問がそのまま調査定義の `questions:` になるので `survey_defaults` 側に置いた
（`questions` に改名）。`survey_type` も調査定義に入る（かつ結果画面の実行一覧の
絞り込みにも使う）ので同様。`output.base_segments` も調査定義に入るので同様。

**`output.formats` は `ui_config.yaml` から外した（項目3）。** 結果画面は SQL Warehouse を
直接引いて表を描き、Excel / CSV のダウンロードは押されたその場で `tempfile` に生成する
（`app/views/results.py`）。ジョブが `output_dir()` に書き出したファイルは誰も読んでいない。
`base_segments` は画面の集計軸そのもの（`aggregate.py` の集計軸と `storage/warehouse.py` が
SELECT する列を決める）なので残した。調査定義のスキーマとしては `formats` を残してあり、
CLI やノートブックからは従来どおり指定できる。

**死んだ設定を1つ削除した。** `screener.available_persona_fields` は読み手が1箇所も無く
（loader と schema で読むだけで、どの画面もどのビルドも参照していなかった）、
「画面の選択肢」と称しながら何も選ばせていなかったため削除した。

`build.py` の `build_screener()` は `build_screening()` に改称。`mode` が `infer` 以外の
ときは判定用の `model` / `prompt` / `persona_card` / `oversample_factor` / `batch_size` を
書かないようにした（判定の LLM を呼ばないので、残すと「設定したつもり」の記録になる）。

### 6. サンプル・ノートブック・仕様書

- `examples/survey_sample.yaml`: 新構造へ。`main_survey.persona_card.attributes` を
  明示的に6項目書き出し、属性行が設定可能になったことがサンプルから読めるようにした。
- `examples/survey_sample_smoke.yaml`: `screening:` を最上位へ、`model:` を
  `main_survey.model` へ。`prompt` / `persona_card` を省略している理由（`fake`
  エンドポイントは LLM を呼ばない）をコメントで明示。
- `notebooks/quickstart.ipynb`: 実行セルとコメントアウトされた本番規模セルの両方を新構造へ。
- `SPEC_PHASE1.md` §3（調査定義スキーマ）・§4.2（`infer` の例と、判定プロンプトを本調査と
  分ける理由）・§6.1（**「1行目の属性行と `persona`（総括）は固定」が偽になったので書き換え**。
  3ブロックすべて設定可能である旨と `suffix` の意味を追記）・§10.3。
- `docs/SPEC_UI.md` §2（2層の表 ＋ 「`ui_config.yaml` 自身も2群に分かれる」節と
  「出力形式は設定に持たない」節を新設）・§3.2・§3.4。
- `README.md`（`panel.screener.mode` → `screening.mode` など）、`deploy/README.md`。

## 検証結果

- `./scripts/run-tests.sh` → **429件全件 PASSED**（非 Spark。変更前の基準403件に対し
  新規26件）。
- Spark テスト（`-m spark`）→ **63件全件 PASSED**（9分22秒）。この環境には pyspark が
  入っていないため（`pyproject.toml` の `spark` extra）、
  `uv run --with pyspark==4.0.1 --with delta-spark==4.3.1 pytest -m spark` で導入して実行した。
  変更の影響が大きい `tests/test_screening_spark.py`（`infer` の判定・前提ブロック・
  メタデータ）と `tests/test_run_spark.py`（`prompt_sample` が実際に送った内容と一致すること）を
  含む。
- `examples/survey_sample.yaml` / `examples/survey_sample_smoke.yaml` の両方が
  `load_survey()` → `validate_static()` を `ok == True`・警告なしで通ることを確認。
- **旧形式が黙って通らないことをテストで固定した。** 移行ヒントの表の各行に1件ずつ対応する
  テストを `tests/test_survey_loader.py` に追加（`include_attributes` は本調査・
  スクリーニングの両方で確認）。`ui_config.yaml` 側も旧フラットキー7種を
  `tests/test_uiconfig.py` で確認。
- `python -m json.tool notebooks/quickstart.ipynb` → JSON 健全。
  `tests/test_notebook.py`（notebook 内の調査定義が `survey_from_dict()` を通ること）も緑。
- 実際の `app/config/ui_config.yaml` を読み込んで `build_survey_dict()` を通し、
  生成される調査定義が新構造（`screening` / `main_survey` を持ち、各ブロックが
  model → prompt → persona_card → その他の順）になっていること、および
  `validate_static()` が警告なしで通ることを確認。
- **Streamlit アプリを実際に起動し、Playwright で調査設計ページを操作して確認した。**
  フォームを埋めた状態で例外・エラー表示が一切出ず、見積もり（総セッション数・有効サンプル数／案・
  入力トークン・想定所要時間）と「抽出倍率（現在 4 倍）」の注記が正しく描画される。
  この2つはそれぞれ `ui.main_survey.model["concurrency"]` と
  `ui.screening.oversample_factor` を読んでおり、`UIConfig` の新しい形が画面から
  引けていることの確認になる。「組み立てた調査定義を見る」の中身
  （`st.json(build_survey_dict(...))`）は expander の開閉によらず毎描画で評価されるため、
  合成が例外なく通っていることも同時に確認できている。

### 追加したテスト（主なもの）

- `tests/test_prompt.py`: 属性行の個別選択、既定6項目以外の列（`region`）を属性行に
  入れられること、属性行も総括も出さないときに空行が残らないこと、総括が空のときに
  行ごと落ちること、`attribute_row()` が判定カードと共用されていること。
- `tests/test_survey_loader.py`: 移行ヒント7パターン、新構造の往復。
- `tests/test_uiconfig.py`: 旧フラットキー7種の移行ヒント、2群に分かれていること、
  `formats` が消えていること、合成結果が新構造になっていること、`assume` では判定用の
  設定を書かないこと。
- `tests/test_validate.py`: `persona_card` の上書きが検証を通ること。

## 未対応・引き継ぎ事項

- **Databricks 実環境での動作確認は未実施**（本セッションに Databricks 環境が無い。
  `deploy/README.md` の既存の未検証注記と同様）。
- 「組み立てた調査定義を見る」の expander を**開いた状態**の JSON 表示は目視していない
  （Playwright から expander を開けなかった。ただし中身は毎描画で評価されており、
  合成が例外なく通っていることは上記のとおり確認済み）。
- 結果閲覧ページは未確認。SQL Warehouse への実接続が要るため、この環境では動かせない。
