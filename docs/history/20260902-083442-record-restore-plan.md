# 結果閲覧は実行記録を「集計に要る範囲だけ」で復元する（計画）

- 日時: 2026-09-02 08:34:42 UTC
- 対象: `persona_sim/panel/loader.py` / `persona_sim/panel/schema.py` /
  `persona_sim/storage/warehouse.py` / `SPEC.md` §9 / `docs/SPEC_UI.md` §4.3 /
  `tests/test_survey_loader.py` / `tests/test_warehouse.py`
- 起点: 結果閲覧ページで「データを取得」を押すと
  `SurveyDefinitionError: main_survey.prompt.rules.reasoning: prompt.rules.reasoning は
  設問タイプごとの入れ子にした（§6.3）…` で落ちる、という報告。

## 1. 原因

1. 「データを取得」は `warehouse.fetch_survey()` を呼ぶ（`app/views/results.py`）。
2. `fetch_survey()` は `runs.metadata_json` の `survey_definition`（実行時の定義の全文＝
   `SurveyDefinition.raw`）を `survey_from_dict()` で**丸ごと再パース**していた。
3. `prompt.rules.reasoning` は 8/19 の破壊的変更で「文字列なら読まずに停止」になった
   （`FLAT_REASONING_RULE`）。
4. `app/config/ui_config.yaml` は 8/18〜8/19 の間、この値が**1本の文字列**だった。
   その期間に実行した調査の記録は旧形式のまま残っている。

つまり、**集計に一切使われないプロンプトの文言キー**を理由に、過去の調査の結果が
読めなくなっていた。書き込み側（新しく書く定義）の厳格化が、読み取り側（実行済みの
記録の閲覧）にそのまま効いた形。`ui_config.yaml` とコードは移行済みだが、既に
`runs.metadata_json` に書かれた定義は誰も移行していない。

同じ形の地雷は他にもある（7/28 に廃止した `model.temperature`、7/30 に `main_survey:`
配下へ移した旧トップレベル `model:` / `prompt:`、`panel.screener`）。

## 2. 方針（利用者と確認済み）

**結果閲覧では、集計が読む範囲だけを復元する。** 境界は「集計の数字を決めるものか」
「どう聞いたかだけを決めるものか」。

| block | 記録から読むか | 理由 |
|---|---|---|
| `survey` / `panel` / `stimuli` / `questions` / `output` | **読む（従来どおり厳格に）** | セル・目標人数・コンセプト・設問タイプ・選択肢・`measure` / `slot`・セグメント軸は数字の意味そのもの |
| スクリーニングの方式（`mode`） | **読む** | 通過率が実測（ask）・推定（infer）・未測定（assume）のどれかで意味が変わる |
| `main_survey`（`model` / `prompt` / `persona_card`） | **読まない** | 集計側に参照が無い。今回の `reasoning` も、廃止した `temperature` も、旧トップレベル `model:` / `prompt:` もここに閉じる |
| `design` | **読まない** | 集計側に参照が無い |
| 未知のキー・旧い書き方 | **無視する** | 記録は実行済みの事実で、綴りを直させる相手がいない |

読まなかった block は既定値のまま入るので、**復元した定義を実行や「何をどう聞いたか」の
根拠に使わせない印**を付ける。定義の全文は従来どおり `raw` に残す。

## 3. 変更内容

1. `persona_sim/panel/loader.py`
   - `survey_from_record(record)` を追加。既存の厳格なフィールドパーサ（`_panel` /
     `_stimulus` / `_question` / `_output`）をそのまま再利用し、上表の block だけ組み立てる。
   - `_record_screening(source, path)`（方式だけ読む）を追加。`screening:` が無ければ
     旧い置き場所 `panel.screener` からも読む。
   - `RECORD_MODEL`（`endpoint: fake` / `deployment: ""`）を追加。読まない `model` に入れる、
     実行に使えないと読み取れる値。
   - `_reject_unusable_questions()` の docstring を更新（記録の読み直しはここを通らない）。
2. `persona_sim/panel/schema.py`: `SurveyDefinition.from_record: bool = False` を追加。
3. `persona_sim/storage/warehouse.py`: `fetch_survey()` を `survey_from_record()` 経由に。
4. `SPEC.md` §9 / `docs/SPEC_UI.md` §4.3 に、復元の範囲と「実行には使えない」ことを明記。
5. テスト
   - `tests/test_survey_loader.py`: 旧形式（1本文字列の `reasoning`／旧トップレベル
     `model:` `prompt:`／廃止した `temperature`／`panel.screener`）の記録が読めること、
     聞き方の block を読んでいないこと、`raw` と `from_record`、そして**数字を決める
     block が壊れていれば従来どおり止まる**こと。
   - `tests/test_warehouse.py`: ダミー接続で `fetch_survey()` が記録経路を通ること。

## 4. 検証

- `./scripts/run-tests.sh`（ベースライン: 3 failed / 952 passed。既存の失敗3件
  = `test_jtbd_definition` と notebook 2件は本変更と無関係）。
- 失敗件数を増やさないこと、追加テストが通ることを確認する。

## 5. この方針の限界

- 復元した定義は**実行・再現には使えない**（聞き方が既定値に入れ替わる）。
  `from_record` の印と `raw` の全文で担保する。
- 記録の読み直しでは未知のキーを弾かないので、**将来 `main_survey` 側に加える変更は
  結果閲覧では検知されない**。数字を決める block の厳格さは残すことで釣り合いを取る。
- 既に書かれた `runs.metadata_json` は書き換えない（実行時の事実として残す）。
