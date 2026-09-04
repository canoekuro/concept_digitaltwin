# Concept Digital Twin — AIペルソナ・コンセプト調査シミュレータ

LLM で生活者ペルソナ群を仮想再現し、コンセプト受容性の定量アンケートを
シミュレーションする社内システムです。実査の**代替ではなく前段**（仮説生成・
事前スクリーニング・調査票プレテスト）に位置づけます。

`research_system` のコンセプト調査部分を切り出したリポジトリです。JTBD 仮説探索
（`persona_jtbd`）は `research_system` 側に残っています。

- 全体構想・ガバナンス・フェーズ2以降: [SPEC.md](SPEC.md)
- **フェーズ1 構築仕様（実装の直接の根拠）**: [SPEC_PHASE1.md](SPEC_PHASE1.md)

---

## 現在地

**フェーズ1: パイプライン構築**。M7 を除いて実装済み。調査定義から集計表・xlsx までが1本通ります。

| # | マイルストーン | 完了条件 | 状態 |
|---|---|---|---|
| M1 | `personas_base` 構築（取り込み・正規化・派生列） | 属性でクエリできる | **完了** |
| M2 | 調査定義パーサ ＋ `validate` / `panel` | 割り付けどおりのパネルが seed 再現で出る | **完了** |
| M3 | 実行エンジン（single/open、設問ごとの記憶に対応） | 1コンセプト × 100人が完走 | **完了** |
| M4 | スクリーニング機構 | インシデンスが記録される | **完了** |
| M5 | 集計・出力（クロス集計表、xlsx） | `SPEC_PHASE1.md` §7.4 の一式が出る | **完了** |
| M6 | 冪等性・再開、品質フラグ、メタデータ出力 | 中断→再開で結果が一致する | **完了** |
| M7 | 並列化・スループット調整 | 1,000人 × 10コンセプト × 3問が実用時間で完走 | 未着手 |

M1〜M3 で1本通してから M4 以降を足します（`SPEC_PHASE1.md` §12）。M6 は実行エンジンと
書き込み経路を共有するため、M3 と同時に入れました。

---

## 処理の流れ（フェーズ1）

```
調査定義 YAML（サンプル数・割り付け・コンセプト・調査項目）
   ↓
パネル構築（層化抽出 ＋ 必要ならスクリーニング）
   ↓
回答生成（提示設計に従ってペルソナが回答 / Azure AI Foundry・Databricks）
   ↓
集計表出力（トップライン、セグメント別、コンセプト別 + 生データ + 実行メタデータ）
```

CLI（`SPEC_PHASE1.md` §10）:

```bash
persona-sim personas build --shards 1     # personas_base を構築（M1）
persona-sim validate  survey.yaml            # スキーマ検証・抽出可能性の事前チェック
persona-sim panel     survey.yaml            # パネル構築のみ（LLM を呼ばない）
persona-sim screen    survey.yaml            # スクリーニング実行とパネル確定
persona-sim run       survey.yaml            # 回答生成（中断しても同じコマンドで再開）
persona-sim aggregate survey.yaml            # 集計（§7）。クロス集計表・比較表・出力一式
persona-sim export    survey.yaml --xlsx     # 集計をやり直さず aggregates からファイルだけ出す
```

テーブルの置き場所は環境変数で指定します（秘密情報をコードに置かないため）。

```bash
export PERSONA_SIM_WAREHOUSE=/path/to/warehouse       # ローカル実行
# Databricks では代わりに以下
export PERSONA_SIM_CATALOG=<catalog>
export PERSONA_SIM_SCHEMA=<schema>
```

> **調査定義の書き方は [`docs/GUIDE_SURVEY_DEFINITION.md`](docs/GUIDE_SURVEY_DEFINITION.md) に
> まとめています。** 最小の定義から、設問の展開・記憶・スクリーニング・提示設計まで、
> 書き写して使える形で並べています。以下はその要点の抜粋です。

### 提示設計は2軸で指定する

`design` は「誰が何を見るか × どう見せるか」の2軸で書きます（`SPEC_PHASE1.md` §5）。
従来の `design.type`（monadic 等）は廃止しました。

