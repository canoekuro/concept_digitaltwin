# コンセプト調査 Web UI 仕様

| 項目 | 内容 |
|---|---|
| 位置づけ | `SPEC_PHASE1.md` §10.3 を展開したもの。矛盾したら `SPEC_PHASE1.md` が優先 |
| 対象 | `persona_sim` の上に載せる Streamlit マルチページアプリ（Databricks Apps） |
| 責務境界 | 調査定義（`SPEC_PHASE1.md` §3）を組み立てて既存の実行経路に渡すまで |
| バージョン | 1.0 |

---

## 1. 目的

コンセプト調査に絞って、調査定義ファイルを書かずに設計・実行・結果閲覧ができる入口を作る。
クイックスタート notebook（`notebooks/quickstart.ipynb`）で通している流れを、そのまま画面にする。

**UI はロジックを持たない。** パネル構築・回答生成・集計は `persona_sim` の既存経路がすべて行う。
UI がやるのは「画面入力 → 調査定義」の変換と、ジョブ投入、進捗と結果の表示だけ。
UI から入力できる範囲を超える調査は、従来どおり調査定義ファイル＋CLI で行う。

### 決定事項

| 項目 | 仕様 | 補足 |
|---|---|---|
| デプロイ環境 | Databricks Apps | Streamlit マルチページアプリ |
| 画面構成 | 2ページ | ① 調査設計 ② 結果閲覧 |
| 実行制御 | 非同期（Databricks Jobs API） | UI はジョブを投入して `survey_id` を返す。§6 |
| データ永続化 | Delta Table | `panels` / `responses` / `runs` / `aggregates`（`SPEC_PHASE1.md` §2） |
| 提示設計 | 反実仮想モナディック | `same` × `sequential` × `none`。UI からは変えられない |
| スクリーニング | `infer`（既定） | 自然言語の条件文1つ。空欄ならスクリーニングなし |
| サンプル割付 | 3パターン | 性別×10歳刻み均等 / 性別×5歳刻み均等 / 国勢調査人口構成比 |
| 設問 | 定量2問（既定） | 購入意向・新規性。文言と選択肢は画面で編集可 |
| 設定管理 | `config/ui_config.yaml` | 調査をまたいで変わらないものだけ。§2 |

---

## 2. 設定は2層に分ける

| 層 | 何が入るか | どこから来るか |
|---|---|---|
| `config/ui_config.yaml` | 本調査とスクリーニングのモデル・プロンプト・ペルソナカード、既定設問、割り付けパターン、見積もり用の実績値 | 運用で固定。リポジトリにコミットする |
| 画面入力（`SurveyForm`） | 調査名、N数、対象性別と性別ごとの対象年齢、対象者条件、割り付けパターンの選択、コンセプト、設問文 | 調査ごとに入力 |

```
config/ui_config.yaml ─┐
                       ├→ build_survey() → 調査定義（§3）→ panel → screen → run → aggregate
画面入力（SurveyForm）─┘
```

分けているのは、両者の寿命が違うから。毎回入力させると事故るし、設定ファイルに埋めると
調査ごとに変えられない。合成は `persona_sim.uiconfig.build.build_survey()` が行う。

**組み立てた dict は `survey_from_dict()` に通す。** 検証を自前で書かず、CLI・ノートブックと
同じ経路に乗せるため。UI 経由と調査定義ファイル経由で通る検証が違ってはいけない。
同じ理由で、`ui_config.yaml` の `model:` / `prompt:` / `persona_card:` は UI 側で解釈せず、
そのまま調査定義へ渡す（スキーマの二重管理を避ける）。

### `ui_config.yaml` 自身も2群に分かれる

設定ファイルの最上位は `survey_defaults` と `ui` の2つだけ。

| 群 | 何が入るか | 調査定義に入るか |
|---|---|---|
| `survey_defaults` | `survey_type` / `screening` / `main_survey` / `questions` / `output.base_segments` | **入る**。`build_survey()` がそのまま渡す |
| `ui` | `allocation_patterns` / `estimation_benchmarks` | 入らない。画面の選択肢と見積もり表示にだけ使う |

