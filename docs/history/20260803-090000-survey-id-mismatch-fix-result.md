# 男性のみを選んだのに結果に女性が混ざる問題の修正 — 結果

- **日付**: 2026/08/03 09:00
- **計画**: [20260803-090000-survey-id-mismatch-fix-plan.md](20260803-090000-survey-id-mismatch-fix-plan.md)

## 破壊的変更

**`persona_sim.uiconfig.jobs` の `submit_survey()` / `upload_survey()` から
`survey_id` 引数を外した。** ID は渡された調査定義の `survey.id` から引く。

```python
# 変更前
submit_survey(client, job_id, volume, survey_id, survey_dict)
upload_survey(client, volume, survey_id, survey_dict)

# 変更後
submit_survey(client, job_id, volume, survey_dict)
upload_survey(client, volume, survey_dict)
```

`survey.id` が引けない dict は `UIConfigError` で停止する。`run()` の引数は変えていない
（パスを受け取る関数であり、唯一の呼び出し元 `submit_survey()` が同じ dict から引いた
ID を渡す）。

既存 Delta テーブルの作り直しは**不要**。`survey_id` の形式が
`{slug}_{時刻}` から `{slug}_{時刻}_{識別子}` に変わるが、過去の ID はそのまま読める。

## 変更内容

### 1. `survey_id` を投入1回につき1つに固定（主因）

`app/views/survey_design.py`

- `survey_id` を `st.session_state` に持つ。調査名が変わったときだけ振り直し、
  ウィジェット操作による再実行では同じ値を使う。`SurveyForm.survey_id` に渡す
- 調査定義の組み立てを **1スクリプト実行につき1回**にした。新設の
  `build_survey_pair()` が dict と検証済み定義を同じ dict から返す。
  検証・見積もり・「組み立てた調査定義を見る」・投入がすべて同じ dict を使う
- 投入が成功したら `session_state` の ID を捨てる。同じ ID で投げ直すと、ジョブ側が
  `delete_survey_rows()` で先行実行の `panels` / `responses` を消して書き直してしまう

`persona_sim/uiconfig/build.py`

- `build_survey_pair(ui, form, census)` を追加。`build_survey()` は従来どおり残す
  （CLI・ノートブックからは1回しか呼ばないので問題にならない）

### 2. 投入先と冪等トークンを調査定義から引く

`persona_sim/uiconfig/jobs.py`

- `survey_id_of(survey_dict)` を追加。`upload_survey()` / `submit_survey()` がこれを使う。
  呼び出し側が別に持っている ID を渡せる限り、ファイル名と中身は再びずれうる

### 3. `survey_id` の衝突回避

`persona_sim/uiconfig/build.py` の `survey_id()` に `secrets.token_hex(3)` の接尾辞を足した。
日本語の調査名は slug が空になり `survey_{時刻}` に潰れるため、同じ秒に2件投入すると
ID が衝突し、後続の実行が先行実行の結果を消していた。

### 4. 対象者を画面に出す

`persona_sim/uiconfig/summary.py`（新規）

- `target_summary(survey)` → `男性 20〜69歳（5セル・計 500名）`
- **`panel.filters` ではなく `panel.quotas.cells` から作る。** 複数性別のときの `filters` は
  年齢の envelope だけで性別条件を持たない（`docs/SPEC_UI.md` §3.3）ため、`filters` を見ると
  「性別の指定なし」に見えてしまう
- 文言の組み立てを Streamlit 側に置かない。調査設計と結果閲覧が同じ文言を出す必要があり、
  片方だけ直すと「開始前に見た対象」と「結果の対象」が食い違って読める

`app/views/survey_design.py` は見積もりの下に、`app/views/results.py` は取得後に表示する。

### 5. 結果一覧で調査を見分けられるようにする

`app/views/results.py`

- 一覧ラベルを `調査名（完了時刻 / survey_id）` にした。調査名は重複しうるので、
  名前と時刻だけでは同名の別実行を見分けられない
- 取得後に `survey_id` と対象者を出す

### 6. 定義と実データの食い違いを表面化させる

`persona_sim/aggregate/aggregate.py` の `build_result()` に `_off_target_notes()` を追加。

- 割り付けセルに無い `sex` の回答、定義に無い `cell_id` の回答を `notes` に積む
- 止めない（品質フラグと同じ扱い）。`app/views/results.py` が既に `notes` を
  `st.warning` で描画しているので表示側の追加は不要
- セル側に条件が無い属性は照合しない。条件を書いていない以上、どの値も対象外ではない
- **これがあれば今回の事象は結果を開いた瞬間に警告として出ていた**

## 検証

`./scripts/run-tests.sh` … 470 passed（`-m "not spark"`）。ruff もクリーン。

追加したテスト:

- `tests/test_jobs.py`
  - 置いた YAML のパスが中身の `survey.id` と一致し、冪等トークンも同じ ID 由来であること
  - `survey.id` が引けない定義は `UIConfigError` で停止すること（4パターン）
- `tests/test_uiconfig.py`
  - `survey_id()` が同一秒・同一調査名で衝突しないこと（50回）
  - `survey_id` を固定すれば組み立てを繰り返しても ID が変わらないこと
  - `build_survey_pair()` の dict と定義の ID が一致すること
- `tests/test_uiconfig_summary.py`（新規）… 対象サマリーの文言5件
- `tests/test_aggregate_pure.py` … 定義外の性別・セルで注記が出ること、
  定義どおりなら出ないこと、セルに条件が無ければ照合しないこと

再現スクリプトでの確認（修正前は1秒あけると ID がずれていた）:

```
固定した survey_id      : survey_20260803085104_3127c0
定義オブジェクトの ID   : survey_20260803085104_3127c0
YAML に入る survey.id   : survey_20260803085104_3127c0
1秒後に組み直した ID    : survey_20260803085104_3127c0
アップロード先          : /Volumes/main/lab/surveys/survey_20260803085104_3127c0.yaml
冪等トークン            : persona-sim-survey_20260803085104_3127c0
対象サマリー            : 男性 20〜69歳（5セル・計 500名）
```

### 未実施

実環境（Databricks）での投入から結果閲覧までの通し確認。接続が要るためこの作業では行っていない。
男性のみで1件投入し、`st.success` に出た ID で結果閲覧の一覧を引けること、その調査の
集計表に女性が現れないこと、対象サマリーが「男性 20〜69歳」と出ることを確認すること。
