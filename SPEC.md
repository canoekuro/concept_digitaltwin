# コンセプト調査シミュレータ 構築仕様

| 項目 | 内容 |
|---|---|
| ドキュメント種別 | 構築仕様（SSoT）。**実装の直接の根拠** |
| スコープ | **パイプラインの構築のみ**。精度検証・解釈は利用側で実施 |
| 責務境界 | 本システムは「指定条件どおりにペルソナが回答し、集計表を出す」ところまで |
| 実行環境 | Databricks（Model Serving / Delta Lake / Apps） |
| バージョン | 2.0（`research_system` からの切り出しに伴い、親仕様と構築仕様を1本に統合） |

LLM で生活者ペルソナ群を仮想再現し、**コンセプト受容性の定量アンケート**を
シミュレーションする。実査の代替ではなく前段（仮説生成・事前スクリーニング・
調査票プレテスト）に位置づける。

Web UI の仕様は [`docs/SPEC_UI.md`](docs/SPEC_UI.md)、調査定義の書き方は
[`docs/GUIDE_SURVEY_DEFINITION.md`](docs/GUIDE_SURVEY_DEFINITION.md) にある。

---

## 1. スコープ

### 1.1 やること

```
調査定義（サンプル数・割り付け・コンセプト・調査項目）
   ↓
パネル構築（層化抽出 ＋ 必要ならスクリーニング）
   ↓
回答生成（提示設計に従ってペルソナが回答）
   ↓
集計表出力（トップライン、セグメント別、コンセプト別）
```

### 1.2 やらないこと

- 実査との突合、精度評価、補正係数の適用（利用側の作業）
- レポート生成、示唆出し

Web UI（コンセプト調査に絞った Streamlit アプリ）は**やる**。§10.2 と `docs/SPEC_UI.md` を参照。
ただし UI は調査定義を組み立てて既存の実行経路に渡すだけで、パネル構築・回答生成・集計の
ロジックは持たない。UI から入力できる範囲を超える調査は、調査定義ファイルを書いて
ノートブック（§10.1）から実行する。

ただし**利用側が突合できるだけの情報を必ず出力する**（§9）。生データ、実行条件、パネル構成を欠かさない。

---

## 2. データモデル

Delta Lake 上に以下を持つ。

### 2.1 `personas_base`（不変・共通）

Nemotron-Personas-Japan を正規化したもの。調査間で共有。

| カラム | 型 | 備考 |
|---|---|---|
| `uuid` | string | 主キー |
| `sex` | string | 男 / 女 |
| `age` | int | |
| `age_band_5` / `age_band_10` | string | 派生。20-24 / 20代 など |
| `prefecture` / `region` / `area` | string | |
| `marital_status` / `education_level` | string | |
| `occupation_raw` | string | 元の値 |
| `occupation_industry` / `occupation_scale` / `occupation_role` / `employment_status` | string | 分解済み。`(現在は引退)` は employment_status へ |
| `persona` | string | 総括 |
| `cultural_background` | string | |
| `professional_persona` / `sports_persona` / `arts_persona` / `travel_persona` / `culinary_persona` | string | |
| `skills_and_expertise` / `hobbies_and_interests` | string | |
| `career_goals_and_ambitions` | string | |
| `source_version` | string | データセットのリビジョン |

### 2.2 `panels`

| カラム | 型 | 備考 |
|---|---|---|
| `survey_id` | string | |
| `persona_uuid` | string | |
| `cell_id` | string | 割り付けセル（例: `M_20s`） |
| `role` | string | `candidate` / `main` / `reserve` / `screened_out`（§4.2） |
| `cell_rank` | int | セル内の抽出順（0始まり）。コンセプト割り当ての起点。スクリーニングの先着確定にも使う |
| `assigned_stimuli` | array\<string\> | 提示設計に従って割り当てたコンセプトID（順序を保持） |
| `weight` | double | 既定 1.0 |

### 2.3 `responses`（生データ）

| カラム | 型 | 備考 |
|---|---|---|
| `survey_id` / `persona_uuid` / `stimulus_id` / `question_id` | string | 複合キー |
| `sequence` | int | 提示順（何番目に見たコンセプトか） |
| `answer_raw` | string | モデル出力そのまま |
| `answer_codes` | array\<int\> | 選択された選択肢番号の並び。**実際に提示した順での番号**。`single`/`scale` は1要素、`multi` は選んだぶん全部、`open`/`numeric` は空。選択式で空ならパース失敗 |
| `answer_text` | string | 自由回答 |
| `answer_reasoning` | string | `reasoning: true` の設問で、モデルが書いた理由（§6.3）。それ以外は null |
| `options_order` | array\<int\> | 実際に提示した選択肢順（定義順での番号を提示順に並べたもの） |
| `flags` | array\<string\> | §8 |
| `latency_ms` / `input_tokens` / `output_tokens` | int | |
| `attempt` | int | リトライ回数 |
| `ts` | timestamp | |

**`answer_codes` と `options_order` の番号空間に注意。** 番号はモデルに見せた順であり、
`options_order` はその提示順を「定義順での番号」で表したもの。定義順に戻すには
`options_order[code - 1]` を引く。シャッフルしていない設問では両者は一致する。
読み替えは集計側（§7）で行い、生データは**モデルが実際に見た通り**に保存する。

単一回答も並びとして持つ。単一回答用の射影列を別に置くと、`multi` で必ず null になる列が
増えるうえ、集計経路が2つに割れて片方だけ直す事故が起きる。

### 2.4 `runs`（実行メタデータ）

| カラム | 型 | 備考 |
|---|---|---|
| `survey_id` | string | |
| `survey_name` | string | 調査名。一覧に出すのに毎回 JSON を掘らずに済むよう列で持つ |
| `survey_type` | string | 調査の種類（§3.0）。種別で絞れるよう列で持つ |
| `run_id` | string | `{survey_id}-{開始時刻}`。乱数を使わない |
| `started_at` / `finished_at` | timestamp | |
| `sessions_total` / `sessions_ok` / `sessions_failed` / `sessions_skipped` | int | |
| `aborted_reason` | string | E5 で中断した場合の理由 |
| `metadata_json` | string | §9 の全文（調査定義の全文・モデル設定・各テーブルの Delta バージョンを含む） |

**行が追加されるのは実行が完走したとき。** 走行中・失敗した調査の行は残らない。

`survey_name` / `survey_type` は `metadata_json` にも入っているが、列としても持つ。
一覧を出すたびに全件の JSON を掘るのは無駄が大きく、種別で絞ることもできないため。

**集計結果はテーブルに保存しない。** 読むたびに `responses` から数え直す（§7）。
保存した表と生データが食い違ったとき、どちらが正しいのかを決める手立てが無いため。

### 2.5 `screener_responses`

スクリーニング（§4.2）の生データ。**`responses` とは分けて持つ。**
混ぜると、本調査のフラグ率・straightline 判定・集計のすべてで除外フィルタが必要になり、
どこか1箇所で忘れると黙って結果に混ざる。

| カラム | 型 | 備考 |
|---|---|---|
| `survey_id` / `persona_uuid` / `question_id` | string | 複合キー。コンセプトに紐づかないので `stimulus_id` は持たない。`infer` は判定1回＝1行なので `question_id` は予約値 `_infer` |
| `answer_raw` | string | モデル出力そのまま |
| `answer_codes` | array\<int\> | 選択された選択肢番号の並び（提示順）。通過判定はこれを見る。空ならパース失敗（＝非通過扱い） |
| `answer_text` | string | 自由回答（スクリーナーでは通常 null） |
| `options_order` | array\<int\> | 実際に提示した選択肢順 |
| `flags` | array\<string\> | §8 |
| `latency_ms` / `input_tokens` / `output_tokens` | int | |
| `attempt` | int | リトライ回数 |
| `ts` | timestamp | |
| `config_hash` | string | その判定を得たときのスクリーニング設定の指紋（§4.2） |

通過したかどうかはここに持たない。判定結果の唯一の置き場所は `panels.role`（§2.2）。

`config_hash` は**判定を再利用してよいかの判断にだけ使う**。判定済みの候補は聞き直さない
仕組みなので、どの設定で得た判定かを行が持っていないと、プロンプトやモデルを変えても
古い判定がそのまま使われる（「判定を直したのに結果が1件も変わらない」として現れる）。
指紋が現在の設定と一致しない行は未判定として扱い、聞き直す。倍率（`oversample_factor`）は
判定の中身を変えないので指紋に含めない — 含めると通過者不足の再試行のたびに全件を
聞き直すことになる。

