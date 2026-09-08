# 設問ごとの記憶保持と、設問の提示スロットへの紐づけ（結果）

- **日付**: 2026/08/08 14:00
- **計画**: [20260808-140000-question-level-memory-plan.md](20260808-140000-question-level-memory-plan.md)

## 実施内容

調査官の記憶の保持単位を、調査全体（`design.memory` の3値）から**設問**に下ろした。
あわせて、参照を設問IDで書けるようにするために設問を提示スロットに紐づけて全展開する
モデルへ移した。**破壊的変更が2件ある**（後述）。

### 1. 設問は提示スロットに紐づく（破壊的変更）

`Question` に3つのフィールドを足した。

| フィールド | 既定 | 意味 |
|---|---|---|
| `slot` | `1` | 何番目に提示するコンセプトについて聞くか（1始まり） |
| `measure` | `id` と同じ | コンセプト横断で「同じ問い」として束ねる集計キー |
| `remember` | `none`（持たない） | どの設問の回答を持ったままこの設問に入るか |

`slot` はコンセプトIDではなく**提示順の位置**で、実行時に `assigned_stimuli[slot - 1]` を
引く。`rotation` や `sample_overlap` による割り当ての違いと直交する。

こうした理由は、**設問IDを実行時にも一意にする**ため。旧モデルでは `questions` が全
コンセプトに繰り返されるテンプレートで、1人の回答履歴に `q_intent` がコンセプト数ぶん
現れる。`remember: [q_intent]` がどれを指すのか決まらない。

`presentation: simultaneous` は全案を1度に見せるので `slot` を持たない（書いたら E6）。

### 2. `measure` — 一意化の代償を受け止める

IDが一意になると、**コンセプト比較表がコンセプトごとに割れる**。`aggregate/tables.py`
の比較表は同じ `question_id` を stimulus 横断で並べる作りで、そこが `q_intent_1` /
`q_intent_2` に分かれると「コンセプトAの購入意向」がAを1番目に見た人と2番目に見た人で
別の指標になってしまう。

集計層の束ねキーを `question_id` から `measure` に移した。`Crosstab.question_id` は
`Crosstab.measure` に改名し、回答は `_by_measure()` で畳む。`aggregates` テーブルの
`question_id` 列も **`measure` に改名**した（破壊的変更2件目。`aggregates` は
`responses` から作り直せる派生テーブルなので、`persona-sim aggregate` の再実行で足りる）。
**`responses` は `question_id` のまま**——何を聞いたのかを辿る鍵はそちらにある。

同じ `measure` の設問は `type` と `options` が一致していることを E6 で保証する。
表頭は代表1つから作るので、揃っていないと別の問いの数字が同じ列に並ぶ。

### 3. 記憶（`remember`）

`none` / `all` / 設問IDのリストの3形。**意味は調査定義の中だけで完結する**——
`all` は「それまでに聞いた設問すべて」で、コンセプトをまたぐ。これ1つしか意味を持たない。

「同じコンセプトの中だけ」に絞る専用の値は用意していない。設問IDを並べれば書けるうえ、
コンセプト調査の都合で `all` の意味を変えると、同じ語が文脈によって別のものを指す。
コンセプト調査は数ある調査の一種でしかない。

**列挙どおりで推移しない。** Q4 が Q3 を、Q3 が Q1 を覚えていても Q4 が見るのは Q3 だけ。
プロンプトに何が載るかを調査定義から直読できるようにするため。再生する順は書いた順では
なく**聞いた順**（ask order = `slot` 昇順・記述順）で、会話履歴の時系列が狂わないようにした。

新設した `persona_sim/run/memory.py` が解決を持つ（`run/prompt.py` と同じく pyspark 非依存の
純関数だけ）。`resolve_remember` / `ask_order` / `plan_remembers` / `session_groups` /
`retains_memory` / `stimulus_for`。

### 4. セッションの粒度は記憶から決まる

「設問 A が設問 B を覚えている」なら A と B は同じセッションに入る。この連結成分
（union-find）が実行単位であり再開単位。**3値との対応は下表のとおりで、分割は従来と
完全に一致することをテストで固定した。**

| やりたいこと | 書き方 | 2人 × 3コンセプト × 2問での実測 |
|---|---|---|
| 記憶を持たせない | 何も書かない | 12セッション（各1問） |
| 同じコンセプトの中だけ | 同じ slot の先行設問IDを並べる | 6セッション（各2問） |
| 全部覚える | 全設問に `remember: all` | 2セッション（各6問） |

記憶を持たない設問は依存を持たないので単独のセッションになり、既定モードの並列度
（＝スループットと費用）が落ちない。

### 5. プロンプトは毎ターン組み直す

