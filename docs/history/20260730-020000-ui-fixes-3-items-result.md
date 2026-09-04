# `docs/issues/UIの修正.md` の3件を実装する（結果）

計画（`20260730-020000-ui-fixes-3-items-plan.md`）どおりに実施した。

## 実施内容

### 1. 性別ごとのチェックボックス + from-to 年齢入力

- `app/views/survey_design.py`: `SEXES`（`persona_sim.uiconfig.schema`）でループし、
  性別ごとに「対象にする」チェックボックス→チェック時のみ年齢 from/to の数値入力
  （15〜79、既定20〜69。従来のスライダーと同じ範囲）を表示。`sex_ranges` に集約し、
  空なら「対象の性別を1つ以上選んでください。」で `build_survey` を呼ばない。
- `persona_sim/uiconfig/schema.py`: `SurveyForm.sex_ranges: Mapping[str, tuple[int,int]]`
  に置き換え。
- `persona_sim/uiconfig/allocation.py`: `build_quotas(pattern, sex_ranges, census=None)`
  に変更。性別ごとに `age_bands()` を呼び、選ばれた性別分だけセルを作る
  （`sex_ranges` 空なら `UIConfigError`）。
- `persona_sim/uiconfig/census.py`: `ratios(rows, sex_ranges, band)` に変更。性別ごとに
  異なる範囲で人口を集計し、全体で再標準化。
- `persona_sim/uiconfig/build.py`: `_global_filters()` を新設。性別1つなら
  `{"sex":..., "age_min":..., "age_max":...}`、複数性別（範囲違いを含む）なら性別条件を
  外し年齢だけ envelope（最小の下限〜最大の上限）にする。割り付けセル条件が性別・年齢を
  厳密に絞るため、この安全網が緩くても対象外のペルソナは混じらない。
- `persona_sim/uiconfig/__init__.py`: `SEXES` を re-export に追加。
- `docs/SPEC_UI.md` §3.1・§3.3 を更新。

### 2. コンセプトの追加・削除ボタン

- `app/views/survey_design.py`: `st.session_state.concept_ids`（安定ID配列）+
  `next_concept_id`（削除してもIDを使い回さない）に置き換え。ウィジェットキーは
  `concept_name_{id}` / `concept_text_{id}`。各コンセプトの expander 内に
  「🗑 このコンセプトを削除」ボタン、一覧の下に「＋ コンセプトを追加」ボタンを1つ。
  0件になっても既存の「コンセプトを1件以上入力してください。」がそのままガードになる
  ため、追加のdisabledロジックは不要だった。
- `docs/SPEC_UI.md` §3.1 を更新。

### 3. ライトモードの見た目

- `app/.streamlit/config.toml`（新規）: `base=light`、`backgroundColor=#f5f5f7`
  （ページ全体のソフトグレー）、`secondaryBackgroundColor=#ffffff`（入力欄の下地。
  ページ背景とのコントラストを作る）、`primaryColor=#0071e3`、`textColor=#1d1d1f`。
- `app/lib/layout.py`: `inject_css()` を追加（input/textarea/select の角丸・薄枠線、
  ボタンの角丸のみ。複雑な指定はしない）。
- `app/app.py`: `set_page_config` 直後に `inject_css()` を呼ぶ。

### 追加で見つかった問題の修正

`tests/test_uiconfig.py` の `CONFIG_PATH = Path("config/ui_config.yaml")` が、実際の
設定ファイルの場所 `app/config/ui_config.yaml` と食い違っていた。この行自体は今回の
3件と無関係だが、`tests/test_uiconfig.py` はまさに今回変更するテストファイルであり、
このバグのせいで `ui` フィクスチャに依存する27件のテストがことごとく
`FileNotFoundError` で ERROR になっていて、自分の変更を検証できなかったため修正した
（`app/lib/context.py` の実行時デフォルトパスは Databricks Apps / ローカル実行時とも
CWD が `app/` になる前提で正しく、そちらは変更していない）。この行を
`Path("app/config/ui_config.yaml")` に直しただけで、CI で継続的に落ちていた
`test_uiconfig.py` の `3 failed, 21 errors` がすべて解消した（PR #12 で報告した
ベースブランチの既存障害と同一のもの）。

## 検証結果

- `./scripts/run-tests.sh`: **403 passed**（新規・既存とも全件グリーン。上記の
  `CONFIG_PATH` 修正により、以前ベースブランチから引き継いでいた
  `test_uiconfig.py` の失敗も解消した）
- `uv run ruff check`: 変更した Python ファイルすべてでエラー無し
- Streamlit アプリを実際に起動（`streamlit run app/app.py`、CWD を `app/` にして
  `app/.streamlit/config.toml` を読ませる）し、Playwright（ヘッドレス Chromium）で
  ブラウザ操作を行い目視・DOM確認した:
  - ページ背景 `rgb(245,245,247)` / 入力欄・サイドバー背景 `rgb(255,255,255)` が
    ピクセル単位で意図どおり適用されていることを確認
  - 「女性を対象にする」のチェックを外すと、女性の年齢入力欄が消え、
    「組み立てた調査定義を見る」の JSON で `panel.quotas.cells` が男性の5セルのみ、
    `panel.filters` が `{"sex": "男", "age_min": 20, "age_max": 69}` になることを確認
  - 「＋ コンセプトを追加」を押すと一覧の下に新しい空のコンセプトカードが追加され、
    各コンセプトに個別の削除ボタンが表示されることを確認

## 未対応・引き継ぎ事項

- 複数性別・範囲違いのケース（例: 男性20〜39歳・女性40〜69歳）は自動テストのみで確認し、
  ブラウザでの目視確認は単一性別のケースのみ行った。挙動はテストで担保しているが、
  UI上の見え方（例: 見積もり表示に性別ごとの範囲が反映されるか）は未確認。
- CSS はごく最小限（入力欄・ボタンの角丸と枠線）に留めた。「macOS風」の具体的な色味に
  ついては要望次第で微調整の余地がある。