`infer` は条件ごとではなく**候補1人につき1回**判定するため、条件の数によらず1人1行になる。
`question_id` は予約値 `_infer`、`answer_codes` は通過なら `[1]`・非通過なら空、
`options_order` は空（選択肢を提示していない）。`flags` に `inferred` が必ず立つ。

---

## 3. 調査定義スキーマ

YAML（または JSON）で1ファイル。これがシステムへの唯一の入力。

**この節はフィールドの網羅的な一覧。** 書き方の手引きは
[`docs/GUIDE_SURVEY_DEFINITION.md`](docs/GUIDE_SURVEY_DEFINITION.md) にある
（最小の定義から順に足していく形で、実例は自動検証している）。

```yaml
survey:
  id: "cs_2026_0801_rtd_a"
  name: "RTD新コンセプト評価"
  type: "concept"          # 調査の種類。省略時は concept

panel:
  size: 1000
  seed: 42
  quotas:
    mode: "count"            # count | proportion
    cells:
      - {cell_id: "M_20s", sex: "男", age_min: 20, age_max: 29, n: 100}
      - {cell_id: "M_30s", sex: "男", age_min: 30, age_max: 39, n: 100}
      - {cell_id: "F_20s", sex: "女", age_min: 20, age_max: 29, n: 100}
      # ...
  filters:                   # 割り付け以外の固定条件（任意）
    prefecture_in: null
    age_min: 20              # 酒類なら必須

screening:                     # 任意。省略すればスクリーニングを行わない。§4.2
  # 並びは本調査（main_survey）と対称に model → prompt → persona_card → その他。
  model:                       # 省略したキーは main_survey.model を引き継ぐ
    deployment: "databricks-gemini-3-5-flash-lite"
    max_tokens: 256
  prompt:                      # 判定プロンプト
    system: "あなたは調査対象者の選定を担当します。…"
    rule: "上記の条件に当てはまる蓋然性が高い人物の番号を、カンマ区切りですべて挙げてください。…"
  persona_card:                # 判定に見せる情報（§4.2）
    attributes:
      - {field: "sex"}
      - {field: "age", suffix: "歳"}
      - {field: "prefecture", suffix: "在住"}
    include_summary: true
    persona_fields:
      - {field: "hobbies_and_interests", label: "趣味・関心事"}
  oversample_factor: 4         # 候補の抽出倍率
  batch_size: 20               # 1回の判定に渡すペルソナ数
  conditions:                  # 対象者条件。自然言語で書く（選択肢や pass_if は書けない）
    - "缶チューハイ・缶ハイボールを月1回以上飲む"

stimuli:
  - id: "c1"
    name: "コンセプトA"
    text: |
      【商品名】...
      【特徴】...
      【価格】...
    image_uri: null          # 画像がある場合は Unity Catalog Volumes のパス
                             # （/Volumes/{catalog}/{schema}/{volume}/...）
    image_mode: "native"     # native（マルチモーダル）| text（文章化して渡す）| none
  - id: "c2"
    # ...

# 提示設計のブロックは無い。反実仮想モナディック固定で、設定では変えられない（§5）。

questions:                     # 設問。slot ごとに展開する（下記参照）
  - id: "q_intent_1"
    slot: 1                    # 1番目に提示するコンセプトについて聞く
    measure: "q_intent"        # コンセプト横断で同じ問いとして束ねるキー
    system: "purchase_intent"  # 任意。main_survey.prompt.systems の名前（§6.1）
    text: "この商品を購入したいと思いますか。"
    type: "single"
    options:
      - "ぜひ購入したい"
      - "やや購入したい"
      - "どちらともいえない"
      - "あまり購入したくない"
      - "まったく購入したくない"
    randomize_options: false   # 順序尺度は固定
    top_box: [1, 2]            # T2B の定義
    remember: "none"           # 記憶を持たずにこの設問へ入る（§5.1）
  - id: "q_novelty_1"
    slot: 1
    measure: "q_novelty"
    system: "novelty"          # 購入意向とは別の [system] で聞く
    text: "この商品は新しいと思いますか。"
    type: "single"
    options: ["とても新しい", "やや新しい", "どちらともいえない", "あまり新しくない", "まったく新しくない"]
    randomize_options: false
    top_box: [1, 2]
    remember: ["q_intent_1"]   # 購入意向の回答を持ったまま聞く
  - id: "q_reason_1"
    slot: 1
    measure: "q_reason"
    text: "そう思った理由を教えてください。"
    type: "open"
    max_length: 150
    remember: ["q_intent_1", "q_novelty_1"]
  # 2番目・3番目に提示するコンセプトぶんも同じ形で並べる（slot: 2 / slot: 3）


#### 設問は提示スロットに紐づく

**`questions` はコンセプトごとに繰り返されるテンプレートではない。** 各設問は
`slot`（そのペルソナが何番目に見るコンセプトについて聞くか、1始まり）に紐づき、
1ペルソナが評価するコンセプト数 m のぶんだけ設問を並べる。実行時は
`panels.assigned_stimuli[slot - 1]` を引く。**コンセプトIDではなく提示順の位置**なので、
パネルの割り当てが変わっても設問側を書き換えずに済む。

| フィールド | 既定 | 意味 |
|---|---|---|
| `slot` | `1` | 何番目に提示するコンセプトについて聞くか。`1〜m` を過不足なく埋めること（E6） |
| `measure` | `id` と同じ | コンセプト横断で「同じ問い」として束ねるキー（§8） |
| `remember` | `none`（持たない） | どの設問の回答を持ったままこの設問に入るか（§5.1） |
| `system` | なし（`prompt.system`） | この設問を聞くときの `[system]`。`main_survey.prompt.systems` の**名前**（§6.1） |
| `reasoning` | `false` | 構造化出力に `reasoning` を足し、理由を書かせてから番号を選ばせる（§6.3）。`single` / `scale` / `multi` のみ |
| `reasoning_max_length` | `80` | 理由の字数。`prompt.rules.reasoning.{設問タイプ}` の `{max_length}` に入る |

こうしているのは、**設問IDを実行時にも一意にする**ため。`remember` は設問IDで参照するので、
同じIDが1人の回答履歴に何度も現れると、どれを指すのか決まらない。

代償として、同じ問いがコンセプトの数だけ別IDに分かれる。集計表（§7.1）は「どの設問と
どの設問が同じ問いか」を知る必要があるので、`measure` を集計の同一性キーにする。
省略すると `id` 単位になり、コンセプト比較がばらける。

**`slot` が `1〜m` を覆っていない調査定義は読まずに停止する。** 抜けがあると、その順番に
提示されたコンセプトが聞かれずに消え、集計から黙って落ちる。

main_survey:                 # 本調査（回答生成）
  model:
    endpoint: "databricks"   # databricks | fake（azure_ai_foundry は未実装）
    deployment: "databricks-gemini-3-5-flash-lite"
    thinking: false          # 必ず false
    max_tokens: 8            # open設問のみ別途上書き
    max_tokens_open: 512     # type: open の設問で使う
    max_tokens_reasoning: 512  # reasoning: true の設問で使う（§6.3）
    concurrency: 32
    structured_output: "auto"  # auto | always | never（§6.3）

  prompt:                    # 任意。省略時は §6.1 の既定
    system: "あなたはこれから提示する人物になりきって、調査に回答します。…"
    systems:                 # 任意。設問ごとに使い分ける [system]（§6.1）
      purchase_intent: "…実際に手に取るかどうかで判断してください…"
      novelty: "…すでに知っている商品との違いで判断してください…"
    headings: {...}          # 各ブロックの見出し（§6.1）
    rules:                   # 回答指示文（設問タイプごと）
      single: "番号のみで答えてください。"
      reasoning:             # reasoning: true の設問で、上の指示行を置き換える表（§6.3）
        single: "まず、そう考えた理由を{max_length}文字以内で書き、…番号を1つ選んでください。"
        multi: "まず、そう考えた理由を{max_length}文字以内で書き、…番号をすべて挙げてください。"

  persona_card:              # 任意。省略時は §6.1 の既定
    attributes:              # 属性行。載せたい属性だけを並べる（[] なら属性行なし）
      - {field: "sex"}
      - {field: "age", suffix: "歳"}
      - {field: "prefecture", suffix: "在住"}
      - {field: "marital_status"}
      - {field: "education_level"}
      - {field: "occupation_raw"}
    include_summary: true    # persona（総括文）を載せるか
    persona_fields:          # ペルソナカードに載せるナラティブ列と見出し
      - {field: "cultural_background",  label: "生活背景"}
      - {field: "professional_persona", label: "仕事"}
      - {field: "hobbies_and_interests", label: "関心事"}
      - {field: "culinary_persona",     label: "食まわり"}

output:
  segments: ["total", "sex", "age_band_10", "sex_x_age_band_10", "cell_id"]
  formats: ["delta", "csv", "xlsx"]
```