`messages` を伸ばし続ける作りをやめ、`replayed_messages()` で**毎ターン組み直す**ように
した。伸ばし続ける形では「直前までの全部」しか表現できず、飛ばし越しが書けない。

取っておくのは回答の文面（`answer_raw`）だけでよい。設問文も選択肢の提示順も純関数から
組み直せる（`presented_options()` は同じ入力なら必ず同じ順序を返す）。

守っている不変条件は2つ。

1. **プロフィールは先頭の [user] に1回だけ。** 回答済みターンの [user] をそのまま使い
   回すと、単独セッションで実行された設問（＝プロフィール入り）の再生で2回出る。
   だから保存するのは回答だけにして、設問ブロックは組み立て直している
2. **コンセプトはその列で初出のターンにだけ挟む**

あわせて `Unit.stimuli_to_present` を**全 unit が常に自分のコンセプトを持つ**形に変えた。
実際に出すかは組み立て時に判定する。旧実装は履歴を持つモードで先頭 unit にだけ持たせ、
持たない unit は `ctx.stimuli`（＝調査定義の記述順）から補っていて、`session.py` 自身の
コメントが「提示順が2問目以降だけ定義順に化ける」と警告していた。条件分岐ではなく
構造で潰した。

### 6. 検証・見積・記録

- `slot` の範囲外／`1..m` の抜け（**提示されるのに1問も聞かれないコンセプト**）を E6
- `remember` の未知ID・自己参照・前方参照を E6
- `W_ROTATION` の判定を **`retains_memory(survey)`**（設問ごとの指定）に変更
- セッション数の見積を `size * m * 設問数` から **`size * len(session_groups(survey))`** に
  変更。実行と同じ関数から数えるので、記憶を持たせてもコスト見積がずれない
- 見積の「設問数」は展開後の延べ数になるので、**別々の問いの種類数（`measures`）を併記**
  するようにした。`9（別々の問い 3 種）` のように出る
- `run_metadata.json` の `design` に **`questions_memory`**（設問ごとの記憶）を追加。
  何を持たせて聞いたのかが分からないと、モデルへの入力を再現できない

### 7. 範囲外の `slot` は実行時にも読める形で落とす

Spark テストの1回目で `IndexError: list index out of range` が20件出た。`validate` は
範囲外の `slot` を E6 で止めるが、**`run` は `validate` を通らずに実行できる**ので、
そこを素通りすると `assigned[slot - 1]` が素の IndexError になる。パネル全員ぶんの
セッションが「list index out of range」で落ちるだけで、調査定義のどこが悪いのか読めない。

`stimulus_for()` で範囲を確かめ、設問IDと件数を添えた `SurveyDefinitionError` にした。
検証の二重化ではある（`validate` 側は全件を集めて報告する役割を保つ）が、`run` から
到達できてしまう以上、落ち方だけは読める形にしておく必要がある。

### 8. 旧形式は読まずに停止する

`slot` の既定 1 で黙って読むと、m > 1 の調査で2つ目以降のコンセプトが聞かれずに消え、
**同じ調査定義で結果の意味が変わる**。判定は `validate` ではなく**ローダー**に置いた——
`run` は `validate` を通らずに実行できる（`cli.py` はサブコマンドごとに `load_survey()` を
呼ぶだけ）ので、`validate` に置くと素通りする経路が残る。

m == 1（`disjoint`、またはコンセプト1件）なら旧形式と新形式の展開が一致するので、そのまま
読んでよい。実際 `examples/survey_sample_smoke.yaml`（1コンセプト）は無改修で通っている。

### 9. UI は変えていない

`uiconfig/build.py` がフォームの設問を slot 展開して調査定義を書く（`{id}_s{slot}` /
`measure: {id}`）。画面と入力は変わらず、利用者に冗長さは見えない。案どうしを独立に
評価させるので `remember` は書いていない。

## 変更したファイル

実装: `panel/schema.py` / `panel/loader.py` / `panel/validate.py` / **`run/memory.py`（新規）** /
`run/session.py` / `run/prompt.py` / `aggregate/{crosstab,tables,aggregate,export}.py` /
`uiconfig/build.py` / `metadata.py` / `app/views/results.py`

仕様・例: `SPEC.md`（§2.5 / §3.1 / §5.1 / §6.2）/ `examples/survey_sample.yaml` /
`notebooks/quickstart.ipynb`

テスト: **`tests/test_memory.py`（新規24件）** / `tests/conftest.py` /
`test_prompt.py` / `test_session.py` / `test_survey_loader.py` / `test_validate.py` /
`test_crosstab.py` / `test_aggregate_pure.py` / `test_rawdata.py` / `test_results_page.py` /
`test_warehouse.py`

