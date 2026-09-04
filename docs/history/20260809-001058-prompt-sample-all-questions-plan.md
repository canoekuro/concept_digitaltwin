# 実行プロンプトの記録を1ペルソナの全設問ぶんに広げる（計画）

- 日時: 2026-08-09 00:10:58 UTC
- 対象: `SPEC_PHASE1.md` §9 の `prompt_sample`
- 起点: 「いま実行すると1セッション分しか保存されないが、1サンプルの全設問ぶんを
  保存できるか」という利用者からの問い

## 1. 現状と問題

`run_metadata.json` / `prompt_sample.md` に残る「実際に送ったプロンプト」は、
**1ペルソナの1設問ぶんだけ**である。

- `persona_sim/run/run.py` `_pick_prompt_sample()` … 全セッションをキー順に並べ、
  **先頭の1セッション**だけを採る。
- `persona_sim/run/session.py` `sample_prompt()` … そのセッションの **`units[0]`**、
  つまり先頭設問だけを組み直す。

記憶を持つ設問が1つも無い調査（`remember` を書かない既定）では1設問＝1セッションなので、
結果として「1名の1設問」しか残らない。記憶を持つ調査でもセッション先頭の1問だけになる。

これが実害になるのは次の2点。

- 設問ごとに `slot`・`randomize_options`・`remember` が違うのに、記録には1問しか無い。
  「結局どう聞いたのか」を設定値以外から読めるようにするという §9 の目的を、
  2問目以降について果たせていない。
- 過去に `docs/history/20260807-141500-high-severity-fixes-result.md`（H2）で、
  2問目以降の提示順が定義順に化けるバグが**事後に気づけなかった**理由として
  「`prompt_sample` も1名の先頭設問だけ」であることが挙がっている。

## 2. 方針

**1ペルソナを1名決め、その人に聞いた全設問ぶんのメッセージ列を残す。**

`units[0]` に限っていたのは、記憶を持つ設問のプロンプトには先行設問の**回答本文**が
入り、それは LLM の出力なので純関数では再現できないためである（先頭ユニットは必ず
記憶を持たない、という性質に乗っていた）。ここは **`responses` に書き出し済みの
`answer_raw` を読んで再生する**ことで解く。実際に送った内容と一致し、再開実行でも
前回の回答が読めるので欠けない。

これに伴い、記録の単位を `system` / `user` の2本から**メッセージ列そのもの**に改める。
記憶を持つ設問は `system` / `user` / `assistant` / `user` … という列で送られるので、
`user` 1本では実際に送った内容を表現できない。

## 3. 変更対象と具体的な変更内容

### `persona_sim/run/session.py`

- `SampleMessage`（`role` / `content`）を新設する。
- `PromptSample` を「1設問ぶんの実メッセージ列」に作り替える。
  `persona_uuid` / `stimulus_id` / `question_id` / `sequence` / `messages` /
  `missing_answers`。
- `sample_prompt()` を `sample_prompts(session, ctx, answers)` に改める。
  `run_session()` と同じループで全ユニットを回し、`turns_by_question` に
  `answers[question_id]` を差し込んで `replayed_messages()` に渡す。
  回答が引けなかった設問は目印文字列（`MISSING_ANSWER`）で埋め、その設問IDを
  `missing_answers` に残す。**黙って一致しない記録を作らない。**

### `persona_sim/run/run.py`

- `RunResult.prompt_sample: PromptSample | None` を
  `prompt_samples: tuple[PromptSample, ...]` に置き換える。
- `_pick_prompt_sample()` を `_pick_prompt_samples()` に改める。キー順で1名を決めた後、
  **そのペルソナの全セッション**を ask order（`memory.ask_order()`）で並べ、
  全ユニットぶんを組み直す。
- `_recorded_answers()` を新設し、`responses` から当該1名の `answer_raw` を引く。
  `_apply_straightline()` の後、書き出しが済んだ状態で呼ぶ。

### `persona_sim/metadata.py`

- `prompt_sample` の構造を変更する（**破壊的変更**）。
  `{persona_uuid, questions: [{stimulus_id, question_id, sequence, messages, missing_answers}]}`。
- `render_prompt_sample()` を全設問ぶんの見出し付き出力に変更する。
  `missing_answers` があれば、その設問の節に注記を出す。

### 追随

- `SPEC_PHASE1.md` §9 の説明文と JSON 例。
- `notebooks/quickstart.ipynb` の説明セル（「1ペルソナ分」の文言）。
- `tests/test_run_spark.py` の `prompt_sample` 系4テスト。
- `docs/history/` の plan・result ペアと `CHANGELOG.md`。

## 4. 検証方法

- `./scripts/run-tests.sh`（非 Spark）。
- Spark テスト（`-m spark`）。この環境には Java 21 があるので実行を試みる。
- 新規・改修テストで押さえること。
  - 全設問ぶんが並ぶこと（設問数と `question_id` の集合が調査定義と一致）。
  - 聞いた順（ask order）に並ぶこと。
  - 記録が**実際にエンドポイントへ送ったメッセージ列と一致**すること。
    記憶なしの定義と、記憶ありの定義（`with_remember(..., "full_session")`）の両方。
  - 何度実行しても同じ1名が残ること（再開実行で全スキップになる場合を含む）。
  - `prompt.system` の上書きが反映されること。

## 5. 残る論点

- `run_metadata.json` は `runs` テーブルの `metadata_json` 列にも入るため、
  1名×全設問ぶんだけ列が太る。利用者の判断で**全文を入れる**方針とした。