### 3.0 調査の種類

`survey.type` に何を聞く調査かを書く。省略時は `concept`。

| 値 | 内容 |
|---|---|
| `concept` | コンセプト調査。案を提示して購入意向・新規性などを聞く |

種別は `runs` にも列として残す（§2.4）。**実行後にその調査が何だったのかを判別する
手がかりが要る**ため。列挙値なので、書き間違えると候補を添えて停止する。
種別を増やすときは `persona_sim.panel.schema.SurveyType` に1行足す。

### 3.1 設問タイプ

対応するもの：`single` / `multi` / `scale`(5,7,10) / `open` / `numeric`
非対応：`rank` / `maxdiff` / コンジョイント

---

## 4. パネル構築

### 4.1 層化抽出

```
確定済み = {}
for cell in quotas.cells:            # 調査定義の記述順に処理する
    候補 = personas_base.filter(cell条件 AND panel.filters)
    候補 = 候補 - 確定済み            # セル間の重複を除外（anti-join）
    if len(候補) < 必要数:
        エラーで停止（§11 E1）
    候補を rank_key 昇順に並べ、先頭から必要数を取る
      rank_key = sha2(concat_ws('|', uuid, str(seed), cell_id), 256)
    確定済み |= 抽出
```

- **乱数によるサンプリングを使わない。** 分散処理ではパーティション数や並列度で結果が変わり、
  再現性が壊れる。上記のハッシュ順ランクなら実行環境によらず同じ集合が得られる
- `seed` 固定で完全に再現可能であること
- セル間で重複抽出しない（同一 uuid が複数セルに入らない）。これは §5.0 の大前提を守るためであり、
  設定で無効化できない。セル条件が重なっていて除外が発動した場合は警告する
- 除外の結果として後続セルが不足しうるため、**E1 の判定は除外を適用した後の候補数で行う**
- 抽出後の実構成を `panels` に記録

### 4.2 スクリーニング

対象者条件は**自然言語の `conditions` で書く**。判定用 LLM に候補をまとめて見せ、条件に
合致する蓋然性が高い者にだけ属性を与える。方式は**これ1つだけ**で、設定では選べない。

| | 内容 |
|---|---|
| 書き方 | `conditions`（自然言語） |
| やること | 候補を `batch_size` 人ずつ判定用 LLM に見せ、合致する蓋然性が高い番号を返させる |
| 費用 | `候補数 ÷ batch_size` 回（条件の数には比例しない） |
| インシデンス | **推定値**。§9 では `screener_incidence_estimated` に記録する |
| パネルの構成 | 割り付けセル内で条件該当者に偏る（例: ビール飲用者は男性寄り） |
| 内的整合性 | ナラティブを読んで選ぶので矛盾しにくい |

**答えている問いは「条件に当てはまりそうな人が、その行動を持つとしたらどう反応するか」。**
本人に聞いたわけではないので、通過率を実測値として扱ってはいけない。判定結果には
`inferred` フラグを必ず立て、実回答と区別できるようにする（§9）。

**選択肢を見せて本人に答えさせる方式は持たない。** `questions` / `options` / `pass_if` は
書く場所が無く、書けば未知のキーとして停止する。実測インシデンスが要る調査は、この
システムの範囲外（実査で取る）。

`screening:` を書かなければスクリーニングを行わない。この場合の通過率は 1.0 ではなく
**未測定**（`null`）として記録する（§9）。

#### 手順

```
1. panel: 各セルで 必要数 × oversample_factor を抽出し、role='candidate' で保存
2. screen: 候補を batch_size 人ずつまとめて判定用 LLM に渡し、条件合致の蓋然性が
   高い番号を返させる（screener_responses へ。flags に inferred を立てる）
3. 通過者から必要数を先着（抽出順）で確定し role='main'、残りの通過者は role='reserve'
4. 非通過は role='screened_out' として保持（削除しない）
5. 確定した main だけで cell_rank を振り直す
6. 通過率をセル別・全体で算出し、推定値として記録（§9）
```

- **手順5を省略してはいけない。** オーバーサンプルした順位のまま残すと、非通過で空いた
  穴がそのまま `cell_rank` の飛びになり、「セル内の何番目に選ばれた人か」がパネルの記録
  から読めなくなる
- あるセルで通過者が必要数に満たない場合、`oversample_factor` を自動的に2倍にして最大3回まで再試行。
  それでも不足ならエラー（§11 E2）。抽出はハッシュ順なので、**再試行は同じ並びの先を伸ばすだけ**であり、
  判定済みの候補を判定し直すことはない
- **ただし到達不能と分かった時点で打ち切る。** 再試行は「通過率はあるが揺らいだ」ときのための
  仕組みで、通過率そのものが構造的に低い場合には効かない。候補を倍にしても通過率は変わらず
  必要数には届かないので、判定の呼び出し費用だけが増える。各試行の後に
  `実測通過率 × 上限倍率 >= 1` を確かめ、満たさないセルがあれば上限まで試さずに
  §11 E2 で停止し、**必要な候補数の見積もりを添える**（通過0なら「何倍にしても届かない」）
- 判定済みかどうかは `screener_responses.config_hash`（§2.5）が現在の設定と一致する行だけで見る。
  プロンプト・モデル・ペルソナカード・条件を変えれば自動的に判定し直しになる。変えていないのに
  判定を引き直したい場合は `screen_survey(..., force=True)`
- **`screened_out` のレコードも消さない。** 通過率の検証に使う

#### 判定の設定

判定の指示は `screening.prompt.rule` **だけ**が出す。条件をすべて満たす必要があるのか、
いずれかで足りるのかも、その文中に書く。仕組み側で「すべての条件を満たす人物を選んで
ください」のような文を足してはいけない — 設定から触れない文が `rule` の直前という最も
効く位置に入り、`prompt` をどう書き換えても判定が動かなくなる。

判定に渡す情報は `screening.persona_card` で決める。回答生成用のペルソナカードをそのまま
使うと1回のプロンプトが長くなり、この方式の費用面の利点が消えるため、既定では
属性行と総括だけを見せる（ナラティブ列は載せない）。

```yaml
screening:
  model:                             # 省略したキーは main_survey.model を引き継ぐ
    deployment: "databricks-gemini-3-5-flash-lite"
    max_tokens: 256                  # 番号列を返すので回答生成より多めに要る
  prompt:
    system: "あなたは調査対象者の選定を担当します。…"   # 判定の [system]
    rule: "…番号を、カンマ区切りですべて挙げてください。…"  # 判定の指示行
  persona_card:                      # 判定に見せる情報
    attributes:                      # 属性行。載せたい属性だけを並べる（[] なら属性行なし）
      - {field: "sex"}
      - {field: "age", suffix: "歳"}
      - {field: "prefecture", suffix: "在住"}
    include_summary: true            # persona（総括文）を載せるか
    persona_fields:                  # 判定に見せるナラティブ列（既定は載せない）
      - {field: "culinary_persona", label: "食まわり"}
  oversample_factor: 2
  batch_size: 20                     # 1回の判定に渡すペルソナ数
  conditions:                        # 対象者条件。自然言語で書く
    - "ビールを月1回以上飲む"
```

**判定のプロンプトは本調査と分けて持つ。** `screening.prompt.system` と
`screening.prompt.rule` は、本調査の `main_survey.prompt.system` / `rules` とは別物。
回答生成は「なりきらせる」が、判定は「外から見て判断させる」ので、同じ block に
置くとどちらを直したのか読み取れなくなる。

