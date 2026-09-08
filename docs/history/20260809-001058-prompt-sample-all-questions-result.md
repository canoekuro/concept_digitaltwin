# 実行プロンプトの記録を1ペルソナの全設問ぶんに広げる（結果）

- 日時: 2026-08-09 00:10:58 UTC
- 計画: [20260809-001058-prompt-sample-all-questions-plan.md](20260809-001058-prompt-sample-all-questions-plan.md)

## 1. 何を変えたか

`run_metadata.json` / `prompt_sample.md` に残る「実際に送ったプロンプト」を、
**1ペルソナの1設問**から**1ペルソナの全設問**に広げた。あわせて、記録の単位を
`system` / `user` の2本から**メッセージ列そのもの**に改めた。

### `persona_sim/run/session.py`

- `SampleMessage`（`role` / `content`）と `MISSING_ANSWER` を新設。
- `PromptSample` を「1設問ぶんの実メッセージ列」に作り替えた。
  `persona_uuid` / `stimulus_id` / `question_id` / `sequence` / `messages` /
  `missing_answers`。
- `sample_prompt()` を `sample_prompts(session, ctx, answers)` に置き換えた。
  `run_session()` と同じループで全ユニットを回す。違うのは [assistant] に置く回答を
  エンドポイントではなく `answers`（＝`responses.answer_raw`）から引くところだけ。
  引けなかった設問は `MISSING_ANSWER` で埋め、その設問IDを `missing_answers` に残す。

`units[0]` に限っていた理由（先行設問の回答は純関数で再現できない）は、書き出し済みの
`responses` を読むことで解いた。**目印で埋めた事実を必ず残す**のは、黙って [assistant] を
落とすとメッセージ列の形が変わり、「実物と一致する」という前提が読み手に分からないまま
崩れるため。

### `persona_sim/run/run.py`

- `RunResult.prompt_sample: PromptSample | None` →
  `prompt_samples: tuple[PromptSample, ...]`。
- `_pick_prompt_sample()` → `_pick_prompt_samples()`。1名の決め方（セッションキー順の先頭）は
  従来どおりで、そのペルソナの**全セッション**を ask order で並べて全ユニットを組み直す。
  セッションキー順は `stimulus_id` の辞書順になるので、聞いた順とは別に整列している。
- `_recorded_answers()` を新設。`responses` から当該1名の `answer_raw` を引く。
  組み直しは `_apply_straightline()` の後、書き出しが済んだ状態で行う。

### `persona_sim/metadata.py`

- `prompt_sample` の構造を変更（**破壊的変更**）。
  旧: `{persona_uuid, stimulus_id, question_id, system, user}`
  新: `{persona_uuid, questions: [{stimulus_id, question_id, sequence, messages, missing_answers}]}`
- `render_prompt_sample()` を全設問ぶんの出力に変更。設問ごとに節を切り、
  `[system]` / `[user]` / `[assistant]` を見出しつきのコードブロックで並べる。
  `missing_answers` があれば、その節に「実際に送った文面とは一致しない」と注記する。

### 追随した文書

- `SPEC.md` §7.4（出力ファイル一覧）と §9（説明文・JSON 例）。
- `notebooks/quickstart.ipynb` の説明セル。

## 2. 検証結果

- `ruff check persona_sim/ tests/` パス。
- 非 Spark テスト **649件全件パス**（変更前640件、9件追加）。
- Spark テスト **84件全件パス**（変更前82件、2件追加）。内訳は末尾の「追記」節。

### 追加したテスト

**`tests/test_session.py`（Spark 不要・5件）** — 記録が実送信と一致することを、
Spark を起動せずに押さえる。`_RecordingClient` が送ったメッセージ列を控え、
`sample_prompts()` が組み直した列と突き合わせる。

- 全設問ぶんが残ること（1設問で終わらないこと）。
- 記憶なしの定義で実送信と一致すること。
- 記憶ありの定義で、[assistant] を挟んだ列まで一致すること
  （最後の設問は `system` + `user`/`assistant` × 5 + `user`）。
- 回答を引けなかった設問が `missing_answers` に出て、`MISSING_ANSWER` で埋まること。
- ペルソナが読み込まれていなければ空を返すこと。

**`tests/test_metadata.py`（新規・Spark 不要・4件）** — `prompt_sample.md` の組み立て。
全設問が本文に出ること、目印で埋めた事実が注記されること、埋めていなければ注記が
出ないこと、`image_mode: native` の画像が URI の目印として出ること。

**`tests/test_run_spark.py`** — 既存4件を新構造に合わせ、2件追加した。

- `test_prompt_sample_covers_every_question_of_one_persona`（新）: 3コンセプト×6設問で、
  ask order どおりに全設問が並び、各設問の `stimulus_id` が**そのペルソナの**
  `assigned_stimuli` の slot 番目と一致すること。提示順が定義順に化けていた不具合
  （`20260807-141500-high-severity-fixes-result.md` H2）を、今度は記録から検知できる。
- `test_prompt_sample_replays_remembered_answers`（新）: 記憶ありの定義で、
  実行時に送った列と記録が一致すること。
- 既存の `is_written_as_markdown` / `is_stable_across_runs` / `reflects_prompt_overrides` /
  `matches_what_was_actually_sent` は、全設問を回して確かめる形に書き換えた。

## 3. 影響と注意

- **`run_metadata.json` の `prompt_sample` は互換性が無い。** 旧構造を読む外部の道具が
  あれば追随が要る。リポジトリ内の読み手は `render_prompt_sample()` と
  `notebooks/quickstart.ipynb`（`prompt_sample.md` を表示するだけ）で、いずれも追随済み。
  アプリ（`app/`）は `prompt_sample` を読んでいない。
- **`runs` テーブルの `metadata_json` 列が太る。** 1名×全設問ぶんの全文が入る。
  利用者の判断で全文を入れる方針とした（メタデータだけを受け取った相手にも
  「どう聞いたか」が伝わるようにするため。§9）。
- `responses` への読み出しが実行あたり1回増える。1ペルソナぶんに絞った読み出しで、
  同じ経路で既に `_apply_straightline()` が全件を読んでいるため、実行時間への影響は小さい。

## 4. 未対応

- 記録するペルソナの選び方は従来どおり「セッションキー順の先頭」で変えていない。
  「回答が全設問そろっている人を選ぶ」ようにはしていない——選び方が実行の成否で変わると、
  同じ調査で同じ1名が残るという性質が崩れるため。そろっていない場合は
  `missing_answers` で表面化させる方に寄せた。
- 複数ペルソナぶんを残す設定は入れていない（要望があれば別途）。

## 追記: Spark テストの実行結果

この環境には Java 21 があるため、CI 任せにせず手元で実行した。**84件全件パス**。

| 対象 | 件数 | 所要 |
|---|---|---|
| `tests/test_run_spark.py` | 21 passed | 388.71s |
| その他の Spark テスト（panel / screening / aggregate ほか） | 63 passed | 738.86s |

`tests/test_run_spark.py` には今回追加した2件
（`test_prompt_sample_covers_every_question_of_one_persona` /
`test_prompt_sample_replays_remembered_answers`）が含まれる。
`tests/test_screening_spark.py` は `write_metadata()` を通るため、
`prompt_sample` の構造変更の影響を受けうるが、こちらも全件パスした。

これで計画に挙げた検証はすべて済み、未実行の項目は無い。