```yaml
design:
  sample_overlap: "same"        # disjoint | allow_overlap | same
  presentation: "sequential"    # sequential | simultaneous
```

**「覚えているか」は `design` にありません。** 記憶は調査全体ではなく設問の性質なので、
`questions[].remember` で設問ごとに指定します（次節）。

既定の `same` × `sequential` は、記憶を持たせなければ**反実仮想モナディック**です。全ペルソナが
全コンセプトを互いに独立したセッションで評価するため、サンプル差による交絡が無く、
順序効果も原理的に発生しません。実査では取れない設計です。
その代わりセッション数がコンセプト数倍になるので、`validate` が見積もりを表示します。

### 設問は提示スロットに紐づき、記憶は設問ごとに指定する

`questions` は全コンセプトに繰り返すテンプレートではありません（`SPEC_PHASE1.md` §3.1）。
各設問が `slot`（そのペルソナが**何番目に見るコンセプト**について聞くか）に紐づき、
1人が評価するコンセプトの数だけ並べます。`measure` はコンセプト横断で同じ問いとして
束ねる集計キーで、コンセプト比較表はこれで組まれます。

```yaml
questions:
  - id: "q_intent_1"
    slot: 1
    measure: "q_intent"
    text: "この商品を購入したいと思いますか。"
    type: "single"
    options: ["ぜひ購入したい", "やや購入したい", "どちらともいえない", "あまり購入したくない", "まったく購入したくない"]
    top_box: [1, 2]
    remember: "none"                 # 記憶を持たずにこの設問へ入る
  - id: "q_novelty_1"
    slot: 1
    measure: "q_novelty"
    text: "この商品は新しいと思いますか。"
    type: "single"
    options: ["とても新しい", "やや新しい", "どちらともいえない", "あまり新しくない", "まったく新しくない"]
    top_box: [1, 2]
    remember: ["q_intent_1"]         # 購入意向の回答を持ったまま聞く
  # slot: 2 / slot: 3 も同じ形で並べる
```

`remember` は **`none` / `all` / 設問IDのリスト**。書かなければ何も覚えません。
「購入意向を踏まえて新規性を聞くが、次のコンセプト提示にはその記憶を持ち込まない」
といった指定を設問ごとに書けます。飛ばし越し（Q4 が Q1 と Q2 だけを覚える）も
書けますが、**列挙どおりで推移しません**——Q4 が Q3 を、Q3 が Q1 を覚えていても、
Q4 が見るのは Q3 だけです。

`all` は「それまでに聞いた設問すべて」で、**コンセプトをまたぎます**。意味はこれ1つで、
他の設定で変わることはありません。同じコンセプトの中だけに絞りたければ設問IDを並べます。

記憶で繋がった設問は同じセッションで実行されるため、`remember` を書くとセッション数
（＝費用）も変わります。`validate` の見積もりに反映されます。

### 対象者条件（スクリーニング）は3方式から選ぶ

`screening.mode` で選びます。**費用と、得られるものが違います。**

| | `ask`（実際に聞く） | `assume`（前提として与える） | `infer`（蓋然性で選ぶ） |
|---|---|---|---|
| 書き方 | `questions`（設問文・選択肢・`pass_if`） | `conditions`（自然言語） | `conditions`（自然言語） |
| 費用 | `panel.size × oversample_factor × 条件数` セッション | **0** | `候補数 ÷ batch_size` 回 |
| インシデンス | **実測できる**（実査と突き合わせられる） | **測れない**。`null` として記録します | **推定値**。実測とは別の欄に記録します |
| パネルの構成 | 割り付けセル内で条件該当者に偏る | 割り付けどおりのまま、行動だけを付与する | `ask` と同じく条件該当者に偏る |
| 内的整合性 | ペルソナ本人の記述と矛盾しない | **ナラティブと矛盾しうる** | ナラティブを読んで選ぶので矛盾しにくい |

どれが正しいというものではなく、**答えている問いが違います**。`ask` は「条件該当者は
どう反応するか」、`assume` は「割り付けどおりの構成の人が、その行動を持つとしたらどう反応するか」、
`infer` は「条件に当てはまりそうな人が、その行動を持つとしたらどう反応するか」。