混ざっていると、設定を変えたときに**調査結果が変わるのか画面の見た目が変わるのか**を
読み分けられない。`survey_defaults` の中は調査定義と同じ並び――スクリーニング
（`screening`）→ 本調査（`main_survey`）の順で、それぞれ model → prompt →
persona_card → その他。

`questions` は画面の設問フォームの初期値で、編集された結果が調査定義の `questions:` の
もとになるので `survey_defaults` 側に置く。`survey_type` も調査定義に入る
（あわせて結果画面の実行一覧の絞り込みにも使う）。

既定の設問は `system`（`main_survey.prompt.systems` の名前）を持つことがある
（`SPEC_PHASE1.md` §6.1）。購入意向と新規性は聞いていることの性質が違うので、
既定の2問には別々の `[system]` を紐づけてある。**画面には出さない。** 文面は調査ごとに
変えるものではなく、運用側が `ui_config.yaml` で書き分けて固定する値だから。

**そのままは渡らない。** 調査定義の設問は提示スロットに紐づくので（`SPEC_PHASE1.md` §3.1）、
`build_survey()` がコンセプトの数だけ `slot` 展開する。`id` は `{元のid}_s{slot}` にして
一意にし、元の `id` を `measure`（コンセプト横断で同じ問いとして束ねる集計キー）に残す。
**画面と入力は変えない。** コンセプトごとに設問を作らせるのは、UI の役割（コンセプト調査を
組み立てるだけ）から外れる。

### 出力形式（`formats`）は設定に持たない

集計軸（`output.base_segments`）は画面の集計軸そのものなので要る。一方**出力形式は
要らない**。結果画面は SQL Warehouse を直接引いて表を描き、Excel / CSV のダウンロードは
押されたその場で生成する（§5）。ジョブがファイルを書き出しても誰も読まないため、
UI 経由の調査は調査定義側の既定（`delta` のみ）で走る。

調査定義のスキーマとしては `output.formats` は残っている。CLI やノートブックから
ファイル一式を書き出したい場合はそちらで指定する（`SPEC_PHASE1.md` §7.4）。

### 書かないもの

- **テーブルの置き場所。** 環境変数 `PERSONA_SIM_CATALOG` / `PERSONA_SIM_SCHEMA` /
  `PERSONA_SIM_WAREHOUSE` からのみ解決する（`persona_sim/config.py`、`AGENTS.md`）。
  論理テーブル名は `persona_sim/storage/locator.py` の定数で、設定できない
- **APIキー・トークン。** `databricks-sdk` の既定の認証チェーンに任せる

サービングエンドポイント名は書いてよい。秘密ではないうえ、何で回したかが読み取れないと
再現できない（`SPEC_PHASE1.md` §9.1）。

### `survey_defaults.questions` には `top_box` を必ず入れる

`Question.is_ordinal()` は `type: scale` か `top_box` 有りで真になる。`single` で `top_box` が
無いと偽になり、`Metric.top_box` **と `Metric.mean` の両方が `None`** になる。
結果画面の T2B も平均スコアも出なくなるので、順序尺度には必ず `top_box: [1, 2]` と
`randomize_options: false` を入れる（後者は `AGENTS.md` の不変条件）。

### 未知のキーは停止させる

`load_ui_config()` は調査定義ローダと同じく、知らないキーを黙って捨てずに停止する。
綴り違いの設定がそのまま消えると、「設定したのに効いていない」ことに実行後まで気づけない。

---

## 3. 画面1: 調査設計（`app/views/survey_design.py`）

ファイル名は英語、サイドバーの表示名は日本語（`.agents/rules/language-strategies.md`）。
これを両立させるため、ページは `st.navigation` ＋ `st.Page(title=...)` で登録する。
`pages/` の自動検出だと表示名がファイル名になってしまう（両者は排他）。

### 3.1 入力項目

1. **基本情報・ターゲット**
   - 調査名（テキスト。例: 「新ビールコンセプト評価調査」）
   - 対象性別・対象年齢（性別ごとにチェックボックス＋年齢 from-to の数値入力。
     チェックを外した性別は調査対象から除外され、性別ごとに異なる年齢範囲を指定できる。
     例: 男性 20〜69歳のみチェック、または男性 20〜39歳・女性 40〜69歳のように分ける）
   - 対象者条件（テキストエリア。**任意**。例: 「週1回以上ビールまたは発泡酒を飲む人」）