判定モデルの `thinking` は指定できない。常に OFF が不変条件のため（§13）。
判定条件（モデル・`batch_size`・見せた列）は実行メタデータに残す。誰をどう判定したかまで
残さないと入力を再現できない（§9.1）。

#### 前提の引き継ぎ

確定したペルソナのカードに前提ブロックを付ける（§6.1）。記憶を持たない設問は独立した
セッションになるため、**会話履歴では前提を引き継げない**。カードに載せることで、設問が
どう記憶を持つかによらず効く。見出し【推定前提】が、本人が答えたのでも一律に与えたので
もなく判定で付与した条件だという由来を残す。

### 4.3 ウェイト

割り付けどおりに抽出できていれば `weight = 1.0`。

スクリーニング後に構成が崩れた場合、または `quotas.mode: proportion` で端数が出た場合のみ、セル別に `weight = 目標比率 / 実比率` を計算する。

---

## 5. 提示設計と割り当て

**提示設計は反実仮想モナディック固定。設定では変えられない。** 全ペルソナが全コンセプトを
定義順に1件ずつ、互いに独立したセッションで評価する。調査定義に `design:` ブロックは無い。

1ペルソナが何を覚えているかだけは設問ごとに指定できる（§5.1）。

### 5.0 大前提：同一ペルソナは同一コンセプトに1回しか回答しない

同じ人が同じコンセプトを2回評価することは起きない。実装は常に次の2つを守る。

- パネル内で `persona_uuid` は一意（1ペルソナは1回だけ登場する）
- `assigned_stimuli` は重複の無い集合（順序は保持する）

これにより `responses` の複合キー（§2.3）と冪等 upsert（§6.4）がそのまま成立する。

### 5.1 記憶は設問ごとに指定する

**提示設計そのものに設定項目は無い**が、「覚えているか」だけは別。記憶は調査全体の性質では
なく**設問の性質**なので、`questions[].remember` が持つ。「この設問は引き継ぐ／この設問は
引き継がない」は調査ごとではなく設問ごとに変わる。

**保持の単位は設問。** 各設問の `remember` が「どの設問の Q&A を持ったままこの設問に
入るか」を決める。参照は設問IDで書き、実行時にもIDは一意なので曖昧さがない。

```yaml
- id: "q_novelty_1"
  remember: ["q_intent_1"]           # 購入意向の回答を持って新規性を聞く
- id: "q_intent_2"
  remember: "none"                   # 別の案の提示。前の案の記憶は持ち込まない
- id: "q_reason_2"
  remember: ["q_intent_1", "q_intent_2"]   # 非連続・案またぎも書ける
```

書ける値は3つ。**意味は調査定義の中だけで完結する**——他の設定を見て変わることはない。

- `none`: 何も持たない（既定。書かなければこれ）
- `all`: それまでに聞いた設問すべて。**コンセプトをまたぐ**
- 設問IDのリスト: 列挙した設問だけ

**「同じコンセプトの中だけ」に絞る専用の値は用意しない。** 設問IDを並べて書けば済むうえ、
`all` の意味をコンセプト調査の都合で変えると、同じ `all` が文脈によって別のものを指す。
以前は調査全体の設定で `all` の範囲が切り替わり、既定の組み合わせでは範囲が未定義のまま
「またぐ」側に倒れていた——反実仮想モナディックの前提（案どうしが独立）が壊れるのに、
`validate` は問題なしと言っていた。

**列挙どおりで推移しない。** Q4 が Q3 を、Q3 が Q1 を覚えていても、Q4 が見るのは Q3 だけ。
Q1 も要るなら Q4 に両方書く。プロンプトに何が載るかを調査定義から直読できるようにするため。

再生する順は書いた順ではなく**聞いた順**（`slot` 昇順・記述順）。会話履歴として時系列が
狂わないようにする。まだ答えていない設問（ask order で自分より後ろ）は参照できない（E6）。

#### セッションの粒度は記憶から決まる

「設問 A が設問 B を覚えている」なら、B の回答が出てからでないと A を聞けない。
だから A と B は同じセッションに入る。この連結成分が実行単位であり再開単位でもある。

従来の3つの形は次のように書き分ける。

| やりたいこと | 書き方 | できるセッション |
|---|---|---|
| 記憶を持たせない | 何も書かない | 設問ごとに1つ |
| 同じコンセプトの中だけ覚える | 同じ `slot` の先行設問IDを並べる | コンセプトごとに1つ |
| 全部覚える | 全設問に `remember: all` | ペルソナごとに1つ |

記憶を持たない設問は依存を持たないので単独のセッションになり、並列度が落ちない。

### 5.2 反実仮想モナディック

全ペルソナが全コンセプトを、互いに独立したセッションで評価する。実査では不可能で、
LLM でのみ成立する設計である。

- サンプル差による交絡が無い（同一人物どうしの比較になる）
- 順序効果・学習効果が原理的に発生しない（記憶を持たせないかぎり）
- 有効サンプル数が `panel.size` のまま落ちない

引き換えに**セッション数がコンセプト数倍**になる。コストに直結するため、検証は
セッション数と有効サンプル数の見積もりを必ず出力する（§10.1）。

割り当ては「定義順のコンセプトIDの配列をそのまま `assigned_stimuli` に置く」だけになる。
コンセプトごとの評価者数は全員が全案を見るので定義から等しく、重複が無いことも配列の形
から自明に成り立つ（§5.0）。

`sequence` カラム（何番目に見たか）は記憶の持たせ方によらず記録する。

### 5.3 選べるようにしない理由

「一部の人にだけ見せる」「全案を同時に見せる」「提示順を入れ替える」を設定で選べるように
すると、次の2つが同時に戻ってくる。

- **コンセプト間でセグメント構成がずれる経路**。1人が一部の案しか見ない設計では、セル内で
  誰にどの案を配るかで構成が変わる。均等割り当てを1箇所でも省くと崩れる
- **順序効果が入る経路**。同時提示や提示順の入れ替えは、比較・記憶を前提とした設計であり、
  案どうしが独立という前提が成り立たなくなる

どちらも「設定を1つ変えただけ」で結果の意味が変わり、集計表からは見分けがつかない。
実装から選択肢ごと外しておく。

### 5.4 画像の扱い

| `image_mode` | 動作 |
|---|---|
| `native` | 画像をそのままモデルに渡す。マルチモーダル対応デプロイが必要 |
| `text` | 事前に画像内容を文章化したものを `text` に連結して渡す |
| `none` | 画像を使わない |

どのモードで実行したかを `runs` に記録する。

---

## 6. 実行エンジン

### 6.1 プロンプト組み立て

```
[system]
あなたはこれから提示する人物になりきって、調査に回答します。
- この人物の実際の生活実感に即して答えてください
- 調査に協力的すぎる態度をとらないでください。
  興味のない対象には率直に興味がないと答えてください
- 指定された形式のみで回答し、説明や前置きは書かないでください

[user]
■あなたのプロフィール
{ペルソナカード}

■提示物
{コンセプト文}

■設問
{設問文}
1. {選択肢1}
2. {選択肢2}
...

番号のみで答えてください。
```

**ペルソナカードの構成**（順序固定）：

```
{sex}・{age}歳・{prefecture}在住・{marital_status}・{education_level}・{occupation_raw}

{persona}

【生活背景】{cultural_background}
【仕事】{professional_persona}
【関心事】{hobbies_and_interests}
【食まわり】{culinary_persona}
```

**3ブロックすべて調査定義の `main_survey.persona_card` で差し替えられる。**
省略時の既定は上記（属性行6項目・総括あり・ナラティブ列4つ）。

| キー | 何を決めるか |
|---|---|
| `attributes` | 1行目の属性行。載せたい属性を並べる。`[]` にすれば属性行そのものを出さない |
| `include_summary` | `persona`（総括）を載せるか |
| `persona_fields` | 【見出し】付きブロック。カテゴリに応じて差し替える |

```yaml
main_survey:
  persona_card:
    attributes:
      - {field: "sex"}
      - {field: "age", suffix: "歳"}
      - {field: "region", suffix: "地方"}   # 都道府県より粗い粒度で見せたいなら
    include_summary: true
    persona_fields:
      - {field: "cultural_background", label: "生活背景"}
      - {field: "sports_persona",      label: "運動習慣"}   # スポーツ用品のカテゴリなら
```

