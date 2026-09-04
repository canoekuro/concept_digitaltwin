# 調査設計ページの見積もりを実測ベース（回答件数単位）に置き直す（計画）

- 日時: 2026-08-10 09:35:30 UTC
- 対象: `app/views/survey_design.py` §5 / `persona_sim/uiconfig/{schema,loader,cost}.py` /
  `persona_sim/panel/validate.py` / `app/config/ui_config.yaml` / `docs/SPEC_UI.md` §3.4
- 起点: `docs/issues/調査設計ページの提示内容改修.md`（3回分の実測にもとづく要望）

## 1. 現状と問題

issue の要望は3つ。実測式を「5. 確認して開始」に反映する、コンセプト文の分量で変動する旨を
添える、トークン数・単価・実行速度をパラメータ化する。現行実装との差は4点ある。

### (1) 換算の単位が食い違っている

issue の実測値（1,600入力 / 15出力 / 1,190.6件/分）は明確に **1回答あたり**の値だが、
`estimate_cost()` はこれを `Estimate.sessions` に掛けている。

セッション数は `size × len(session_groups(survey))`（`persona_sim/panel/validate.py`）で、
`session_groups()` は `remember` の連結成分で設問をまとめる。つまり**記憶を持つ設問がある
調査ではセッション数 < 回答件数**になり、回答単位の実績値を掛けると出力トークンが
まとまり具合のぶんだけ過小に出る。

現行の UI は `memory: none` 固定（`persona_sim/uiconfig/build.py` の `_expand_questions()`）
なので1セッション＝1回答で偶然一致しているだけで、単位としては誤っている。

### (2) 実績値が古い

`survey_session` は 1,500 入力 / 12 出力。実測は 1,600 / 15。

### (3) 所要時間の式が実測と合わない

現行は `平均レイテンシ × セッション数 ÷ model.concurrency`。設定値（2秒・並列度32）では
960件/分に相当し、実測のエンドツーエンド速度 1,190.6件/分 と合わない。

### (4) 金額が出ない

単価がワークスペースごとに違うため算定しない、という設計だった（`docs/SPEC_UI.md` §3.4）。
単価を設定パラメータにすれば概算は出せる。

加えて、入力トークンはコンセプト文の分量で変動するのに、その旨がどこにも出ていない。

## 2. 方針（利用者と確認済み）

- 本調査側の換算は **回答件数ベース**（`R = panel_size × 展開後設問数`）。
  スクリーニングは1回で `batch_size` 人分を判定するので**セッション単位のまま**。
- 所要時間は `avg_latency_sec` を廃止し、**実測スループット（件/分）** に置換する。
  並列度では割らない（実測値に織り込み済みのため、割ると二重に効く）。
- 単価は **USD 単価＋為替レート**（モデル改定と為替を別々に直せる）。
- 金額表示は **安全側予算のみ**。標準の概算は保持も表示もしない——予算取りに使う数字が
  2つあると、どちらを見たかで結論が変わる。
- 記憶を持つ調査（`sessions ≠ R`）向けの**実行時の警告は出さない**。現行 UI の
  コンセプト調査は `memory: none` 固定で実害がないため。ただし**今後ほかの調査種別を
  作るときに考慮から漏れないよう、メモを残す**。

### 仕様の食い違いの扱い

`SPEC_PHASE1.md` §9 の「実行メタデータの `estimated_cost` は `null` 固定」は**変更しない**。
変えるのは画面の事前見積もり表示だけで、`docs/SPEC_UI.md` §3.4 の「金額は出さない」の
段落を差し替える。事前の目安とワークスペースの実請求は別物という線引きは残す。

## 3. 変更内容

| ファイル | 変更 |
|---|---|
| `persona_sim/panel/validate.py` | `Estimate.answers`（`panel_size × questions`）を追加。`sessions` との違いを docstring に書く |
| `persona_sim/uiconfig/schema.py` | `AnswerBenchmark`（回答単位）/ `Pricing` を新設。`SessionBenchmark` のフィールドを `*_per_session` / `sessions_per_min` に改名 |
| `persona_sim/uiconfig/loader.py` | `survey_answer` / `pricing` を読む。旧キー（`survey_session` / `avg_*`）は移行先と単位変更を添えて停止。速度・為替の 0 以下と `budget_margin < 1` を拒否 |
| `persona_sim/uiconfig/cost.py` | `concurrency` 引数を削除。本調査は `answers` に、判定は `screener_sessions` に掛ける。`budget_jpy` を追加。注記にコンセプト文の分量と単価の前提を足す |
| `app/config/ui_config.yaml` | 実測値と単価に差し替え。キー名に単位を入れる |
| `app/views/survey_design.py` | メトリクスを5列にし「予算目安」を追加 |
| `docs/SPEC_UI.md` §3.4 | 表・単位の説明・金額の段落を書き換え |
| `tests/test_uiconfig.py` / `tests/test_validate.py` | 回答件数ベースへの追随と、単位・予算・注記・移行の回帰テスト |

### 単位の取り違えを再発させない置き方

コメントだけに頼らず、**型と設定キー名で**分ける。

- 本調査は `AnswerBenchmark`（`input_tokens_per_answer` / `answers_per_min`）、
  判定は `SessionBenchmark`（`input_tokens_per_session` / `sessions_per_min`）。
- 旧キー `survey_session` は「`survey_answer` に改めた。単位も1回答あたりに変わっている」と
  告げて停止する。素通りさせると、桁は合っているのに記憶を持つ調査で過小に出る見積もりになる。

「回答件数単位である」ことのメモは、次に調査種別を足す人が読む場所すべてに置く——
`AnswerBenchmark` の docstring（本体）、`cost.py` のモジュール docstring、
`Estimate` の docstring、`ui_config.yaml` のコメント、`docs/SPEC_UI.md` §3.4。

## 4. 検証

1. `./scripts/run-tests.sh`（非 Spark 全件）
2. 実配置の設定が読めること（`load_ui_config('app/config/ui_config.yaml')`）
3. issue の実例（200人 × 64コンセプト × 2問 = 25,600回答、判定なし）と突き合わせる——
   入力 約 40.96M / 出力 384,000 / 所要 約 21.5分 / 1件あたり 約 0.28円
4. `remember` を持つ調査で `sessions < answers` でも回答件数ぶんのトークンが出ること
5. 旧設定（`survey_session` / `avg_latency_sec`）が移行メッセージつきで止まること