2. **サンプル・割り付け**
   - 総有効サンプル数 N（数値。例: 500）
   - 割り付けパターン（ラジオボタン。`ui_config.yaml` の `ui.allocation_patterns`）
3. **コンセプト**
   - 動的フォーム。一覧の下に「＋ コンセプトを追加」ボタン、各コンセプトのカードに
     個別の削除ボタンを持つ
   - `stimulus_id` は入力させず `c1`, `c2`, … と連番で振る
4. **設問**
   - 既定2問（購入意向・新規性）を初期値として表示し、文言と選択肢を編集可能にする
   - 選択肢の順序は固定（順序尺度をシャッフルしない）

`survey_id` は調査名から機械的に作る（英数字だけを残し、実行時刻とランダムな識別子を足す）。
日本語の調査名がそのままテーブルのパーティション値やファイル名に入ると扱いにくいため。

**時刻だけでは一意にならない。** 日本語の調査名は slug が空になって `survey_{時刻}` に
潰れるので、同じ秒に2件投入すると ID が衝突する。衝突すると後続の実行が
`delete_survey_rows()` で先行実行の `panels` / `responses` を消し、別の調査の結果を
自分の調査として読むことになる。ランダムな識別子を足して避ける。

**`survey_id` は投入1回につき1つに固定する。** Streamlit はウィジェット操作のたびに
スクリプト全体を再実行するので、`SurveyForm.survey_id` を空のまま組み立てると呼ぶたびに
ID が作り直される。画面に出す ID・Volumes に置く YAML のファイル名・その YAML の
`survey.id`（＝ジョブが結果を書き込む ID）が食い違い、利用者は結果閲覧で自分の調査を
特定できなくなる。ID は `st.session_state` に持ち、調査定義は `build_survey_pair()` で
**1回だけ**組み立てて、検証・表示・投入で同じ dict を使い回す。投入が成功したら ID を捨て、
次の調査には新しい ID を振る（同じ ID で投げ直すと先行実行の結果を上書きしてしまう）。

### 3.2 対象者条件 → スクリーナー

条件文は**自然言語のまま** `screening.conditions` に入る（`SPEC_PHASE1.md` §4.2）。

```yaml
screening:
  model: { deployment: "...", max_tokens: 256 }   # ui_config の survey_defaults.screening から
  prompt: { system: "...", rule: "..." }
  persona_card: { attributes: [...], include_summary: true, persona_fields: [...] }
  mode: "infer"
  conditions:
    - "週1回以上ビールまたは発泡酒を飲む人"
  logic: "all"
  oversample_factor: 4
  batch_size: 20
```

`mode` が `infer` 以外のときは `model` / `prompt` / `persona_card` を書かない。
判定の LLM を呼ばないので使い道が無く、残すと「設定したつもり」の記録になる。

**選択肢や `pass_if` を UI 側で捏造しない。** `infer` は候補をまとめて判定用 LLM に見せる方式で、
選択肢を提示しない。実際に提示していないものを調査定義に残すと、何を聞いたのか読み取れなくなる。
条件文はそのまま判定プロンプトの対象者条件になり、通過者のペルソナカードの前提文にもなる。

条件が空欄なら `screening` を書かない（＝スクリーニングなし）。

### 3.3 割り付け

`ui_config.yaml` の `ui.allocation_patterns` から選ぶ。3パターンとも
**`quotas.mode: proportion` で比率を渡す**。

- 均等2パターン … 性別 × 年代セルに等しい比率
- 国勢調査 … 対象年齢の範囲で再標準化した人口構成比（§5）

対象にした性別ごとに独立した年齢範囲でセルを作る（`SurveyForm.sex_ranges`）。
チェックを外した性別はセルを作らず調査対象から除外され、性別によって異なる年齢範囲を
指定してもよい。全体条件（`panel.filters`）は、性別を1つしか選ばなければ従来どおり
その性別・年齢範囲で絞り、複数性別（範囲が違う場合を含む）を選んだときは性別条件を付けず
選ばれた範囲すべてを覆う年齢の envelope にする。性別・年齢の厳密な絞り込みは
割り付けセル条件（`panel.quotas.cells`）が担うため、これでも対象外のペルソナは混じらない。