`attributes` の `suffix` は値の後ろに付く文字列。値だけでは何の数字か読めない項目
（`age` の「歳」、`prefecture` の「在住」）のために設定側に持たせている。
`field` には `personas_base` の列（§2.1）を指定する。無い列を書いたら
`validate` が停止させる（実行時に黙って空になると、その項目が抜けたカードで聞いてしまう）。

**値が空の項目・ブロックは行ごと落とす。** 属性行も総括も出さない設定なら空行も出さない。
空行だけが残ると、何かを載せ忘れたようにプロンプトが読める。

スクリーニングの判定カードも同じ形で設定するが、**別に持つ**
（`screening.persona_card`、§4.2）。回答生成用をそのまま流用すると判定プロンプトが
長くなり、この方式の費用面の利点が消える。どの属性を載せるかの指定だけを共通の仕組みにし、
整形（判定側は1人1行に詰める）は分けてある。

**`[system]`・見出し・指示行も調査定義から差し替えられる。** 省略したキーは既定のまま残る。

| キー | 既定 | 用途 |
|---|---|---|
| `system` | 上記の `[system]` 全文 | なりきりの指示。`systems` を指さない設問はこれで聞く |
| `systems` | `{}` | 設問ごとに使い分ける `[system]` を名前で並べたもの（下記） |
| `headings.profile` | `■あなたのプロフィール` | ペルソナカードの見出し |
| `headings.stimulus` | `■提示物` | コンセプトの見出し |
| `headings.question` | `■設問` | 設問の見出し |
| `headings.ask_premise` | `調査前の確認` | `ask` の前提ブロック見出し |
| `headings.assume_premise` | `前提` | `assume` の前提ブロック見出し |
| `rules.single` / `rules.scale` | `番号のみで答えてください。` | 単一回答の指示行 |
| `rules.multi` | `当てはまる番号をすべて、カンマ区切りで答えてください。` | 複数回答の指示行 |
| `rules.open` | `{max_length}文字以内で答えてください。` | 自由回答の指示行 |
| `rules.numeric` | `数値のみで答えてください。` | 数値回答の指示行 |
| `rules.reasoning.single` / `.scale` / `.multi` | §6.3 | `reasoning: true` の設問で、同じ設問タイプの指示行を置き換える |

`rules` の**直下**は設問タイプごとの回答指示文だけを持つ。スクリーニング判定の指示行は
`screening.prompt.rule`（§4.2）にある。設問タイプではないものを並べると
`for_type()` から引けるように見えてしまう。`reasoning` が入れ子なのもこのためで、
設問タイプではなく**設問タイプの指示行を差し替える表**である（§6.3）。

`rules.open` と `rules.reasoning.*` だけ `{max_length}` を差し込める（`open` は設問の
`max_length`、`reasoning` は `reasoning_max_length`）。`open` は設問に `max_length` が
無ければ指示行そのものを出さない。他のキーに差し込みを書くと**読み込み時に停止する**
（実行時まで気づけないため）。

```yaml
main_survey:
  prompt:
    system: |
      あなたはこれから提示する人物になりきって、調査に回答します。
      - 指定された形式のみで回答してください
    headings:
      question: "●質問"
    rules:
      single: "当てはまる番号を1つだけ答えてください。"
```

`headings` の前提ブロック見出し（`ask_premise` / `assume_premise` / `infer_premise`）は
スクリーニング由来の名前だが、描画されるのは本調査のペルソナカード末尾なので
`main_survey.prompt` に置く。

#### 設問ごとに `[system]` を使い分ける

聞いていることの性質が設問で違うなら、`[system]` も分けられる。購入意向は
「自分の金で買うか」を、新規性は「既存品と何が違うか」を判断させる問いで、1本の指示で
兼ねるより書き分けたほうが精度が高い。

`main_survey.prompt.systems` に**名前を付けて**並べ、設問側の `system` でその名前を指す。

```yaml
main_survey:
  prompt:
    system: |                      # systems を指さない設問はこれで聞く
      あなたはこれから提示する人物になりきって、調査に回答します。
    systems:
      purchase_intent: |
        …この人物のふだんの買い物の仕方・支払える金額に照らして判断してください
      novelty: |
        …すでに知っている商品と引き比べて、どこが違うのかで判断してください

questions:
  - id: "q_intent_1"
    system: "purchase_intent"
  - id: "q_novelty_1"
    system: "novelty"
    remember: ["q_intent_1"]       # 記憶は跨いでよい（下記）
```

**設問には文面ではなく名前を書く。** 設問はコンセプトの数だけ `slot` 展開される（§3）ので、
文面を直書きすると同じ長文が展開数ぶん複製され、直すときに1箇所でも取りこぼすと
案によって違うプロンプトで聞いたことになる。

**`systems` に無い名前を指したら読み込みで停止する。** 綴り違いを既定へ黙って落とすと、
書き分けたつもりの設問が既定の `[system]` で聞かれ、実行後に `prompt_sample.md` を
読み比べるまで気づけない。逆に、**どの設問からも指されていない `systems`** は
`validate` が `W_UNUSED_SYSTEM_PROMPT` として警告する（設問側の書き忘れが多い）。

**記憶（`remember`、§5.1）を跨いでもよい。** その場合の `[system]` は
**いま聞いている設問のもの**。メッセージ列は毎ターン組み直す方式（`replayed_messages()`）
なので、先行設問の回答は履歴として残ったまま、これから答えさせる設問の指示で聞ける。
先行設問のものを使うと、答えさせたい設問が別の指示で聞かれることになる。

`template_version` によるプロンプトの版管理は廃止した。理由: 生成AIの回答には再現性が無く、
版による比較が成立しにくいこと。利用者がプロンプトを自由に書き換える運用を想定していること。

**スクリーナーがある場合は、カード末尾に前提ブロックを足す**（§4.2）。見出しで由来を区別する。

```
【調査前の確認】               ← ask: 本人が実際に選んだ選択肢
- 缶チューハイの飲用頻度: 月2〜3回
```
```
【前提】                       ← assume: 調査定義の premise
- 缶チューハイ・缶ハイボールを月1回以上飲む
```

`ask` の文言は「`label`（無ければ設問文）: 選択した選択肢の文言」で機械的に作る。
言い換えを挟むと、本人が答えていない内容を混ぜてしまう。

### 6.2 プロンプト順序と prefix cache

共有部分を先頭に置く。

```
[system]（全体共通） → [コンセプト]（当該stimulus共通） → [ペルソナ] → [設問]
```

ただし記憶を持つ設問があると会話履歴が伸びるため、**ペルソナ外側・コンセプト内側**のループにする。

**どの設問も記憶を持たない**なら各設問が独立したセッションになるので、
**コンセプト外側・ペルソナ内側**に回して prefix cache を最大限効かせられる。
既定の反実仮想モナディック（§5.3）はこの経路になる。

設問ごとに `[system]` を使い分けても（§6.1）この経路は損なわれない。並べ替えのキーが
`(stimulus_id, question_id, persona_uuid)` なので、**同じ設問＝同じ `[system]` が
連続する**ためである。

記憶を持つ設問と持たない設問が混ざる場合の投入順は決めていない。セッションの長さが
まちまちで「何を先頭に揃えるか」が一意に決まらないため、並べ替えずパネル順で流す。

### 6.3 出力の強制とパース

1. 可能なら**構造化出力（JSON Schema / 制約デコード）**を使い、選択肢番号のみを返させる
2. 使えない場合は正規表現でパース。先頭の数字を採用
3. パース失敗時は最大3回リトライ（同じ入力を再送する。サンプリングは指定せずエンドポイント既定に任せるため、揺れるかどうかはエンドポイント次第）
4. 3回失敗したら `answer_codes` を空にし、`flags` に `parse_error` を付けて続行（調査は止めない）

`model.structured_output` で挙動を選ぶ。

| 値 | 挙動 |
|---|---|
| `auto`（既定） | 構造化出力を試し、エンドポイントが受け付けなければ正規表現パースへ1回だけ切り替える。**切り替えた事実を実行メタデータに記録し、警告を出す** |
| `always` | 構造化出力が使えなければエラーで停止する |
| `never` | 最初から正規表現パース |

`auto` のフォールバックを黙って行わないのは、「構造化出力を使っているつもり」で
精度が落ちた状態に気づけなくなるため。

