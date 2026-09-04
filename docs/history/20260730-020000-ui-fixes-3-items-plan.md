# `docs/issues/UIの修正.md` の3件を実装する（計画）

## 背景・目的

`docs/issues/UIの修正.md` に、調査設計ページ（`app/views/survey_design.py`）への
3件の改修要望が追加された。

1. 性別ごとにチェックボックスで対象/対象外を選び、対象にした性別だけ年齢を from-to の
   数値入力で指定できるようにする（未チェックの性別は調査対象から除外）。job 側が
   性別ごとの絞り込みに対応しているかも確認する。
2. 「コンセプトを追加」ボタンを一覧の下に、削除ボタンを各コンセプト単位で持てるようにする
   （現状の「最後の1件を削除」ボタンは廃止）。
3. ライトモードで入力欄が白くて見づらいので、複雑なCSSを使わずに macOS 風の見た目に
   近づける。

事前調査の結果、job側（`persona_sim/panel`）は `QuotaCell.conditions`（性別・年齢を
セルごとに独立して持てる `PersonaFilter`）により、すでに性別ごとの絞り込みに対応済み
だった。変更が必要なのは UI (`app/views/survey_design.py`) と、その間を橋渡しする
`persona_sim/uiconfig/*` だけ。

「コンセプトを追加」ボタンの配置は、「各コンセプトの一番下」という文言に複数の解釈が
あり得たため、削除ボタンと違って追加操作はどこを押しても同じ挙動（末尾に1件追加）に
なることから、一覧全体の下に1個だけ置く案を採用する（違和感があれば後で調整）。

## 変更内容

### 1. 性別ごとのチェックボックス + from-to 年齢入力

- `app/views/survey_design.py`: 単一のスライダーを、`SEXES` でループした
  性別ごとの `st.checkbox` → チェック時のみ2つの `st.number_input`（from/to）に置き換え。
  結果を `sex_ranges: dict[str, tuple[int,int]]` に集める。1つも選ばれていなければ
  `st.info` でガードする。
- `persona_sim/uiconfig/schema.py`: `SurveyForm.age_min`/`age_max` を
  `sex_ranges: Mapping[str, tuple[int,int]]` に置き換え。
- `persona_sim/uiconfig/allocation.py`: `build_quotas()` の `age_min`/`age_max` 引数を
  `sex_ranges` に置き換え、性別ごとに `age_bands()` を呼んでセルを作る（未選択の性別は
  自然に除外）。
- `persona_sim/uiconfig/census.py`: `ratios()` も同様に `sex_ranges` を受け取るよう変更。
- `persona_sim/uiconfig/build.py`: `build_survey_dict()` が `build_quotas(pattern,
  form.sex_ranges, census)` を呼ぶ。グローバル `panel.filters` の組み立てを
  `_global_filters()` に切り出し、性別が1つだけなら従来どおり性別・年齢とも絞り、
  複数性別（範囲が違う場合を含む）なら性別条件を外し年齢は envelope にする
  （セル条件が性別・年齢を厳密に絞るので安全）。
- `persona_sim/uiconfig/__init__.py`: `SEXES` を re-export に追加。
- `tests/test_uiconfig.py`: 既存の `build_quotas`/`ratios` 呼び出し・`_form()` を新
  シグネチャに更新し、性別除外・性別ごとの範囲違い・`sex_ranges` 空拒否・
  `panel.filters` の envelope 化を確認するテストを追加。
- `docs/SPEC_UI.md` §3.1・§3.3 を新しい入力項目・割り付けロジックに合わせて更新。

### 2. コンセプトの追加・削除ボタン

- `app/views/survey_design.py`: 位置ベースの `concept_count`（int）を、安定ID方式
  （`concept_ids: list[int]` + `next_concept_id`）に置き換え。ウィジェットキーも
  インデックスではなく ID にする。各コンセプトの expander 内に個別の削除ボタンを、
  一覧の下に追加ボタンを1つ置く。「最後の1件を削除」・`disabled=...<=1` は削除。
- `docs/SPEC_UI.md` §3.1 の説明も配置に合わせて更新。

### 3. ライトモードの見た目

- `app/.streamlit/config.toml`（新規）: `[theme]` で背景・セカンダリ背景（入力欄の下地）・
  アクセント色・文字色だけを指定。
- `app/lib/layout.py`: `inject_css()` を追加（入力欄・ボタンの角丸と枠線のみ、最小限）。
- `app/app.py`: `set_page_config` の直後で `inject_css()` を呼ぶ。

## 検証方法

- `./scripts/run-tests.sh` でリポジトリ全体のテストがグリーンであること
- Streamlit アプリを実際に起動し、ブラウザ（Playwright）で性別チェックボックスの
  ON/OFF・コンセプトの追加/削除・テーマ適用を目視確認する
- `docs/history/` に plan/result のペアを保存し、`CHANGELOG.md` に1エントリ追記する
