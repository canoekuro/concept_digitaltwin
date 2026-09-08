# コンセプト調査 Web UI: ルール改定とスキーマ再設計（結果）

- 日付: 2026/07/28 16:00
- 計画: [20260728-160000-concept-survey-ui-schema-plan.md](20260728-160000-concept-survey-ui-schema-plan.md)

## やったこと

### 1. ルール改定

**`AGENTS.md` の秘密情報の定義からサービングエンドポイント名を外した。** 秘密ではないうえ、
何で回したかが読み取れないと入力を再現できない（`SPEC.md` §9.1）。あわせて
「カタログ名・スキーマ名は書かない。テーブルの置き場所は環境変数からのみ解決する」を
独立した項目として書き分けた。両者を1行にまとめていたために、片方を守ろうとすると
もう片方まで巻き込む状態になっていた。

プレースホルダ `REPLACE_WITH_SERVING_ENDPOINT_NAME` / `REPLACE_WITH_JUDGE_ENDPOINT` /
`<サービングエンドポイント名>` を、リポジトリで実績のある `databricks-gemini-3-5-flash-lite`
に置き換えた（`SPEC.md` §3・§4.2、`examples/survey_sample.yaml`、
`notebooks/quickstart.ipynb`）。

**Web UI をフェーズ1のスコープに入れた。** `SPEC.md` §1.2 の「やらないこと」から
外し、§10.3 として CLI／ノートブック／UI の責務境界を1箇所に定義した。

### 2. screener スキーマの再設計（破壊的変更）

`infer` と `assume` は選択肢を提示しないのに `options` と `pass_if` を必須にしており、
**使い道の無い設定を書かせていた**。方式ごとに書ける形を分けた。

| 方式 | 書き方 |
|---|---|
| `ask` | `questions:`（設問文・選択肢・`pass_if`）。`conditions:` は書けない |
| `assume` / `infer` | `conditions:`（自然言語）。`questions:` は書けない |

方式に合わない書き方は、移し方を添えて読み込み時に停止する（`MISPLACED_SCREENER_KEYS`。
`job:` → `survey:` や `design.type` 廃止と同じ機構）。黙って読めると「条件を書いたのに
効いていない」ことに実行後まで気づけない。

条件文の出口を `Screener.condition_texts()` に一本化した。`ask` は設問から組み立て、
`assume` / `infer` は `conditions` をそのまま返す。判定プロンプト（`infer.condition_lines()`）も
前提ブロック（`assume_premise()` / `infer_premise()`）も、すべてここを通る。

`infer` の `screener_responses` を**判定1件＝1レコード**に改めた（予約
`question_id: "_infer"`、通過は `answer_codes: [1]`、非通過は空、`options_order` は空）。
従来は条件ごとに行を作り `pass_if[0]` を借りていたが、聞いてもいない設問に答えたように
見えてしまう。

### 3. 既存バグの修正

再設計の過程で表に出た。`screen_survey()` は判定済み候補を「全設問に答えているか」で
判断していたが、`infer` では `screener.questions` が空なので `all([])` が真になり、
**判定対象が空になって誰も通過しない**（Spark テスト12件が実際に落ちた）。
`expected_question_ids()` を追加し、`ask` は設問ID・`infer` は予約IDを返す単一の出口にした。
`load_premises()` の判定済み判定も同じ関数に寄せた。

### 4. 2層設定の導入

`config/ui_config.yaml`（調査横断の固定設定）と画面入力を `persona_sim/uiconfig` が
合成して調査定義を作る。

| モジュール | 役割 |
|---|---|
| `schema.py` | `UIConfig` / `SurveyForm` / `AllocationPattern` / `Benchmarks` |
| `loader.py` | YAML 読み込み。未知キーは調査定義ローダと同じく停止 |
| `allocation.py` | 年齢範囲 → 性年代セル。比率のみを積む |
| `census.py` | 国勢調査の人口構成比。対象年齢の範囲で再標準化 |
| `build.py` | 合成。組み立てた dict は `survey_from_dict()` に通す |
| `cost.py` | `Estimate` をトークン数と所要時間に換算 |

設計上の要点は3つ。