**理由を書かせる（`questions[].reasoning`）。** 選択式の設問で、構造化出力のスキーマに
`reasoning`（string）を**番号より前のプロパティとして**足す。生成は前から進むので、
順序がそのまま「理由を書いてから番号を選ぶ」順になる。後ろに置くと番号を決めた後の
後付けの説明になり、回答そのものは変わらない。

- 対象は `single` / `scale` / `multi`。`open` / `numeric` は本文が回答なので書けない
- **`structured_output: always` が必須**（`validate` が止める）。正規表現パースへ落ちると、
  理由の文中に現れた最初の数字を回答番号として拾う
- 指示行は設問タイプのもの（`rules.single` 等）を `rules.reasoning.single` 等で
  **置き換える**。「番号のみで答えてください」と併記すると矛盾する。
  **置き換え先も設問タイプごとに持つ**——1本の文を全タイプで使い回すと、そのタイプ固有の
  指示が落ちる（`multi` の「すべて、カンマ区切りで」が消え、複数回答なのに1つだけ
  選ばせる問いになる）。書ける設問タイプは `rules.reasoning` のキーと同じ3つで、
  この並びが `REASONING_QUESTION_TYPES` の出どころでもある
- 出力予算は `model.max_tokens_reasoning`。JSON Schema に `maxLength` は入れない
  （strict 対応がエンドポイント依存で、拒否されると `always` の下で調査が止まる）
- **字数と予算は別物。** `reasoning_max_length` は文字数でモデルへの指示、
  `max_tokens_reasoning` はトークンでエンドポイント側の打ち切り。文字とトークンの比は
  モデルで変わるので片方から他方を導出せず、噛み合っていない組み合わせを `validate` が
  `W_OUTPUT_BUDGET` で警告する（`max_length` と `max_tokens_open` も同じ関係）
- 書かれた理由は `responses.answer_reasoning` に残る。**`refusal` の判定からは理由の本文を
  除く**（§8）——こちらが書かせた散文なので、「わかりません」が拒否の目印にならない。
  本当の拒否は番号を返せないので `parse_error` で表面化する

思考モード（§13）とは別物である。思考の内容が記録に残り、予算で抑えられ、
どの設問で使ったかが実行メタデータに残る点が違う。**ただしばらつきが縮む懸念は同じで、
使った調査では分布の形とセグメント間の差を必ず確かめること**（`validate` が警告する）。

**出力が `max_tokens` に達した場合は、予算を広げて再送する。**

1. エンドポイントが出力上限を理由に失敗を返す（HTTP 400）か、`finish_reason: "length"` を
   返したら、`max_tokens` を段階的に引き上げて同じ入力を送り直す
2. 天井まで広げても完了しなければ、そこまでに得られた本文を記録し、`flags` に
   `output_limit` を付けて続行（調査は止めない）
3. 引き上げは**パース失敗のリトライ回数（`responses.attempt`）に数えない**。軸が違う

同じ入力でも出力長は揺れるため、これはリクエストの不正ではなく再送に意味がある失敗である。
429・5xx のバックオフ（§6.5）とは打つ手が違うので分けて扱う。広げて完了した場合はフラグを
立てないが、**引き上げが起きた事実は実行メタデータ（`model.output_limit`）と警告に残す**。
残さないと「予算が足りていない」という兆候がどこにも現れない。

**`temperature` などのサンプリングパラメータは指定しない。** 最新モデルではこの指定が
非推奨・無効化される傾向にあり、値を書いても実際には効かないまま「設定したつもり」の
記録だけが残って実挙動と食い違う。リクエストに載せず、エンドポイント側の既定に従う。
調査定義に `model.temperature` / `infer.model.temperature` が書かれていた場合は、
黙って無視せず理由を添えて停止する（§2 の調査定義パーサ）。

### 6.4 冪等性と再開

- 主キー `(survey_id, persona_uuid, stimulus_id, question_id)` で upsert
- 調査再実行時、既に成功しているレコードはスキップ
- 大規模調査が途中で落ちても、続きから再開できること

### 6.5 並列度とレート制限

- `model.concurrency` で制御。既定 32
- 429 応答時は指数バックオフ
- Databricks のバッチ推論を使う場合はその並列制御に従う

---

## 7. 集約と出力

### 7.1 集計表の形式

**表の形は2つだけ持つ。** 同じ数字が何通りにも出る余地を作らないため。集計そのものは
`responses` から1度だけ数え、以降は並べ替えと表示用の整形しかしない。

**(a) measure ごとの表** — 表側＝コンセプト × セグメント、表頭＝選択肢（ラベルつき）。

| コンセプト | 軸 | セグメント | n | n(フラグ除外後) | 1. ぜひ | 2. やや | … | T2B | 平均 |
|---|---|---|---|---|---|---|---|---|---|
| コンセプトA | 全体 | 全体 | 1000 | 980 | 12.3% | 28.1% | … | 40.4% | 3.21 |
| コンセプトA | sex | 男性 | 500 | … | | | | | |
| コンセプトB | 全体 | 全体 | 1000 | … | | | | | |

**(b) 全設問を積んだ表** — 設問を1列目に足して縦に積んだもの。設問ごとに選択肢ラベルが
違うので**選択肢列は番号だけ**に揃え、ラベルは注記の凡例に回す。コンセプトを measure 横断で
見比べるのはこちら。

| 設問 | コンセプト | 軸 | セグメント | n | n(フラグ除外後) | 選択肢1 | … | T2B | 平均 |
|---|---|---|---|---|---|---|---|---|---|
| q_intent | コンセプトA | 全体 | 全体 | 1000 | 980 | 12.3% | … | 40.4% | 3.21 |
| q_novelty | コンセプトA | 全体 | 全体 | 1000 | 980 | … | | 35.2% | 3.05 |

- 束ねる単位は設問IDではなく **`measure`**（§3.1）。設問は `slot` ごとに別IDへ展開される
  ので、設問IDで束ねるとコンセプト比較が slot の数だけに割れる
- `output.segments` で指定した軸すべてを表側に展開
- n はウェイト適用前の実数、% はウェイト適用後（ウェイトが全て1.0なら一致）
- `T2B` は設問定義の `top_box` に従う
- `平均` は選択肢番号を逆順スコア化（5段階なら 1→5点）した平均値
- 品質フラグの立った回答も含めて数え、除いた場合の n を `n(フラグ除外後)` に併記する（§8）

### 7.2 自由回答

`survey_id, persona_uuid, stimulus_id, question_id, answer_text` ＋ ペルソナの主要属性を付与した長持ちテーブルを CSV で出力。

### 7.3 出力ファイル一式

```
outputs/{survey_id}/
  ├ crosstab_{measure}.csv       # §7.1(a) measure ごと
  ├ crosstab_all.csv             # §7.1(b) 全設問を積んだもの
  ├ panel_composition.csv        # 実際のパネル構成と通過率（推定値）
  ├ open_ends.csv                # §7.2 自由回答
  ├ responses_raw.csv            # 生データ全件
  ├ run_metadata.json            # §9
  ├ prompt_sample.md             # 実際に送ったプロンプト1ペルソナ分・全設問（§9）
  └ report.xlsx                  # 上の表を1ブックにまとめたもの
```

`report.xlsx` は先頭に「概要」シートを置き、調査情報・注記（§11 E3・E4）・読み方・
帰属表示（§15.3）と免責（§15.1）をそこに集める。**表だけ配られる前提で必ず載せる。**
以降が表1つにつき1シート。ローデータは積まない（`responses_raw.csv` に同じものがあり、
Excel の行数上限にも当たるため）。

Web UI のダウンロードも同じ writer を通す。渡す表とローデータの有無が違うだけで、
構成は同じにする（`docs/SPEC_UI.md` §4.4）。別実装にしていた頃は、片方に入れた注記や
帰属表示がもう片方から落ちていた。

---

## 8. 品質フラグ

セッション単位で判定し、`responses.flags` に格納。**除外はしない**。集計時に除外できるようフラグだけ立てる。

| フラグ | 条件 |
|---|---|
| `parse_error` | 選択肢番号を抽出できなかった |
| `refusal` | 拒否・回答保留・説教的応答と判定された。**`reasoning: true` の設問では理由の本文を判定から除く**（§6.3） |
| `straightline` | 当該ペルソナが全設問で同一位置の選択肢を選んだ |
| `out_of_range` | 存在しない選択肢番号を返した |
| `retried` | 1回以上リトライした |
| `output_limit` | 出力が `max_tokens` に達し、予算を広げても完了しなかった（§6.3）。回答は欠落か切り詰め |