整数人数への配分は `persona_sim.panel.quotas.allocate_cell_sizes()` の最大剰余法が行う
（`SPEC_PHASE1.md` §4.1）。**UI 側で丸め処理を書かない。** 書くと CLI 経由と UI 経由で
人数が食い違いうる。

対象年齢の範囲が刻み幅で割り切れない場合（例: 25〜60歳を10歳刻み）はエラーにする。
端数のセルを黙って作ると、そのセルだけ母集団が薄くなり、他セルと比べられなくなる
（この判定は性別ごとの範囲それぞれに対して行う）。

集計軸は刻み幅に追従する。10歳刻みなら `age_band_10` / `sex_x_age_band_10`、
5歳刻みなら `age_band_5` / `sex_x_age_band_5`。5歳刻みで割り付けたのに10歳刻みでしか
集計できないと、割り付けたセルの構成を確認できない。

### 3.4 事前チェックと見積もり

「調査を開始する」の押下前に、次を画面に出す。

| 表示 | 出どころ |
|---|---|
| 見積もり総セッション数 | `validate_static()` の `Estimate.sessions` ＋ `Estimate.screener_sessions` |
| 入力／出力トークン数の見込み | `estimate_cost()`。本調査＝`Estimate.answers` × `survey_answer` の実績値、判定＝`Estimate.screener_sessions` × `screener_session` の実績値 |
| 予算目安（円） | `estimate_cost()`。上のトークン数を `ui.estimation_benchmarks.pricing` の単価・為替・安全側の係数で換算 |
| 想定所要時間 | `回答件数 ÷ answers_per_min` ＋ `判定回数 ÷ sessions_per_min` |
| 有効サンプル数／案 | `Estimate.effective_n_per_stimulus` |
| 対象者（例: `男性 20〜69歳（5セル・計 500名）`） | `uiconfig.summary.target_summary()` |

**誰に聞くのかを開始前に必ず出す。** 性別チェックの外し忘れや年齢範囲の取り違えは、
出さないと結果を見るまで気づけない。文言は `panel.filters` ではなく
`panel.quotas.cells` から作る。複数性別のときの `filters` は年齢の envelope だけで
性別条件を持たないため（§3.3）、`filters` を見ると「性別の指定なし」に見えてしまう。

**セッション数も回答件数も UI で数え直さない。** 数え方を2箇所に持つと、画面と CLI の
見積もりが食い違う。反実仮想モナディックなので本調査は `N × コンセプト数 × 設問数`
（設問数は画面で入力した問いの数。展開後の設問はその `コンセプト数` 倍で、どの設問も
記憶を持たないため1設問＝1セッションになる）、`infer` の判定は `候補数 ÷ batch_size` 回になる
（`SPEC_PHASE1.md` §5.3 / §4.2）。

**換算の単位を取り違えない。** 本調査の実績値は**1回答あたり**（`survey_answer`）で取っており、
`Estimate.answers` に掛ける。判定の実績値は1回で `batch_size` 人分を捌く**1セッションあたり**
（`screener_session`）なので `Estimate.screener_sessions` に掛ける。`estimation_benchmarks` が
この2つを分けて持つのは、入力トークンが桁違いなのに加えて単位が違うためで、同じ値では見積もれない。

セッション数は `remember` でまとまった設問群を1つに数えるので、**記憶を持つ設問がある調査では
回答件数と一致しない**（`persona_sim.run.memory.session_groups()`）。現行のコンセプト調査は
`memory: none` 固定なので1セッション＝1回答だが、**記憶を持つ調査種別を足すときは実績値の
取り方から見直すこと**。履歴を再生するぶん、回答あたりの入力トークンも増える。

