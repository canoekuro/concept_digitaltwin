# 設問ごとにシステムプロンプトを使い分けられるようにする（計画）

- 日時: 2026-08-13 08:25:12 UTC
- 対象: `persona_sim/panel/{schema,loader,validate}.py` / `persona_sim/run/prompt.py` /
  `app/config/ui_config.yaml` / `examples/survey_sample.yaml` /
  `SPEC_PHASE1.md` §3・§6.1・§6.2 / `docs/GUIDE_SURVEY_DEFINITION.md` §2 / `docs/SPEC_UI.md` §2
- 起点: `docs/issues/202608131659.md`

## 1. 現状と問題

issue の内容は2文。

> - 現状のコンセプト調査に関して、購入意向の質問と新規性の質問で、別々のシステムプロンプトを
>   与えた方が精度が高まることが分かった。現状は1つのシステムプロンプトを与えているが、
>   これを分けられるようにしたい。
> - 方策として、ui_config 側で購入意向用システムプロンプトと新規性用のシステムプロンプトを
>   書き分けるようにする。そのうえで、調査定義側で各Qと使用するシステムプロンプトを
>   紐づけて与えられるようにする。

`[system]` は調査全体で1本しかない。`PromptConfig.system`（`main_survey.prompt.system`）を
`initial_messages()` / `replayed_messages()`（`persona_sim/run/prompt.py`）がそのまま
先頭メッセージに置いており、設問ごとに変える手段が無い。

購入意向は「自分の金で買うか」、新規性は「既存品と何が違うか」を判断させる問いで、
1本の指示で兼ねると片方に寄る。実測で精度差が出たという起票者の判断が根拠。

## 2. 方針（利用者と確認済み）

3点を確認して決めた。

1. **スキーマは名前付きマップ＋参照。** `main_survey.prompt.systems` に `{名前: 文面}` を
   並べ、`questions[].system` にその**名前**を書く。設問への全文直書きは採らない——設問は
   コンセプト数だけ `slot` 展開されるので、文面を持たせると同じ長文が展開数ぶん複製され、
   直すときに取りこぼすと案によって違うプロンプトで聞いたことになる。
2. **記憶（`remember`）を跨いでよい。** その場合 `[system]` は**いま聞いている設問**のもの。
   混在を読み込みで止める案は採らない（「購入意向を覚えたまま別プロンプトで新規性を聞く」
   という、まさに issue の並びが書けなくなる）。メッセージ列は毎ターン組み直す方式
   （`replayed_messages()`）なので、切り替えは自然に収まる。
3. **調査設計画面は無改修。** 文面は調査ごとに変えるものではなく運用側が固定する値なので、
   `ui_config.yaml` に置く。画面に出すと毎回入力させる事故（消し忘れ・貼り間違い）の
   余地が増える。

## 3. 変更内容

### スキーマ（`persona_sim/panel/schema.py`）

- `PromptConfig.systems: Mapping[str, str]`（既定 `{}`）を追加。
- `PromptConfig.system_for(key: str | None) -> str` を追加。`None` なら `system`、
  それ以外は `systems[key]`（存在は読み込みで保証済みなので素引き。既定へ黙って落とさない）。
- `Question.system: str | None`（`systems` の**名前**。既定 `None`）を追加。

### 読み込み（`persona_sim/panel/loader.py`）

- `_prompt()` の許可キーに `systems` を足し、`_prompt_systems()` で `{名前: 文面}` を読む
  （空名・空文面は停止。空文面を通すと省略と区別がつかない）。
- `_question()` の許可キーに `system` を足す。
- `_reject_unknown_system_prompts()` を新設し、`systems` に無い名前を指した設問を
  **読み込みの時点で停止**させる（使える名前を添える）。綴り違いが既定に落ちると、
  実行後に `prompt_sample.md` を読み比べるまで気づけない。

### プロンプト組み立て（`persona_sim/run/prompt.py`）

- `initial_messages()`: `[system]` を `prompt_config.system_for(question.system)` に。
- `replayed_messages()`: `[system]` を `system_for(turns[-1].question.system)` に
  （＝いま聞いている設問のもの）。docstring の不変条件を2つ→3つに増やす。
- `session.py` は `survey.prompt` を渡すだけなので無改修。

### 検証（`persona_sim/panel/validate.py`）

- `_check_system_prompts()` を新設。**どの設問からも指されていない `systems`** を
  `W_UNUSED_SYSTEM_PROMPT` として警告（設問側の書き忘れが多い）。エラーにはしない。

### 設定・サンプル

- `app/config/ui_config.yaml`: `main_survey.prompt.systems` に `purchase_intent` /
  `novelty` を追加し、既定2問に `system:` を紐づける。`system:`（既定）は残す。
  文面は現行のものを土台に、購入意向は「ふだんの買い物・支払える金額に照らして実際に
  手に取るか」、新規性は「既に知っている商品との違いで判断し、買いたいかは考えない」を足す。
  **運用しながら調整する前提の値**である旨をコメントに残す。
- `examples/survey_sample.yaml`: 同じ内容を書き写し（冒頭コメントの同期要件）、
  `q_intent_*` / `q_novelty_*` に `system:` を付ける。このサンプルは
  `q_novelty_1: remember: ["q_intent_1"]` を持つので、記憶を跨ぐ実例そのものになる。
- `examples/survey_sample_smoke.yaml`: `prompt:` を意図的に省略しているので無改修。
- UI 側は無改修で通る（`survey_design.py` の `{**default, ...}` と
  `uiconfig/build.py::_expand_questions()` の `{**dict(question), ...}` が `system` を運ぶ。
  `uiconfig/loader.py` は `prompt` を素通しの `Mapping` として扱う）。

### ドキュメント

- `SPEC_PHASE1.md`: §3 スキーマ例と設問フィールド表、§6.1 に「設問ごとに `[system]` を
  使い分ける」節、§6.2 に prefix cache が損なわれない理由（並べ替えキーが
  `(stimulus_id, question_id, persona_uuid)` なので同じ設問＝同じ `[system]` が連続する）。
- `docs/GUIDE_SURVEY_DEFINITION.md` §2 に書き方の節（実例は自動検証に載る完全な定義で書く）。
- `docs/SPEC_UI.md` §2 に、既定設問が `system` を持つことと画面に出さない理由。

### テスト

- `tests/test_prompt.py`: 名前で切り替わる／未指定は既定に落ちる／`systems` を定義しても
  他の設問の入力は1バイトも変わらない／記憶を跨いでも今聞いている設問のものが載る。
- `tests/test_survey_loader.py`: `systems` が読める／未定義の名前で停止する／空文面で停止する。
- `tests/test_uiconfig.py`: 既定2問が別々の `system` を指す／slot 展開後も残る。
- `tests/test_validate.py`: 未参照の `systems` で警告が出る／参照されていれば出ない。

## 4. 検証方法

1. `./scripts/run-tests.sh`（非 Spark 全件）。
2. `uv run ruff check persona_sim app tests`。
3. `examples/survey_sample.yaml` を読み込み、`q_intent_1` / `q_novelty_1` /
   `q_reason_1`（`system` 省略）の `[system]` が実際に3通りになることを目視。

## 5. 影響範囲

- 後方互換。`systems` も `questions[].system` も省略可で、書かなければ入力は不変。
- `responses` / `runs` のスキーマ変更なし。実際に送った `[system]` は既存の
  `prompt_sample.md` にそのまま出るので記録の追加も不要。
- 見積もり（`uiconfig/cost.py`）は実測ベンチマークを持つだけなので無改修。文面の長さは
  現行と同程度に収める。