`screener_responses` にはさらに `inferred`（本人に聞かず判定した）が立つ。
`responses` には現れない。実回答と取り違えないための目印であり、品質フラグではない。

集計表には、フラグ付きレコードを含めた場合と除いた場合の n を併記する。

---

## 9. 実行メタデータ（必ず記録）

利用側が実査と突き合わせるために必要。`run_metadata.json` として出力し、`runs` テーブルにも保存。

**実際に送ったプロンプトを1ペルソナ分だけ残す**（`prompt_sample`）。設定値だけでは
「結局どう聞いたのか」が読み取れないため。人が読む用に `prompt_sample.md` にも書き出す。
記録するペルソナはキー順で決めるので、同じ調査を何度実行しても同じ1名になる。
プロンプト組み立ては純関数なので、実行後に組み直しても送った内容と一致する。

**残すのはその1名の全設問**。設問ごとに `slot`・`randomize_options`・`remember` が違い、
1問だけでは2問目以降に何を送ったのかが読めない（提示順が定義順に化けていた不具合を
事後に検知できなかった原因でもある）。設問は聞いた順（`slot` 昇順、同 slot 内は定義順）に並べる。

各設問について **[system] / [user] / [assistant] のメッセージ列をそのまま残す**。
記憶を持つ設問は先行設問の Q&A を再生してから送るので、`system` と `user` の2本には潰せない。
再生する [assistant] は `responses.answer_raw` から引く——エンドポイントの出力は
純関数では組み直せないため。引けなかった設問は目印で埋め、`missing_answers` に残す。
黙って [assistant] を落とすと列の形が変わり、実物と一致しない記録になる。

**調査名と種別は `survey_definition` の中だけでなく、独立した項目としても残す**（§2.4 で列にも
持つ）。`survey.type` は省略できるので、調査定義の全文だけでは実際に何の調査だったのかが
書かれていないことがある。既定値を適用した後の値をここに残す。

**記録から読み直すときは、集計が読む範囲だけを復元する**（`loader.survey_from_record()`）。
`survey_definition` には実行当時の書き方がそのまま残るので、その後で書き方を変えた block
まで読もうとすると、過去の調査の結果が**聞き方の文言を理由に**読めなくなる。復元するのは
`survey` / `panel` / `stimuli` / `questions` / `output` とスクリーニングの方式で、ここは
書くときと同じ厳格さで読む（数字の意味を決めるため）。`main_survey`（model / prompt /
persona_card）と `design` は読まない。復元した定義は集計専用で、実行や「何をどう聞いたか」の
根拠には使えない（`SurveyDefinition.from_record` が印。全文は `raw` にある）。

```json
{
  "survey_id": "...",
  "survey_name": "...",
  "survey_type": "concept",
  "survey_definition": { /* 調査定義の全文 */ },
  "model": {
    "endpoint": "...", "deployment": "...", "model_version": "...",
    "thinking": false, "structured_output": "auto",
    "endpoint_retries": {
      "calls": 8000, "retried_calls": 412, "retries": 655,
      "backoff_seconds": 3180.5, "reasons": { "429": 640, "503": 15 }
    },
    "output_limit": { "escalated": 37, "exhausted": 4 }
  },
  "prompt_sample": {
    "persona_uuid": "...",
    "questions": [
      {
        "stimulus_id": "c1", "question_id": "q_intent_1", "sequence": 1,
        "messages": [
          { "role": "system", "content": "/* 実際に送った [system] 全文 */" },
          { "role": "user", "content": "/* 実際に送った [user] 全文 */" }
        ],
        "missing_answers": []
      },
      {
        "stimulus_id": "c1", "question_id": "q_novelty_1", "sequence": 1,
        "messages": [
          { "role": "system", "content": "..." },
          { "role": "user", "content": "/* q_intent_1 の [user] 全文 */" },
          { "role": "assistant", "content": "/* q_intent_1 に実際に返ってきた回答 */" },
          { "role": "user", "content": "/* q_novelty_1 の [user] 全文 */" }
        ],
        "missing_answers": []
      }
    ]
  },
  "data_versions": {
    "personas_base": "delta_version_17",
    "source_dataset": "nvidia/Nemotron-Personas-Japan@<commit>"
  },
  "panel": {
    "seed": 42,
    "requested": { "M_20s": 100, ... },
    "achieved": { "M_20s": 100, ... },
    "screener_method": "infer",
    "screener_incidence_estimated": { "total": 0.42, "M_20s": 0.51, ... },
    "oversample_actual": 4,
    "screener_infer": null
  },
  "design": {
    "presentation": "counterfactual_monadic", "stimuli_per_persona": 3,
    "questions_memory": {"q_intent_1": "none", "q_novelty_1": ["q_intent_1"]},
    "image_mode": "text"
  },
  "execution": {
    "started_at": "...", "finished_at": "...",
    "sessions_total": 10000, "sessions_ok": 9987, "sessions_failed": 0, "sessions_skipped": 0,
    "records_written": 9987,
    "flags": { "parse_error": 8, "refusal": 5, "straightline": 12 },
    "tokens": { "input": 21500000, "output": 12000 },
    "estimated_cost": null
  },
  "reproducibility": {
    "seed": 42,
    "guaranteed": "inputs_only",
    "note": "..."
  }
}
```

`estimated_cost` はワークスペースごとの単価設定により異なるためシステム内では算定せず `null` 固定とし、`tokens`（トークン数）のみを記録する。
`execution` には構造化出力のフォールバック有無（§6.3 の `auto` で正規表現に切り替えたか）も記録する。

### 9.1 再現性の範囲

**`seed` と各バージョンが揃えば、モデルへの入力が完全に再現できること。** これが満たせない実装は不可。

保証するのは**入力側**である。誰が選ばれ、どのコンセプトを、何番目に、どの選択肢順で、
どのプロンプト文面で提示されたか——ここまでは `seed` と各バージョンから一意に決まる。

**生成結果そのものの一致は保証しない。** 本システムは `temperature` を指定せず、
サンプリングの設定はエンドポイント側の既定に委ねる（§6.3）。同じ入力でも出力は揺れうるし、
その揺れ方をシステム側から固定する手段も持たない。これは除去できない非決定性なので、約束しない。

そのため生データ（§2.3）は `answer_raw`・`attempt`・`flags` を含めて残す。
結果が食い違ったときに、入力が同じだったのか出力が揺れたのかを切り分けられるようにするため。

---

## 10. インターフェース

**入口は2つ**。ノートブックと Web UI で、どちらもライブラリの同じ関数を直接呼ぶ。
CLI は持たない——同じ経路を3通りの入口から呼ぶと、片方だけ直す事故が起きる。

### 10.1 ノートブック

Databricks ノートブックから関数群を呼ぶ。調査定義は YAML でも dict でも渡せる。

```python
survey = load_survey("survey.yaml")            # 調査定義の読み込み
validate_static(survey)                        # スキーマ検証
validate_feasibility(spark, personas, survey)  # 抽出可能性の事前チェック（E1）
build_panel(spark, survey, storage)            # パネル構築（LLM を呼ばない）
screen_survey(spark, survey, storage)          # スクリーニング（screening: があるときだけ）
run_survey(spark, survey, storage)             # 回答生成（中断しても同じセルで再開）
aggregate_survey(spark, survey, storage, output_dir())   # 集計と §7.3 の出力一式
write_metadata(spark, survey, run_result, storage, output_dir())
```

`build_panel` を独立させているのは、**割り付けが埋まるかを本実行前に確認する**ため。
スクリーニング込みで走らせてから足りないと分かるのは無駄が大きい。`screen_survey` を
分けているのは、`build_panel` を LLM を呼ばない安い確認手段のまま保つため。
スクリーナーが無い調査では `screen_survey` は何もせずに返る。

検証（`validate_static` / `validate_feasibility`）は次を必ず出力する。セッション数は
コンセプト数倍になりコストに直結するため、実行前に必ず目に入るようにする。

```
セッション数   = Σ(パネル人数 × assigned_stimuli 数 × 設問数)
有効サンプル数 = コンセプトごとの評価者数
```