**金額は画面の概算としてのみ出す。** 単価・為替・安全側の係数は `ui.estimation_benchmarks.pricing`
の設定値で、ワークスペースの実請求ではない。**実行メタデータの `estimated_cost` は
`null` のまま**（`SPEC_PHASE1.md` §9）。事前の目安と実際の請求を同じ数字として扱わせない。

入力トークンはコンセプト文の分量で増減するので、予算目安には `budget_margin` を掛けて
安全側で出し、分量で変動する旨を注記に添える。

通過者が不足すると抽出倍率が2倍・最大3回まで再試行される（§4.2 / E2）ため、
スクリーニング回数は上振れしうる。その旨を注記として併記する。

### 3.5 実行

1. `build_survey_pair()` で調査定義を組み立てる（dict と検証済み定義を同じ dict から得る）
2. `validate_static()` を実行。エラーがあれば**全件**を画面に出して停止する
   （`ValidationReport` は fail-fast せず全部集める。1つ直すたびに再実行させない）
3. 調査定義を Unity Catalog Volumes（または Workspace Files）に保存する
4. Databricks Jobs に投入し（§6）、`survey_id` と「結果閲覧ページで進行状況を確認してください」
   の案内を表示する

---

## 4. 画面2: 結果閲覧（`app/views/results.py`）

### 4.1 調査一覧

`runs` テーブルを SQL Warehouse から読み、`survey_type = 'concept'` で絞って新しい順に出す。
調査名は `runs.survey_name` から直接引く（§2.4 で列にしたので `metadata_json` を掘らない）。
種別で絞るのは、ほかの調査種別が同じテーブルに入っても画面に混ざらないようにするため。

**行が入るのは実行が完走したとき。** 走行中・失敗した調査は `runs` に行が無いので、
一覧にも出ない。「開始した調査は、完了すると一覧に出てくる」という見え方になる。

**一覧の表示名に `survey_id` を必ず添える。** 調査名は重複しうるので、名前と完了時刻だけでは
同名の別実行を見分けられない。調査設計ページが投入時に出す ID と突き合わせられるようにする。
取得後は、選んだ調査の `survey_id` と対象者（§3.4 と同じ `target_summary()`）を画面に出す。
これが無いと、別の調査の集計表を自分の調査だと思って読んでしまう。

**`survey_id` ごとに最新の1件へ SQL 側で畳む**（`QUALIFY ROW_NUMBER()`）。畳まないと、
画面が `survey_id` をキーに辞書へ入れ直すところで後勝ちの上書きが起き、新しい順に
並べているぶん**最古の行**が残る。調査定義は最新1件から復元するので、一覧に出る完了時刻と
実際に取得するデータが食い違う。

**引くのは画面が使う列だけ**（`survey_id` / `survey_name` / `finished_at` /
`aborted_reason`）。実行セッション数・成功・失敗・完了日時のメトリクスは、読み手に
何の数字か伝わらないので置かない（`docs/issues/20260805004.md`）。この時点で見えるのは
調査の選択と「データを取得」だけにする。**中断（`aborted_reason`）の警告だけは残す。**
データが欠けているサインで、黙って消すと不完全な集計を完全なものとして読んでしまう。

**一覧の取得結果はキャッシュする**（`app/lib/context.py` の `runs()`、TTL 60秒）。
Streamlit はウィジェットを触るたびにページ全体を再実行するので、素で呼ぶと調査を
選び直すだけで毎回ウェアハウスへ問い合わせが飛び、一覧が出るまで待たされる。
接続と設定はハッシュできないので `_` 始まりの引数名でキーから外し、`survey_type` だけを
キーにする。更新ボタンは置かない（実行は数分かかるので TTL で足りる）。

### 4.2 データの取得

調査を選んで「**データを取得**」を押したときだけ SQL Warehouse を叩く。
表示のたびに叩くと、ウェアハウスの起動と課金が意図せず走る。

取得するのは `responses ⋈ panels ⋈ personas_base` の結合結果1回ぶん。
集計表もグラフもローデータのダウンロードも、**すべてこの1回の取得結果から作る**。

```
fetch_answers()  →  answers_from_rows()  →  build_result()
                    （Spark 経由と同じ純関数）
```