**方式ごとに書ける形が違います。** `ask` は本人に選択肢を見せて答えさせるので、
`questions` に設問文・選択肢・`pass_if` が要ります。`assume` と `infer` は選択肢を提示
しないので、`conditions` に対象者条件を自然言語で書くだけです。

```yaml
screening:
  mode: "infer"
  conditions:
    - "缶チューハイ・缶ハイボールを月1回以上飲む"
```

書く場所を間違えると、移し方を添えて読み込み時に停止します。黙って無視すると
「条件を書いたのに効いていない」ことに実行後まで気づけないためです。

`infer` は `ask` の費用と `assume` の矛盾の中間を取る方式です。候補を `batch_size` 人ずつ
まとめて判定用の LLM に見せ、条件に合致する蓋然性が高い人にだけ属性を付けます。
判定に使うモデルは `screening.model`、判定プロンプトは `screening.prompt`、
判定に見せるペルソナ情報は `screening.persona_card` で指定できます。

> **`infer` の通過率は推定値です。** 本人には聞いていないので、`ask` の実測値と
> 同じものとして読まないでください。判定結果には `inferred` フラグが立ちます。

どの方式でも、確定したペルソナのカード末尾に前提ブロックが付きます（記憶を持たない設問では
会話履歴で前提を引き継げないため）。見出しで由来が分かるようにしてあります
——【調査前の確認】が本人の回答、【前提】がこちらが与えた条件、【推定前提】が判定で
付与した条件です。

`ask` と `infer` のときだけ `panel` と `run` のあいだに `screen` が入ります。`assume` では
`screen` は「実行不要」と表示して何もしないので、`validate && panel && screen && run`
をそのまま書けます。

### 推論エンドポイント

`model.endpoint` で選びます。認証情報は調査定義に書かず、環境から解決します。

| 値 | 内容 |
|---|---|
| `databricks` | Databricks Model Serving。`model.deployment` にサービングエンドポイント名を書く。認証は `databricks-sdk` の既定の認証チェーン（Databricks 上は環境から、ローカルは `DATABRICKS_HOST` / `DATABRICKS_TOKEN`） |
| `fake` | 決定論的なダミー。**実エンドポイント無しでパイプラインを端から端まで動かせる** |

### 集計と出力

`persona-sim aggregate` が `outputs/{survey_id}/` に一式を書きます（`SPEC_PHASE1.md` §7.4）。

| ファイル | 内容 |
|---|---|
| `crosstab_{stimulus_id}_{measure}.csv` | クロス集計表。表側＝セグメント、表頭＝選択肢 |
| `concept_summary.csv` / `concept_summary_by_segment.csv` | コンセプト比較表（全体 / セグメント別） |
| `open_ends.csv` | 自由回答＋ペルソナの主要属性 |
| `panel_composition.csv` | 実際のパネル構成とインシデンス |
| `responses_raw.csv` | 生データ全件 |
| `report.xlsx` | 上記をシート分けした1ファイル。先頭に「概要」シート |
| `run_metadata.json` | 実行メタデータ（`run` が書く） |

読むときの前提:

- **`n` はウェイト適用前の実数、`%` はウェイト適用後**。ウェイトが全て 1.0 なら一致します
- **平均は選択肢番号を逆順スコア化した値**（5段階なら 1→5点）。順序尺度の設問だけ出します
- **品質フラグの立った回答も集計に含めています**（§8）。除いた場合の n を併記してあります
- CSV / xlsx は人が読む用に整形した値（`12.3%`）です。**機械可読な生の値は `aggregates`
  テーブル**（§2.5）にあります。`export` はそこから表を組み直します
- コンセプト間の**相対比較**として読んでください。統計的推測の指標は算出しません

> **M5 より前に作った `responses` / `screener_responses` は作り直してください。**
> 複数回答を保存するため `answer_codes` 列を足しました。列の無い既存テーブルには
> 追記できません（`persona-sim panel` からやり直すのが確実です）。

