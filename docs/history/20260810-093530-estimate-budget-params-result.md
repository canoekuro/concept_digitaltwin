# 調査設計ページの見積もりを実測ベース（回答件数単位）に置き直す（結果）

- 日時: 2026-08-10 09:35:30 UTC
- 計画: [20260810-093530-estimate-budget-params-plan.md](20260810-093530-estimate-budget-params-plan.md)
- 起点: [docs/issues/調査設計ページの提示内容改修.md](../issues/調査設計ページの提示内容改修.md)

## 1. 何を変えたか

調査設計ページ「5. 確認して開始」の見積もりを、実測ベースに置き直した。変更は4系統。

### (1) 換算の単位を「1セッションあたり」から「1回答あたり」へ

issue の実測値（1,600入力 / 15出力 / 1,190.6件/分）は1回答あたりの値だが、
`estimate_cost()` はこれを `Estimate.sessions` に掛けていた。セッション数は
`remember` の連結成分で設問をまとめて数えるので（`session_groups()`）、記憶を持つ設問が
ある調査では回答件数より少なくなり、掛けると出力トークンと予算が過小に出る。

`Estimate.answers`（`panel_size × 展開後設問数`）を追加し、本調査の換算はこれに掛ける
ようにした。判定は1回で `batch_size` 人分をまとめて捌くので、`screener_sessions` に
掛ける**セッション単位のまま**。

**単位の取り違えを型と設定キー名で潰した。**

- `AnswerBenchmark`（`input_tokens_per_answer` / `output_tokens_per_answer` / `answers_per_min`）
- `SessionBenchmark`（`input_tokens_per_session` / `output_tokens_per_session` / `sessions_per_min`）

同じ dataclass を2つの単位で使い回していたのをやめたので、掛ける先を間違えると型で気づく。

### (2) 所要時間をレイテンシ式から実測スループットへ

`平均レイテンシ × セッション数 ÷ model.concurrency` をやめ、`回答件数 ÷ answers_per_min`
＋ `判定回数 ÷ sessions_per_min` にした。`estimate_cost()` の `concurrency` 引数は削除した——
`*_per_min` が並列度込みのエンドツーエンド実測値なので、さらに割ると二重に効く。

### (3) 予算目安（円）の追加

`Pricing`（`input_usd_per_million` / `output_usd_per_million` / `jpy_per_usd` /
`budget_margin`）を `ui.estimation_benchmarks.pricing` に置き、画面に「予算目安」を出す
（メトリクスは4列→5列）。単価と為替を分けたのは、モデル改定と為替が別々に動くため。

**出すのは安全側の予算だけ**にした。標準の概算は保持も表示もしない——予算取りに使う
数字が2つあると、どちらを見たかで結論が変わる。判定ぶんのトークンも合算している
（issue の式は本調査だけを扱うが、判定も実際にトークンを消費するので、
足さないと対象者条件を付けた調査で過小に出る）。

### (4) 注記と実績値

- 「入力トークンはコンセプト文の分量で増減します」を注記に追加（issue の要望）。
- 「予算目安は設定した単価・為替に安全側の係数を掛けた概算で、実際の請求額ではありません」も追加。
- 実績値を 1,500/12 → 1,600/15、速度を 960件/分相当 → 1,190.6件/分 に更新した。

### 仕様の食い違いの扱い

`docs/SPEC_UI.md` §3.4 の「金額は出さない」を差し替えた。**`SPEC_PHASE1.md` §9 の
`estimated_cost` は `null` 固定のまま**で、変えたのは画面の事前見積もりだけ。
事前の目安とワークスペースの実請求を同じ数字として扱わせないよう、その線引きは残した。

## 2. 記憶を持つ調査の扱い（実行時の警告は入れていない）

利用者の判断で、`sessions ≠ answers` を検知した際の画面警告は**入れていない**。
現行 UI のコンセプト調査は `memory: none` 固定（`uiconfig/build.py` の `_expand_questions()`）
で実害がないため。

代わりに、**次に調査種別を足す人が読む場所すべてにメモを置いた**。