`answers_from_rows()` と `build_result()` は CLI（Spark 経由）が通るのと同じ関数なので、
読み方が違っても数値は一致する。UI 側に集計の算術を書かない。

配列カラム（`answer_codes` / `options_order` / `flags`）は SQL 側で `to_json` して
**文字列で受け取る**。コネクタが配列を何で返すかは私的な接続オプションに依存するので、
そこに挙動を預けない。

**実行中の進捗は表示しない。** `run_survey()` は全セッション完了後に `responses` を一度だけ
書くので、走行中は Delta に1行も無い。セッション単位の進捗を出すには実行側にハートビートを
仕込む必要があり、今回の範囲では持たない。

### 4.3 結果ダッシュボード（完了時）

§4.2 で取得した結果から描く。`aggregates` テーブルは読まない（生データから作り直すので、
集計軸を画面側で変えられる余地も残る）。

> 集計には `SurveyDefinition` が要る。調査定義が永続化されているのは
> `runs.metadata_json.survey_definition` だけなので、選んだ1調査についてだけ
> そこから `survey_from_record()` で復元する（`SPEC_PHASE1.md` §9）。
>
> **復元するのは集計が読む範囲だけ**（`survey` / `panel` / `stimuli` / `questions` /
> `output` とスクリーニングの方式）。記録には実行当時の書き方が残るので、その後で
> 書き方を変えた block まで読むと、過去の調査の結果が**聞き方の文言を理由に**
> 読めなくなる。`main_survey`（model / prompt / persona_card）と `design` は集計側に
> 参照が無いので読まない。数字の意味を決める block はこれまでどおり厳格に読み、
> 読めなければ止める——黙って既定値で埋めると、誤った表を正しい表として読んでしまう。

**出すのはクロス集計表だけ。** グラフとサマリーカードは置かない
（`docs/issues/20260805002.md`）。表の作りは上から順に次の3つ。

1. **設問ごとのコンセプト表**: 集計対象の設問1問につき1表。表側＝コンセプト、
   表頭＝n・各選択肢・T2B・平均。セグメントは全体のみ。既定では購入意向 → 新規性の順
   （並びは調査定義の `questions:` のまま）。**畳む単位は `measure`** で、slot 展開された
   設問を設問IDで回すと同じ問いの表がコンセプトの数だけ並ぶ
2. **属性別のクロス集計表**: 表側＝`output.segments` の軸。コンセプトと設問を選ばせる。
   選択肢に出すのは**コンセプト × 設問の表だけ**で、コンセプト比較表とパネル構成表は出さない
3. ダウンロード（§4.4）

表の整形は `persona_sim/aggregate/tables.py` の `concept_axis_table()` /
`stacked_crosstab_table()` が行う。**どちらも集計をやり直さない。** 材料は
`build_result()` が返す `crosstabs`（`compute_metric()` を通った値）で、ここでは
並べ替えと表示用の整形しかしない。同じ数字が2通りに出る余地を作らないため。

「平均スコア」は `SPEC_PHASE1.md` §7.1 の定義に揃える
（選択肢番号を逆順スコア化し、5段階なら 1→5点とした平均）。

**統計的推測の語彙を画面に出さない。** p値・有意差・信頼区間は禁止（`AGENTS.md` 禁止事項）。
コンセプト間は相対比較として示す。

**回答が1件も無いコンセプトも n=0 の行として残す。** `group_by_segments()` は回答から
セグメント値を作るので、放っておくとコンセプトが表から消える。実査に出したのに出てこないのか、
そもそも出していないのかが読み分けられなくなる。

**調査定義の割り付けセルに無い属性値が集計対象に入っていたら注記を出す**
（`build_result()` の `notes`）。パネルと回答の対応が壊れているか、別の調査の結果を
見ている兆候であり、どちらも数字そのものは出てしまう。品質フラグと同じく止めはせず、
必ず表面化させる（`SPEC_PHASE1.md` §11）。

### 4.4 ダウンロード

**ボタンは1つだけ**で、集計表とローデータを **xlsx 1ファイル**（シート2枚）で渡す。
`st.download_button` は1ファイルしか返せないため、2つ渡すには同梱するしかない。

