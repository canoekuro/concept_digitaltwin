# 結果閲覧は実行記録を「集計に要る範囲だけ」で復元する（結果）

- 日時: 2026-09-02 08:34:42 UTC
- 計画: [20260902-083442-record-restore-plan.md](20260902-083442-record-restore-plan.md)

## 1. 直したもの

結果閲覧の「データを取得」が
`SurveyDefinitionError: main_survey.prompt.rules.reasoning: …` で落ちていた。原因は
`warehouse.fetch_survey()` が `runs.metadata_json` の定義を `survey_from_dict()` で
**丸ごと再パース**していたこと。8/19 に `prompt.rules.reasoning` を設問タイプごとの
入れ子にしたため、それ以前に実行した調査の記録が旧形式（1本の文字列）のまま残っており、
**集計に一切使われない文言キー**を理由に結果が読めなくなっていた。

書き込み側（これから実行する定義）の厳格さは正しい。誤っていたのは、その厳格さを
**実行済みの記録の読み直し**にも当てていたこと。記録は事実であって、書き直させる相手が
いない。

`persona_sim/panel/loader.py` に `survey_from_record()` を新設し、`fetch_survey()` を
そちらへ切り替えた。境界は「集計の数字を決めるものか」。

| block | 記録から読むか |
|---|---|
| `survey` / `panel` / `stimuli` / `questions` / `output` | **読む。`survey_from_dict()` と同じ厳格さ** |
| スクリーニングの方式（`mode`） | **読む**（実測 / 推定 / 未測定で通過率の意味が変わる） |
| `main_survey`（`model` / `prompt` / `persona_card`）・`design` | **読まない**（集計側に参照が無い） |
| 未知のキー・旧い書き方 | **無視する** |

これで、同じ形の地雷もまとめて外れた——7/28 に廃止した `model.temperature`、7/30 に
`main_survey:` 配下へ移した旧トップレベル `model:` / `prompt:`、`panel.screener`。
`panel.screener` については**方式だけそこから読む**ようにしたので（計画では未対応と
していた）、7/30 より前の記録でも通過率の注記が「未実施」に化けない。

読まなかった block は既定値のまま入る。復元した定義を実行や「何をどう聞いたか」の
根拠に使わせないため、`SurveyDefinition.from_record` を印として足し、読まない `model`
には `RECORD_MODEL`（`endpoint: fake` / `deployment: ""`）を入れた。定義の全文は
従来どおり `raw` に残る（記録は書き換えていない）。

## 2. 変更ファイル

| ファイル | 変更 |
|---|---|
| `persona_sim/panel/loader.py` | `survey_from_record()` / `_record_screening()` / `RECORD_MODEL` を追加。`_reject_unusable_questions()` の docstring を更新（記録の読み直しはここを通らない） |
| `persona_sim/panel/schema.py` | `SurveyDefinition.from_record` を追加 |
| `persona_sim/storage/warehouse.py` | `fetch_survey()` を `survey_from_record()` 経由に |
| `SPEC.md` §9 | 記録から読み直すときの範囲と、実行に使えないことを明記 |
| `docs/SPEC_UI.md` §4.3 | 同上（`survey_from_dict()` と書いていた箇所） |
| `tests/test_survey_loader.py` | 復元経路のテスト15件 |
| `tests/test_warehouse.py` | ダミー接続で `fetch_survey()` の経路を確認する2件 |

## 3. 検証

- `./scripts/run-tests.sh`: **969 passed**（変更前 952、17件追加）。
- `ruff check persona_sim app tests`: パス。
- 既存の失敗3件（`test_jtbd_definition::test_load_study_from_sample_yaml` /
  `test_jtbd_notebook` / `test_run_survey_notebook`）は**本変更の前から落ちており**、
  内容も無関係（JTBD の定義サンプルと notebook）。件数は増えていない。
- 旧形式（1本文字列の `reasoning` ＋ 廃止した `temperature` ＋ `screening.mode: infer`）の
  記録を組み立て、結果閲覧が通る経路（`build_result()` → `tabulated_measures()` →
  `concept_axis_table()` / `stacked_crosstab_table()` → `labelled_csv_rows()` →
  `target_summary()`）を端から端まで実行して、例外なく表が組み上がることを確認した。

## 4. 未対応事項

- **Databricks 実環境・Streamlit 画面での確認は未実施**（この環境に接続が無い）。実際に
  落ちていた調査で「データを取得」が通ることは、利用者側での確認が要る。
- 既に書かれた `runs.metadata_json` は書き換えていない。復元は読むたびに行う。
- 記録の読み直しでは未知のキーを弾かないので、**今後 `main_survey` 側に加える変更は
  結果閲覧では検知されない**。数字を決める block の厳格さを残すことで釣り合いを取っている。
- 復元した定義は集計専用。実行経路に渡せば既定の聞き方で走ってしまうため、`from_record`
  を見て止める仕掛けは**入れていない**（現在そこへ渡す経路が無い。増やすときに要検討）。