## 検証

- `ruff check` パス
- **非 Spark テスト 628件全件パス**（変更前602件、26件追加）
- **Spark テスト 82件全件パス**（Java 17 + pyspark 4.0.1 / delta-spark 4.3.1 をこの環境に
  導入して実行。前サイクルまでは Java が無く CI に委ねていた）。ただし**一発では通っていない**。
  - **1回目: 20 failed / 10 errors。** Spark 側のひな形がコンセプト数を減らす
    （`test_run_spark` は1件、`test_aggregate_spark` は2件）のに設問は3 slot のままで、
    `slot` が割り当て件数を超えていた。ひな形を `base_survey_dict(slots=n)` で揃えて解消。
    **この失敗は実装の欠陥も1つ暴いた**（次項）
  - **2回目: 6 failed。** すべて設問IDの直書き（`q_intent` → `q_intent_1`）と、
    集計が measure 単位になったことへの追随漏れ。特に
    `test_aggregates_match_the_raw_responses` は、コンセプト c1 への回答を数えるのに
    slot 違いの設問すべてを見る必要がある（c1 を1番目に見た人は `q_intent_1`、
    2番目に見た人は `q_intent_2` に答えている）ことを、テスト側にそのまま突きつけた
  - **3回目: 82件全件パス**
- 3値のセッション分割が変更前と一致することを、件数と各セッションの設問数で固定
  （`tests/test_memory.py::test_legacy_memory_values_keep_their_session_split`）
- 記憶を持たない設問の入力が従来と**1バイトも変わらない**ことを、
  `replayed_messages()` と `initial_messages()` の同値比較で固定
  （`tests/test_prompt.py::test_a_single_turn_replay_equals_initial_messages`）
- プロンプトの実物を確認: 飛ばし越しの設問がコンテキストに入らないこと、プロフィールが
  1回だけ、コンセプトが初出でだけ挟まることを、送信メッセージ列を控えて実測
- 見積が記憶の指定を反映することを実測。`examples/survey_sample.yaml`（20人 × 3案 × 3問）で
  **`remember` あり60セッション / なし180セッション**（案内で3問を1会話にまとめるぶん 1/3）
- `examples/survey_sample*.yaml` がどちらも `validate_static` を通ることを確認

### ついでに直したもの

`examples/survey_sample.yaml` は **本変更以前から** `validate` が
`割り付けの合計 20 が panel.size 600 と一致しない` で止まっていた（`git stash` して確認済み）。
サンプルは `load_survey()` が通ることしかテストしておらず、検証まで見ていなかったため
残っていたもの。割り付けを 100 × 6セル = 600 に直し、酒類カテゴリなので `filters.age_min: 20`
も足した（`survey_sample_smoke.yaml` と揃えた）。`test_sample_yaml_is_valid` を
`validate_static` まで通すように広げて、同じことが起きないようにしている。

### 確認していないこと

- **Databricks 実環境での実行は未実施。** `endpoint: fake` と Spark ローカルセッションまで
- 記憶を持つ設問と持たない設問が混ざる場合の投入順（prefix cache）は決めていない。
  並べ替えずパネル順で流し、その旨を `build_sessions()` のコメントに残した

## 破壊的変更のまとめ

1. **`slot` の無い調査定義は m > 1 で読み込み時に停止する。** 全展開して書き直すこと。
   案内メッセージに書き方を載せている（`LEGACY_QUESTIONS_WITHOUT_SLOT`）
2. **`aggregates` テーブルの `question_id` 列が `measure` になった。** 既存の
   `aggregates` は `persona-sim aggregate` の再実行で作り直す。`responses` は無変更なので
   回答生成のやり直しは不要


---

## 追記: 実装レビューで見つけた4件の修正（同日）

実装後に読み直したところ、**テストは全件通っているのに黙って結果が変わる欠陥が4件**
見つかった（すべて実測で再現）。根っこは2つ——`remember` の意味を別の設定に依存させたこと、
`run` が `validate` を通らないこと。

### 1. `remember: all` の範囲が未定義のまま「コンセプトまたぎ」に倒れていた（高）

`all` の範囲を `design.memory` から決める作りにしていたが、その既定値
（`none`＝「設問側に書かなければ記憶なし」）のときに範囲が定義されない。実装は
**コンセプトをまたぐ側**に倒れており、案3の回答に案1・案2の評価が混ざる。
反実仮想モナディックの前提（案どうしが独立）が壊れるのに `validate` は「問題なし」。

**利用者の判断で、根っこから単純化した。**

- `all` は「それまで全部」ただ1つの意味。コンセプトをまたぐ
- 「同じコンセプトの中だけ」は**設問IDを並べて書く**。専用の値は足さない——
  コンセプト調査は数ある調査の一種でしかなく、その都合を `remember` に持ち込むと
  同じ語が文脈で別のものを指す