| シート | 内容 |
|---|---|
| `集計表` | 全設問を縦に積んだクロス集計表（コンセプト × セグメント、全体を含む）。n・各選択肢・T2B・平均 |
| `ローデータ` | `responses` 全件に属性・コンセプト名・選択肢ラベルを付けたもの |

書き出すのは `persona_sim/aggregate/export.py` の `write_ui_workbook()`。
`Path` への書き出し専用でバイト列を返さないので、一時ディレクトリに書いてから
読み直し、`st.download_button` に載せる。

**`write_xlsx()`（§7.4 の `report.xlsx`）とは別関数にする。** シート構成が違うので
共用すると、UI の都合で CLI の出力が変わる。

集計表シートは1枚なので、これまで概要シートに載せていた **E3・E4 の注記は表の上に置く**。
表だけ配ると注記が読まれずに終わる（`SPEC_PHASE1.md` §11）。
設問ごとに選択肢ラベルが違うため、選択肢列は番号だけ（`選択肢1`〜）に揃え、
ラベルは同じく表の上の凡例に出す。

**ローデータに回答時刻（`responses.ts`）は載せない。** `timestamp` 列はコネクタが
タイムゾーン付きの `datetime` で返し、openpyxl はそれをセルに書けないので、
ダウンロードが `TypeError: Excel does not support timezones in datetimes` で丸ごと
失敗する（`docs/issues/20260805004.md`）。tz を落として書くこともできるが、どの
タイムゾーンで出すかを決めないまま値だけ出すことになるので載せない。診断に要る
`latency_ms` / `attempt` は残す。CLI の `responses_raw.csv`（§7.4）は Spark 経由の
別経路で、CSV はタイムゾーンを扱えるためそちらは変えていない。

ローデータには `stimulus_name` と `answer_labels` を足す（`persona_sim/aggregate/rawdata.py`）。
`responses` が持つのは `stimulus_id` と**提示順**の選択肢番号だけで、そのままでは
どのコンセプトに何を答えたのか読めない。**ラベルを引く前に `to_defined_codes()` で
定義順へ戻す。** 飛ばすと、選択肢をシャッフルした設問で別の選択肢の名前が付く（§2.3）。

### 4.5 帰属表示

Nemotron-Personas-Japan は CC BY 4.0 で、帰属表示が義務（`SPEC.md` §15.3）。
文言は `persona_sim/__init__.py` の `DATASET_ATTRIBUTION` / `OUTPUT_DISCLAIMER` を使い、
フッタと About、および出力ファイルに含める。国勢調査データを使った場合はその出典も併記する
（`persona_sim/data/README.md`）。

---

## 5. 国勢調査人口構成比

参照テーブルは `persona_sim/data/census_population_by_sex_age5.csv`
（列: `sex` / `age_min` / `age_max` / `population`）。出典・年次・取得日・加工内容は
`persona_sim/data/README.md` に記載する。出典は総務省統計局「国勢調査」（e-Stat）で、
政府統計の利用にあたり出典を明示する。

`persona_sim.uiconfig.census` が読み、対象年齢の範囲で比率を再標準化する
（全年齢に対する比率ではなく「20〜69歳の中での構成比」）。5歳階級で持ち、
10歳刻みが要るときは2つずつ束ねる。

**CSV が無い場合は国勢調査パターンを選択不可にする。** 均等割り付けに黙って
フォールバックしない。数値を持たないまま「人口構成比で割り付けた」と表示する方が害が大きい。

---

## 6. 非同期実行

### 6.1 Databricks Jobs API で投入する

`databricks-sdk` は既に依存にある。UI は `WorkspaceClient.jobs` で事前定義ジョブを
`run_now` により投入する。ジョブは単一の `notebook_task`（`notebooks/run_survey.ipynb`）で、
ノートブック内で `validate` → `panel` → `screen` → `run` → `aggregate`（export 含む）を
順に実行する。`job_parameters` は `notebook_task` でも `python_wheel_task` と同じ仕組みで
渡せるため、アプリ側（`persona_sim/uiconfig/jobs.py`）に変更は要らない。