- **整数配分を UI で書かない。** 割り付け3パターンとも `quotas.mode: proportion` で比率を渡し、
  `panel/quotas.py::allocate_cell_sizes()` の最大剰余法に委ねる。UI 側で丸めると CLI 経由と
  人数が食い違いうる
- **検証を自前で書かない。** 組み立てた dict を `survey_from_dict()` に通すので、UI 経由と
  調査定義ファイル経由で通る検証が同じになる
- **セッション数を数え直さない。** `validate_static()` の `Estimate` をそのまま使う。
  `cost.py` がやるのは実績値での換算だけ

`ui_config.yaml` の `model:` と `prompt:` は解釈せずそのまま調査定義へ渡す。ここで再定義すると
`panel/schema.py` と二重管理になる。

`default_questions` には `top_box: [1, 2]` と `randomize_options: false` を入れた。
`top_box` が無いと `Question.is_ordinal()` が偽になり、`Metric.top_box` **と `Metric.mean` の
両方が `None`** になって、結果画面の T2B も平均スコアも出ない。

国勢調査の参照テーブルは `persona_sim/data/` に置き場所と出典欄（`README.md`）を用意した。
`.gitignore` の `data/` がパッケージ配下まで巻き込んでいたので、ルート限定（`/data/`）に直し、
`pyproject.toml` に `package-data` を追加してホイールに同梱されるようにした。
**実数値は未投入**で、CSV が無ければ国勢調査パターンを選択不可にする（均等割り付けへ
黙ってフォールバックしない）。

### 5. 仕様書

`docs/issues/concept_survey_ui_spec.md` → `docs/SPEC_UI.md` へ移して全面改訂した。
内容は issue（数行の課題メモ）ではなく仕様書のため。

非同期実行は **Databricks Jobs API 方式**と定めた。アプリ内スレッドで回さないのは、
Databricks Apps のコンテナが再起動しうるうえ、`run_survey()` が完走まで `responses` を
書かないため、途中で落ちるとその実行分が丸ごと消えるから。

**走行中のセッション単位の進捗は現状取れない**ことを、理由（`run/run.py:128-136` で
完走後に一度だけ `merge_upsert` する／`run_survey(progress=...)` はプロセス内コールバック）
とともに明記し、進捗表示はステップ単位から始めるとした。`runs` に要る列
（`status` / `job_run_id` / `survey_name` / `error_message`）も定義した。実装は後続。

## 検証

| 対象 | 結果 |
|---|---|
| `./scripts/run-tests.sh`（非Sparkテスト） | 344 passed |
| Spark マーク付きテスト（`-m spark`） | 63 passed |
| `ruff check persona_sim/ tests/` | All checks passed |
| `REPLACE_WITH_` の残存 | 0件 |
| `SPEC.md` §1.2 の Web UI 記述 | 削除済み |

新規テスト `tests/test_uiconfig.py`（30件）で次を確認した。

- `config/ui_config.yaml` がそのまま読めること、未知キー・未対応の刻み幅・
  ベンチマーク欠落で停止すること
- 割り付けが比率のみを積むこと（`n` を持たない）、年齢範囲が刻み幅で割り切れなければ停止すること
- 国勢調査比が対象範囲で再標準化されること、5歳階級を10歳刻みに束ねられること、
  参照テーブルが無ければ停止すること
- 合成した調査定義が `validate_static()` を `report.ok` で通ること
- セッション数が `N × コンセプト数 × 設問数` に一致すること
- 条件文が `conditions` に自然言語のまま入り、空欄なら `screener` が `None` になること
- 所要時間が並列度に反比例すること

`tests/test_survey_loader.py` に、方式に合わない書き方が移行ヒント付きで停止することと、
`ask` でも `condition_texts()` が同じ形で出ることのテストを追加した。

## 未解決・後続

- `app/` 配下の Streamlit ページ実装、Jobs API 投入コード、`app.yaml`、`streamlit` / `plotly` の依存追加
- `RUNS_SCHEMA` への `status` / `job_run_id` / `survey_name` / `error_message` の列追加
- 国勢調査 CSV の実数値投入（出典・年次・取得日を `persona_sim/data/README.md` に記入すること）
- セッション単位の進捗ハートビート
- `docs/issues/202607281100.md`（`model.thinking` がエンドポイントに送られていない）は据え置き