- **`remember` は `design.memory` の影響を一切受けない**

その帰結として `design.memory` はやることが無くなったので**廃止した**（残したまま無視すると
「設定したつもり」の記録だけが残る）。`§5` の「3軸」は **2軸＋設問ごとの記憶**になった。
移行は `REMOVED_DESIGN_FIELDS` に案内つきで載せている。

| 旧 `design.memory` | 新しい書き方 |
|---|---|
| `none`（既定） | 何も書かない |
| `full_session` | 全設問に `remember: all` |
| `within_stimulus` | 各設問に、同じコンセプトの先行設問のIDを並べる |

### 2. 同じ `measure` の `top_box` 不一致が検査されていなかった（高）

`type` と `options` は検査していたのに `top_box` を見ていなかった。集計は measure ごとに
**代表1つの設問から T2B を出す**ので、コンセプトごとに「上位いくつ」がずれていても表は
出てしまい、**全コンセプトが1つ目の定義で計算される**。`is_ordinal()` も `top_box` を見るので、
平均を出す／出さないも入れ替わる。`_check_measures()` に足した。

### 3・4. 設問が無言で消える2経路（中）

`run` は `validate` を通らずに実行できる（`cli.py` は各サブコマンドで `load_survey()` を
呼ぶだけ）。そのため次の2つが素通りしていた。

- **設問IDの重複**: 実行計画は設問IDをキーにした対応表で組むので重複が畳まれ、
  6問定義しても5問しか聞かれない
- **`slot` の抜け**: そのコンセプトは `assigned_stimuli` に入って評価枠を消費する
  （有効サンプル数にも数えられる）のに1問も聞かれず、集計表に n=0 の行だけが残る

`survey_from_dict()` は `SurveyDefinition` を組み立てる唯一の場所で、CLI・ノートブック・
画面（`storage/warehouse.py`）が全部ここを通る。**結果の意味が変わる不備はここで止める**
（`_reject_unusable_questions()`）。Spark テストで踏んだ slot 範囲外を `stimulus_for()` で
塞いだのと同じ理由だが、あのときは**落ちた1箇所だけを塞いで同種の穴を探さなかった**。

### 5. 到達しなくなった検査を落とした

読み込みで止める以上、`validate` にその状態の調査定義は届かない。**一度も発火しない検査**を
残すのは「設定したつもり」と同じなので、`_check_slots` の範囲外・抜けの分岐と、設問IDに
対する `_check_unique_ids` を削除した（`presentation: simultaneous` で `slot` を書いた場合の
分岐は、ローダーがそこを飛ばすので今も到達する。残してある）。

### 6. 設計の誤りが設問の誤りとして報告されていた（この修正中に判明）

`sample_overlap: disjoint` に `stimuli_per_persona: 2` を書くと、
`stimuli_count_per_persona()` が矛盾を無視して `1` を返すため、読み込みガードが
「m=1 なのに slot 2 の設問がある」と**設問を指して**停止していた。直すべき行は
`design` の側なのに辿り着けない。

`stimuli_count_per_persona()` が矛盾を検出して送出するようにした。**規則の定義が
1箇所に集まる**のが要点で、ローダーは例外を捕まえて自分の判定を飛ばし、`validate` が
`_check_design()` で本来の E6 を報告する。

### 検証（追記分）

- `ruff check` パス、**非 Spark テスト 633件全件パス**、**Spark テスト 82件全件パス**
- Spark は**ここでも一発では通っていない**（1回目 2 failed）。`test_panel_spark` のひな形が
  `sample_overlap` を変えるのに設問は3 slot のままで、読み込みガードに掛かった。
  直前に `test_run_spark` / `test_aggregate_spark` / `test_screening_spark` の同じ問題を
  直したときに、**同じひな形を使う他のファイルを確認しなかった**のが原因
- **新規テスト5件が修正前のコードで落ちることを確認済み**（`persona_sim/` を一時的に戻して実行）。
  設問IDの重複・`slot` の抜け／範囲外・`top_box` の不一致・`design.memory` の廃止の5件
- **`test_all_does_not_depend_on_anything_outside_the_question` は修正前でも通る。**
  修正前の既定（`design.memory: none`）でも `all` はまたぐ側に解決されるため、
  このテストは差分を検出しない。**新しい意味を固定する網**であって、欠陥の再現ではない
  ——欠陥そのもの（`within_stimulus` を書くと同じ `all` が別の範囲を指す）は、
  設定を廃止したので書き表せなくなった
- `examples/survey_sample*.yaml` がどちらも `validate_static` を通ることを再確認
