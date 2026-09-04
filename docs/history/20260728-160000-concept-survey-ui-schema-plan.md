# コンセプト調査 Web UI: ルール改定とスキーマ再設計（計画）

- 日付: 2026/07/28 16:00
- 対象: `docs/issues/concept_survey_ui_spec.md` のレビューと、そこから判明した既存ルール・
  スキーマの見直し

## 背景

`docs/issues/concept_survey_ui_spec.md` が追加された。レビューしたところ、仕様と既存実装の
食い違いが複数見つかった。ただし食い違いの多くは**仕様側ではなくルール・スキーマの方を
直すべきもの**という判断になった。

1. 「エンドポイント名を仕様書に残さない」というルールが、実務の設定ファイルと合わない
2. 提示された `ui_config.yaml` の形の方が実務に近い。コード側をその形に合わせる
3. `infer` なのに `options` / `pass_if` を必須にしているのは不合理
4. テーブル所在が環境変数固定なのは維持（仕様側から `storage:` を落とす）
5. Web UI はやる（フェーズ1のスコープを改定する）

## 判明した主な食い違い

| # | 内容 |
|---|---|
| 1 | `ui_config.yaml` のキーが調査定義スキーマと合わず、`loader._reject_unknown` に全部弾かれる |
| 2 | `default_questions` に `top_box` が無く、T2B と平均スコアが両方 `None` になる |
| 3 | 自然言語の対象者条件を `ScreenerQuestion` に落とす経路が無い（選択肢と `pass_if` が必須） |
| 4 | `runs` に `status` 列が無く、`responses` も完走後に一度だけ書かれるため走行中の進捗が取れない |
| 5 | 国勢調査の人口比データがリポジトリに存在しない |
| 6 | `SPEC_PHASE1.md` §1.2 が Web UI を「やらないこと」に挙げている |
| 7 | 最大剰余方式は `panel/quotas.py` に実装済み（新規実装は不要） |

## 対象ファイル

### ルール改定
- `AGENTS.md` … 秘密情報の定義からエンドポイント名を外す。Web UI をスコープに加える
- `SPEC_PHASE1.md` … §1.2（スコープ）、§10.3（Web UI）を追加・改定
- `persona_sim/config.py` … docstring をカタログ名・資格情報のみに限定
- `examples/survey_sample.yaml` / `notebooks/quickstart.ipynb` … プレースホルダを実名に置換

### screener スキーマ再設計（破壊的変更）
- `persona_sim/panel/schema.py` … `Screener.conditions` 追加、`condition_texts()` を単一の出口に
- `persona_sim/panel/loader.py` … mode 分岐と移行ヒント付きエラー
- `persona_sim/panel/validate.py` … `_check_screener_questions` / `_check_screener_conditions`
- `persona_sim/panel/screening.py` … 前提ブロック、`expected_question_ids()`、`INFER_QUESTION_ID`
- `persona_sim/panel/infer.py` … `condition_lines()` の委譲、判定1件＝1レコード
- `SPEC_PHASE1.md` §2.6 / §4.2、`README.md`
- テスト: `test_survey_loader.py` / `test_validate.py` / `test_screening.py` /
  `test_screening_spark.py` / `test_infer.py`

### 2層設定
- `config/ui_config.yaml`（新規）
- `persona_sim/uiconfig/`（新規: `schema` / `loader` / `allocation` / `census` / `build` / `cost`）
- `persona_sim/data/README.md`（新規: 国勢調査データの出典欄）
- `tests/test_uiconfig.py`（新規）

### 仕様書
- `docs/issues/concept_survey_ui_spec.md` → `docs/SPEC_UI.md`（`git mv` ＋全面改訂）

### 記録
- `docs/history/20260728-160000-concept-survey-ui-schema-{plan,result}.md`
- `CHANGELOG.md`

## 方針

- **UI はロジックを持たない。** 調査定義を組み立てて既存の実行経路に渡すだけ。
  割り付けの整数配分は `allocate_cell_sizes()` の最大剰余法に委ね、UI 側で丸めない
- **組み立てた調査定義は `survey_from_dict()` に通す。** UI 経由と調査定義ファイル経由で
  通る検証が違ってはいけない
- **セッション数は `validate_static()` の `Estimate` を使う。** 数え方を2箇所に持たない
- 非同期実行は Databricks Jobs API 方式とする。今回は仕様に定めるところまでで、
  Streamlit アプリ本体と `runs` の列追加は後続

## 検証

1. `./scripts/run-tests.sh`（非Sparkテスト）
2. Spark マーク付きテスト（`test_screening_spark.py` を含む）
3. `config/ui_config.yaml` から合成した調査定義が `validate_static()` を通ること
4. 旧形の screener が移行ヒント付きで停止すること
5. `REPLACE_WITH_` の残存が0件であること

## 今回やらないこと

- `app/` 配下の Streamlit ページ、Jobs API 投入コード、`app.yaml`
- `RUNS_SCHEMA` への `status` 等の列追加の実装（仕様に定めるところまで）
- 国勢調査 CSV の実数値投入
