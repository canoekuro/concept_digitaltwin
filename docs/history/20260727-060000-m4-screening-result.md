# 実施結果: M4 スクリーニング機構

- **日時**: 2026/07/27 07:30
- **計画**: [20260727-060000-m4-screening-plan.md](20260727-060000-m4-screening-plan.md)

## 変更内容

### 1. 仕様の改訂（`SPEC.md` 0.8 → 0.9）

| 箇所 | 内容 |
|---|---|
| §2.2 `panels` | `role` を4値（`candidate` / `main` / `reserve` / `screened_out`）に。`cell_rank` を列として追加 |
| §2.6（新設） | `screener_responses`。`responses` と分ける理由（混ぜると4箇所で除外フィルタが要り、どこか1つ忘れると黙って混ざる）を明記 |
| §3 | `screener.mode` / `questions[].label` / `questions[].premise` |
| §4.2 | **2方式の比較表**と、それぞれの手順に全面改稿 |
| §6.1 | ペルソナカード末尾の前提ブロック |
| §9 | `screener_method` を追加 |
| §10.1 | `persona-sim screen` |
| §13 | 不変条件2件を追加 |

`AGENTS.md` の不変条件にも同じ2件を追加した。

- **確定後に `cell_rank` を振り直してから割り当てる**
- **`assume` のインシデンスを 1.0 と記録しない**

### 2. 実装

| モジュール | 内容 |
|---|---|
| `panel/screening.py`（新規） | 2方式の分岐、通過判定、前提ブロック、セッション組み立て、`screen_job` の再試行 |
| `panel/build.py` | `candidate` の書き出しと `finalize_panel`（**`cell_rank` 振り直し**） |
| `panel/sampling.py` | `cell_sizes` / `role` 引数、role 定数4種 |
| `run/prompt.py` | `PremiseBlock` を `persona_card` → `build_user_message` → `initial_messages` に貫通 |
| `run/session.py` | `SessionContext.questions` / `premises`、`NO_STIMULUS` |
| `run/run.py` | `role='main'` だけを本調査の対象にする（**非通過者に聞かない**）、前提ブロックの読み込み |
| `personas/load.py`（新規） | `run` と `screen` で共用するペルソナ読み込み（片方に置くと循環参照になる） |
| `cli.py` / `metadata.py` / `validate.py` | `screen` コマンド、インシデンス、スクリーナー検証と両方式の見積もり |

### 3. 判断の記録

- **2方式とも前提ブロックに集約した。** `ask` の「回答を持たせ続ける」は会話履歴では
  実現できない（`memory: none` では設問ごとに独立セッション）。カードに載せることで
  `design.memory` の設定によらず効く
- **`premise` は自動生成しない。** 設問文と `pass_if` から自然な日本語を組み立てるのは
  当てにならない。何を前提にしたかは書いた本人の言葉で残す
- **`assume` のインシデンスは `null`。** 1.0 と書くと「インシデンス100%の調査」と誤読される
- **オーバーサンプルが母集団を超えたら E1 ではなく E2 を返す。** 実装中に発覚した。
  30人依頼に対して「480人足りない」（内部のオーバーサンプル数）という報告は分かりにくいので、
  条件に合うペルソナを使い切ったらそこで打ち切り、取れるだけで判定して実インシデンスを示す
- **`screen` の再実行は何もしない。** 確定後は `candidate` 行が無くなるため、
  素朴に実装すると「候補が無い」で落ちる。判定済みなら現状を返す

## 検証結果

### 単体テスト（Spark 不要）— 187 件すべて成功

通過判定（`all` / `any` / `multi` / パース失敗は非通過）、前提ブロックの2形式、
スクリーナーのセッションに提示物が無いこと、`validate` の停止条件
（`pass_if` 範囲外・`open` 型・空の `pass_if`・`assume` の `premise` 欠落）、
`assume` の `oversample_factor` 警告、両方式の見積もり。

### Spark 結合テスト（`-m spark`）— 39 件すべて成功

M4 分の16件に加え、既存の M1〜M3 分も通ることを確認した（`panels` に `cell_rank` を
足したが既存テストへの影響なし）。

- **インシデンスが記録される**（M4 の完了条件）
- **`assume` は LLM を1回も呼ばない**（Fake クライアントの呼び出し 0 回）、
  `screener_responses` テーブル自体が作られない
- **`assume` のインシデンスが `null`**、`screener_method` に方式が入る
- `screened_out` の行が消えない。`main` はちょうど必要数、超過分は `reserve`
- **`disjoint` で `cell_rank` が 0〜29 に振り直され、コンセプトごとの評価者数が各10人ちょうど**
- E2: 母集団を使い切った場合に実インシデンス付きで停止
- `screen` の再実行で聞き直さない（呼び出し 0 回）
- スクリーナーの回答が `responses` に混ざらない
- 本調査の対象が `main` と完全一致する

### 実データ検証（125,000行 / 600人パネル、Fake エンドポイント）

`validate` → `panel` → `screen` → `run` を両方式で通した。

| | `ask` | `assume` |
|---|---|---|
| スクリーニング | 1,800 セッション | **0**（「実行不要」と表示して素通り） |
| インシデンス | **79.7%**（セル別 77.0〜83.3%） | `null` |
| `oversample_actual` | 3.0 | `null` |
| 確定パネル | 6セル各100人 | 6セル各100人 |
| 本調査 | 5,400 レコード | 5,400 レコード |

インシデンス 79.7% は、5択中4つを通過条件にした設定（理論値 80%）と整合している。
`validate` の見積もりは `assume` でも「ask なら 2,400」を併記し、方式選択の材料になっている。

## 未対応事項

- **Databricks Model Serving への実通信は引き続き未検証**（この環境から疎通できない）
- M5 集計・出力、M7 スループット調整は未着手
- スクリーナーの `numeric` / `scale` 型による通過判定は未対応（`single` / `multi` のみ）
- `ask` で「聞くが回答をコンテキストに残さない」指定は用意していない。
  必要になったら足す（いまは `ask` = 聞いて残す、`assume` = 聞かずに与える の2択）
- 2方式の結果差の検証は利用側の分析（`SPEC.md` §1.2 の責務境界の外）