> **既存の `runs` テーブルに `survey_name` と `survey_type` を足してください。**
> 調査一覧を出すたびに `metadata_json` を掘るのを避けるため、調査名と調査の種類（§3.0）を
> 列として持つようにしました。列の無い既存テーブルには追記できません。
>
> ```sql
> ALTER TABLE {catalog}.{schema}.runs ADD COLUMNS (survey_name STRING, survey_type STRING);
> ```
>
> 既存行の2列は `NULL` のままで構いません（`metadata_json` から読めます）。
> 作り直しても構いませんが、`runs` は実行履歴なので通常は列追加で足ります。

> **用語と列の変更にともない、既存テーブルはすべて作り直してください。**
> 対象は `responses` / `screener_responses` / `panels` / `runs` / `aggregates` です。
>
> - 「ジョブ」は社内で別の意味を持つ語なので、調査基盤の中心概念は **`survey`／調査** に
>   統一しました。調査定義のトップレベルキーは `job:` → `survey:`、全テーブルの主キー先頭列は
>   `job_id` → `survey_id` に変わります。旧 `job:` の調査定義はエラーで停止し、書き換え方を表示します。
> - `responses` / `screener_responses` の **`answer_code` 列を廃止**し、`answer_codes` に
>   一本化しました。単一回答も1要素の並びとして入ります。選択式で `answer_codes` が
>   空なら、番号を取れなかった（パース失敗）ということです。

### 再現性の範囲

`seed` と各バージョンが揃えば、**誰にどのコンセプトをどの順・どの選択肢順・どのプロンプトで
提示したか**までが再現されます。**生成結果そのものの一致は保証しません**（エンドポイント側の
非決定性は除去できないため）。だから生データに `answer_raw` / `attempt` / `flags` を必ず残します
（`SPEC_PHASE1.md` §9.1）。

---

## ディレクトリ構成

```
concept_digitaltwin/
├── persona_sim/            # 実装本体
│   ├── cli.py              # validate / panel / screen / run / aggregate / export
│   ├── config.py           # 置き場所の解決（環境変数）
│   ├── spark.py            # SparkSession の取得
│   ├── storage/            # Delta テーブルの所在解決と読み書き
│   ├── personas/           # M1: 取り込み・正規化・派生列
│   ├── panel/              # M2/M4: 調査定義・検証・抽出・割り当て・スクリーニング
│   ├── llm/                # 推論クライアント（Databricks / Fake）
│   ├── run/                # M3/M6: セッション組み立て・実行・パース・品質フラグ
│   └── aggregate/          # M5: 集計と §7.4 の出力一式
├── examples/survey_sample.yaml
├── notebooks/quickstart.ipynb            # 環境構築から結果までを1本で通す
├── notebooks/run_survey.ipynb            # Databricks ジョブの本体（アプリから起動）
├── app/                    # コンセプト調査 Web UI（Streamlit / Databricks Apps）
├── app.yaml                # Databricks Apps の起動設定
├── deploy/                 # デプロイ手順
├── SPEC.md                 # 親仕様（全体構想・ガバナンス・フェーズ2以降）
├── SPEC_PHASE1.md          # フェーズ1構築仕様（実装の根拠）
├── AGENTS.md               # プロジェクト固有ガイドライン（SSoT / エージェント共通）
├── CLAUDE.md / GEMINI.md   # 各エージェントの入口。AGENTS.md と .agents/rules/ を読ませる
├── CHANGELOG.md            # docs/history の目次
├── .agents/
│   ├── rules/              # 行動規範（common な6件は retrospective が SSoT）
│   ├── workflows/          # /plan /review /commit 等のワークフロー
│   └── skills/             # git-push / indexing-awareness / knowledge-cutoff-awareness
├── .claude/
│   ├── agents/             # implementer-sonnet / implementer-opus（実装委譲先）
│   ├── commands/           # /pdca /codebase-review
│   ├── hooks/              # auto-open-review.sh（HTML成果物を自動で開く）
│   └── settings.json       # PostToolUse フック登録（共有設定）
├── docs/
│   ├── history/            # 計画(plan)と結果(result)のペア
│   ├── schema/             # データ仕様の調査結果（occupation の分解規則など）
│   └── issues/             # /codebase-review が出す所見ドキュメント
└── tests/                  # pytest
```

---

## セットアップ

### 1. 共通ナレッジ（retrospective）に配線する