`notebooks/run_survey.ipynb` がこの順序をそのまま持ち、Databricks ジョブの本体でもある
（Web UI はこのジョブを起動する）。

### 10.2 Web UI

コンセプト調査に絞った Streamlit アプリ（Databricks Apps）。詳細は `docs/SPEC_UI.md`。

設定は**2層**にする。調査をまたいで変わらないもの（本調査とスクリーニングのモデル・
プロンプト・ペルソナカード、既定設問、割り付けパターン、見積もり用のベンチマーク）は
`app/config/ui_config.yaml`。1調査ごとに変わるもの（調査名・N数・年齢範囲・対象者条件・
コンセプト・設問）は画面入力。両者を `persona_sim.uiconfig.build_survey()` が合成して
調査定義（§3）を作る。
結果の読み取りは SQL Warehouse 経由（`persona_sim.storage.warehouse`）で、Spark を使わない。

`app/config/ui_config.yaml` 自身も最上位で2群に分ける。`survey_defaults` はそのまま調査定義に
なるもの、`ui` は画面を描くためだけのもの。混ざっていると、設定を変えたときに調査結果が
変わるのか画面の見た目が変わるのかを読み分けられない（`docs/SPEC_UI.md` §2）。

```
app/config/ui_config.yaml ─┐
                       ├→ build_survey() → 調査定義（§3）→ panel → screen → run → aggregate
画面入力（SurveyForm）─┘
```

- UI は §3 の調査定義を作る以上のことをしない。割り付けの整数配分は §4.1・§4.3 の
  既存経路（`quotas.mode: proportion`）に委ね、UI 側で丸め処理を書かない
- 実行は非同期。UI は事前定義ジョブを起動して `survey_id` を返す。完走した調査が
  結果画面の一覧に出てくる（§2.4）。走行中の進捗は追わない
- 二者の関係: ノートブック = 全機能・調査定義ファイル前提。
  UI = コンセプト調査に絞った既定値つきの入口。UI で書けない調査は調査定義を書いて
  ノートブックから実行する

---

## 11. エラー処理

| # | 条件 | 挙動 |
|---|---|---|
| E1 | セルの候補ペルソナが必要数に満たない | 実行前に停止。不足セルと候補数を提示 |
| E2 | スクリーニング後、通過者が必要数に満たない | oversample を2倍にして最大3回再試行。それでも不足なら停止し、実インシデンスを提示。**実測通過率から見て上限倍率でも届かないと分かった時点で、上限まで試さずに停止**し、必要な候補数の見積もりを添える（§4.2） |
| E3 | パース失敗が全体の5%を超えた | 警告を出して続行。集計表に注記 |
| E4 | 拒否応答が全体の5%を超えた | 警告を出して続行。集計表に注記 |
| E5 | エンドポイントの継続的な失敗。**連続20回失敗、または直近100件の失敗率が50%超** | 調査を中断し、完了分を保存（同じコマンドで再開可能） |
| E6 | 調査定義の不整合（§3・§5）。`slot` が `1〜コンセプト数` を覆っていない、設問IDの重複、同じ `measure` で選択肢や `top_box` が揃っていない、`remember` が未回答の設問を参照している 等 | 実行前に停止。どこをどう直すかを示す |

E3・E4 は**止めずに続行し、必ず表面化させる**。黙って集計されるのが最も危険。

E5 に至らない範囲の再試行（レート制限からの復帰など）も、5% を超えたら
`W_ENDPOINT_RETRY` として警告し、内訳を `run_metadata.json` の
`model.endpoint_retries` に残す（§9）。**成功しているので E5 では止まらないが、
そのぶん実行時間だけが伸びる**。`responses.latency_ms` にはバックオフの待ち時間も
含まれるため、これを分けて残さないと「モデルが遅い」のか「並列度を捌けていない」のかを
実行後に切り分けられない。

---

## 12. 実装状況

| # | 内容 | 完了条件 | 状態 |
|---|---|---|---|
| M1 | `personas_base` 構築（取り込み・正規化・派生列） | 属性でクエリできる | 完了 |
| M2 | 調査定義パーサ ＋ 検証 / パネル構築 | 割り付けどおりのパネルが seed 再現で出る | 完了 |
| M3 | 実行エンジン（single/open、設問ごとの記憶に対応） | 1コンセプト × 100人が完走 | 完了 |
| M4 | スクリーニング機構 | 通過率が記録される | 完了 |
| M5 | 集計・出力（クロス集計表、xlsx） | §7.3 の一式が出る | 完了 |
| M6 | 冪等性・再開、品質フラグ、メタデータ出力 | 中断→再開で結果が一致する | 完了 |
| M7 | 並列化・スループット調整 | 1,000人 × 10コンセプト × 3問が実用時間で完走 | 未着手 |

---

## 13. 実装上の注意（要点のみ）

- **思考モードは必ず OFF。** 回答が収束してペルソナ間のばらつきが失われる。
  理由を書かせたいなら `questions[].reasoning`（§6.3）を使う。思考の内容が
  `responses.answer_reasoning` に残り、どの設問で使ったかが実行メタデータに残る。
  **ばらつきが縮む懸念は同じなので、使ったら分布の形とセグメント間の差を必ず確かめる**
- **`temperature` などのサンプリングパラメータを指定しない**（§6.3）。指定してもモデル側に
  無視されうるため、設定値と実際の挙動が食い違う
- **順序尺度の選択肢はシャッフルしない。** 単一回答の非順序選択肢のみ `randomize_options: true` を許可し、実際の提示順を必ず記録する
- **同一ペルソナは同一コンセプトに1回しか回答しない**（§5.0）
- **提示設計を設定で選べるようにしない**（§5.3）。反実仮想モナディック固定
- **スクリーニング確定後に `cell_rank` を振り直す**（§4.2）。オーバーサンプル時の順位のまま
  残すと、非通過で空いた穴がそのまま順位の飛びになり、パネルの記録から読めなくなる
- **スクリーナーの通過率を実測値として扱わない**（§4.2・§9）。本人には聞いていないので
  実測はできない。実測値の欄を作らず、`screener_incidence_estimated` に記録する。
  スクリーニングを行っていない調査の通過率は 1.0 ではなく未測定（`null`）
- **`screened_out` レコードを削除しない**（通過率の検証に使う）
- **集計結果をテーブルに保存しない**（§2・§7）。読むたびに `responses` から数え直す
- **量子化モデルを使う場合はバージョン固定**し、途中で変えない
- 未成年を含めない設計のカテゴリでは `filters.age_min` を必ず設定する

---

## 14. スコープ外（非目標）

以下は **意図的にやらない**。要求されても断る。

- 実査の完全代替。統計的に有意な差の検定、市場規模の絶対値推計には使わない
- 個人の再現。実在する特定顧客の意思決定モデル化は行わない（倫理・法務リスク）
- 広告効果の絶対値予測（CTR、売上リフト等）。相対比較のみ
- 社外提供・SaaS化（自社内利用に限定）
- 実査で取る実測インシデンス。本システムのスクリーナーは推定値しか出せない（§4.2）

---

## 15. ガバナンス・法務

### 15.1 結果の取り扱いルール

社内周知として明文化し、UI上にも常時表示する。

> 本ツールの出力はAIによるシミュレーションであり、実在する生活者の回答ではありません。
> 意思決定の根拠として単独で用いず、仮説生成・優先順位付け・調査設計の目的で使用してください。

- レポート出力には必ず本文言と、使用モデル・プロンプトバージョン・検証時点の精度を記載する
- 対外資料（クライアント提案、広報）への数値掲載は法務レビューを必須とする

### 15.2 個人データ

- 社内データはセグメント統計に集約してから使う（§6.2 モードA）
- 実在個人を特定・再現するペルソナは作らない
- 個人情報保護法上、統計目的での利用に該当することを法務で確認する

### 15.3 ライセンス表示（必須）

Nemotron-Personas-Japan は CC BY 4.0。**帰属表示が義務**。

- レポートのフッタ、システムのAboutページ、社外資料に以下を記載
  ```
  本システムは NVIDIA が公開する Nemotron-Personas-Japan (CC BY 4.0) を使用しています。
  https://huggingface.co/datasets/nvidia/Nemotron-Personas-Japan
  ```
- 派生データ（エンリッチドペルソナ）を配布する場合の条件を法務に確認する

---