アプリ内のバックグラウンドスレッドで回さない。Databricks Apps のコンテナは再起動しうるし、
`run_survey()` は完走まで `responses` を書かないので、途中で落ちるとその実行分が丸ごと消える。

### 6.2 `runs` に足す列

進捗表示をしないので、実行状態の列（`status` / `job_run_id` / `error_message`）は**持たない**。
完走時に1行 append される現在の挙動が、そのまま「完了したら一覧に出る」と一致する。

足すのは調査を識別するための2列だけ（`SPEC_PHASE1.md` §2.4、実装済み）。

| 列 | 用途 |
|---|---|
| `survey_name` | 一覧の表示名。毎回 `metadata_json` を掘らずに済む |
| `survey_type` | 調査の種類。一覧を種別で絞る |

### 6.3 デプロイ前提

**Apps のソースコードパスはリポジトリ直下**（`app.yaml` の場所）とし、`app/` は指さない。
Apps はソースディレクトリ配下だけを同期し、そのルートの `requirements.txt` を install する。
`app/` をソースにすると、その外にある `persona_sim` が同期対象から外れ、ホイールを作って
Volumes に置き直す運用が更新のたびに発生する。直下をソースにすればその手順ごと無くなる。

ルートの `requirements.txt` に入れる依存: `streamlit` / `databricks-sdk` /
`databricks-sql-connector` / `PyYAML` / `openpyxl`。**plotly は入れない**
（§4.3 でグラフを置かなくなったため）。**pyspark も入れない**
（Apps は Spark ドライバを持たない軽量コンテナで、読み取りは SQL Warehouse 経由）。
パイプライン実行基盤の依存は `requirements-pipeline.txt` に分けてある。

`persona_sim` 本体は install しない。同期されたソースを `app/lib/__init__.py` が `sys.path`
経由で直接 import する。事前定義ジョブも Git フォルダ内の `notebooks/run_survey.ipynb` から
同じリポジトリのコードを読むので、アプリとジョブでバージョン差による数値の食い違いが起きない。

作業ディレクトリはリポジトリ直下になる。**アプリ側で cwd 相対のパスを書かないこと**
（`config/ui_config.yaml` の既定は `app/lib/context.py` でモジュール相対に解決している）。

`app.yaml` に書けるのは `command` と `env` の2キーだけ。リソース（SQL Warehouse・ジョブ・
Volume）はワークスペースのアプリ設定か Bundle 側で宣言し、`valueFrom` で参照する。
SQL Warehouse は **ID** が渡ってくるので、`http_path` は `/sql/1.0/warehouses/{id}` を
アプリ側で組み立てる。ポートは設定しない（Apps が `DATABRICKS_APP_PORT` から注入する）。

アプリのサービスプリンシパルに要る権限、ジョブの構成、疎通確認の手順は
**`deploy/README.md`** にまとめてある。具体的なワークスペース名・カタログ名は
ここに書かず、環境変数とアプリ設定で渡す。

---

## 7. 実装状況

| 範囲 | 状態 |
|---|---|
| `config/ui_config.yaml`（§2） | 実装済み |
| `persona_sim/uiconfig/`（§2・§3.2〜§3.4・§5） | 実装済み |
| `persona_sim/storage/warehouse.py`（§4.2） | 実装済み。**実接続は未検証** |
| `persona_sim/uiconfig/jobs.py`（§6.1） | 実装済み。**実投入は未検証** |
| `runs` の `survey_name` / `survey_type`（§6.2） | 実装済み |
| `app/` の Streamlit アプリと `app.yaml`（§3・§4） | 実装済み。画面1はローカルで動作確認済み |
| ソースコードパスをリポジトリ直下にする構成（§6.3） | 実装済み。ローカルで `streamlit run app/app.py` の起動を確認。**Apps での再デプロイは未実施** |
| `deploy/README.md` の手順 | 記載済み。**実環境でのデプロイは未実施** |
| 国勢調査 CSV の実数値投入（§5） | **未投入**（当該割り付けは選択不可） |
| `estimation_benchmarks` の実測値（§3.4） | **仮の値**。特に `screener_session` は根拠が無い |
| セッション単位の進捗表示（§4.2） | **持たない**（仕様として不要と判断） |