- `persona_sim/uiconfig/schema.py` の `AnswerBenchmark` docstring（本体。なぜ回答単位か、
  記憶を持つ調査では履歴再生で回答あたりの入力も増えるので実績値の取り方から見直すこと）
- `persona_sim/uiconfig/cost.py` のモジュール docstring
- `persona_sim/panel/validate.py` の `Estimate` docstring（`sessions` と `answers` の違い）
- `app/config/ui_config.yaml` のコメント
- `docs/SPEC_UI.md` §3.4

## 3. 旧設定の扱い

旧キーは黙って捨てず、移行先を添えて停止する（`ui_config.yaml` は他リポジトリや
ワークスペースにコピーが存在しうるため）。

- `survey_session` → 「`survey_answer` に改めた。単位も1セッションあたりから1回答あたりに
  変わっている」と単位変更まで告げる。素通りさせると、桁は合っているのに記憶を持つ調査で
  過小に出る見積もりになる。
- 各ベンチマーク内の `avg_input_tokens` / `avg_output_tokens` / `avg_latency_sec` も、
  それぞれの新キー名を添えて停止する。

あわせて `answers_per_min` / `sessions_per_min` / `jpy_per_usd` の 0 以下と
`budget_margin < 1` を拒否する（前者はゼロ除算、後者は安全側の係数が下振れ側に効く）。

## 4. 検証

- `./scripts/run-tests.sh`（非 Spark 全件）… **661件全件パス**（変更前649件、12件追加）
- `uv run ruff check persona_sim app tests` … パス
- 実配置の設定が読めること … 確認済み
  （`AnswerBenchmark(1600, 15, 1190.6)` / `SessionBenchmark(4500, 60, 640.0)` /
  `Pricing(1.0, 6.0, 150.0, 1.1)`）
- **issue の実例と一致**（200人 × 64コンセプト × 2問、判定なし）

  | 項目 | issue | 実装 |
  |---|---|---|
  | 回答件数 | 25,600 | 25,600 |
  | 入力トークン | 約 40.96M | 40,960,000 |
  | 出力トークン | 384,000 | 384,000 |
  | 実行時間 | 約 21.5分 | 21.5分 |
  | 予算 | 約 7,168円（0.28円/件） | 7,139円（0.2789円/件） |

  予算の差は、issue が 0.28円/件 の丸めた係数を使うのに対し、実装が
  標準 0.2535円/件 × `budget_margin` 1.1 で出しているため。

- 旧設定（`survey_session` / `avg_latency_sec`）が移行メッセージつきで止まること … 確認済み

### 追加したテスト（12件）

- `test_cost_uses_answers_not_sessions` … **今回の指摘そのものの回帰テスト**。
  セッション数だけ半分にした `Estimate` でトークンと予算が変わらないことを固定する。
- `test_duration_follows_throughput` … スループット半分で所要時間が倍になること。
- `test_budget_uses_configured_pricing` / `test_budget_matches_the_measured_rate_per_answer`
  … 予算が設定どおりに出ること、判定なしの調査で 1回答あたり約 0.28円 になること。
- `test_estimate_notes_that_concept_length_moves_the_input_tokens` … 注記が消えたら落ちる。
- loader 4件 … 旧キー（`survey_session` / `avg_latency_sec`）の停止、`pricing` 欠落、
  速度 0以下、`budget_margin < 1`。
- `tests/test_validate.py` 2件 … 記憶なしで `answers == sessions`、
  `full_session` で `answers > sessions` になること。

## 5. 未対応事項

- **Streamlit 画面での目視確認は未実施**（この環境に Databricks 接続が無いため）。
  数値は `estimate_cost()` を直接呼んで確認した。5列メトリクスの見え方は実機で確認が要る。
- **判定側（`screener_session`）の実績値は実測ではない。** `sessions_per_min: 640` は
  旧 `avg_latency_sec: 3.0` × 並列度32 からの換算で、トークン数も従来値のまま。
  本調査側だけが今回の実測で更新されている。
- **記憶を持つ調査種別を作る場合、本調査の実績値は取り直しが要る。** 履歴を再生するぶん
  回答あたりの入力トークンが増えるため、現在の 1,600 では合わなくなる（§2 のメモ参照）。