初回のみ、[retrospective](https://github.com/canoekuro/retrospective) を導入します。

```bash
git clone https://github.com/canoekuro/retrospective.git ~/src/retrospective
cd ~/src/retrospective && ./scripts/install.sh
```

このリポジトリ側で配線します（冪等。`SessionStart` フックを `.claude/settings.json`
に追記し、共通スキル・サブエージェントを毎セッション同期します）。

```bash
cd /path/to/concept_digitaltwin
~/.claude/shared/scripts/link-repo.sh
```

`.agents/rules/` の共通6ルール（`agent-orchestration` / `git-commit-rules` /
`indexing-codebase` / `language-strategies` / `plan-before-modify` /
`senior-engineer-conduct`）は retrospective の `agents-rules/` が SSoT です。
編集は retrospective 側で行い、こちらへは同期します（同期後に差分をコミット）。

```bash
~/.claude/shared/scripts/sync-agent-rules.sh
```

`review-deliverables.md` は SSoT に無いリポジトリ固有ルールのため、同期対象外です。

### 2. 開発環境

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.11 以上
pip install -r requirements-pipeline.txt -r requirements-ci.txt
ruff check .
python -m pytest tests/ -q -m "not spark"   # Spark 不要の単体テスト
python -m pytest tests/ -q -m spark         # Spark 結合テスト（Java 17/21 が必要）
```

### 3. まず動かしてみる（クイックスタート）

[`notebooks/quickstart.ipynb`](notebooks/quickstart.ipynb) が、環境セットアップから結果までを
1本で通します。**Databricks ノートブックでもローカル Jupyter でも動きます**（先頭で環境を判定して
依存の入れ方とテーブルの置き場所を切り替えます）。

```bash
pip install -r requirements-pipeline.txt -r requirements-notebook.txt
jupyter lab notebooks/quickstart.ipynb
```

既定は `model.endpoint: fake`（決定論的なダミー）なので、**推論エンドポイントが無くても
最後まで通ります**。実際の Databricks Model Serving に繋ぐ手順はノートブック内に書いてあります。

`requirements-ci.txt` だけでも lint と Spark 不要のテストは動きます。Spark 結合テストは
`requirements-pipeline.txt`（pyspark / delta-spark）と JDK が要ります。初回は Delta の JAR を
Maven から取得するため時間がかかります。

依存ファイルは3本に分かれています。**リポジトリ直下の `requirements.txt` は Web UI 用**で、
開発時に入れる必要はありません。Databricks Apps がソースコードパスのルートにある
`requirements.txt` を自動で install する仕様のため、この名前でなければならないだけです
（`deploy/README.md`）。

| ファイル | 用途 |
|---|---|
| `requirements.txt` | Databricks Apps（Web UI）のコンテナ。pyspark を含まない |
| `requirements-pipeline.txt` | CLI・ノートブック・ジョブのクラスタ。pyspark / delta-spark |
| `requirements-ci.txt` | lint と Spark 不要のテスト |

---

## 開発の進め方

- `/pdca` — レビュー → 計画 → 承認 → 実装（サブエージェント委譲）→ 監査 → コミット。
- `/codebase-review` — 読み取り専用の全体点検。所見は `docs/issues/` へ。修正は `/pdca` に引き継ぐ。
- 実装・仕様・設定の変更は `docs/history/` に plan / result をペアで残し、
  `CHANGELOG.md` に1行追記します（`.agents/rules/plan-before-modify.md`）。
- 実装時に絶対に外してはいけない不変条件は [AGENTS.md](AGENTS.md) にまとめています
  （thinking OFF / 順序尺度をシャッフルしない / monadic のセル内均等割り当て /
  `screened_out` を消さない / seed 再現性）。

---

## 出力の取り扱い（必須表示）

> 本ツールの出力はAIによるシミュレーションであり、実在する生活者の回答ではありません。
> 意思決定の根拠として単独で用いず、仮説生成・優先順位付け・調査設計の目的で使用してください。

本システムは NVIDIA が公開する Nemotron-Personas-Japan (CC BY 4.0) を使用しています。
https://huggingface.co/datasets/nvidia/Nemotron-Personas-Japan

帰属表示は CC BY 4.0 の義務です。レポート出力・About 表示から外さないでください（`SPEC.md` §15.3）。
