# 設問ごとにシステムプロンプトを使い分けられるようにする（結果）

- 日時: 2026-08-13 08:25:12 UTC
- 計画: [20260813-082512-per-question-system-prompt-plan.md](20260813-082512-per-question-system-prompt-plan.md)
- 起点: `docs/issues/202608131659.md`

## 1. 変更内容

計画どおり。既定は一切変えていない（`systems` も `questions[].system` も省略可）。

| ファイル | 変更 |
|---|---|
| `persona_sim/panel/schema.py` | `PromptConfig.systems: Mapping[str, str]` と `system_for(key)`、`Question.system: str \| None` を追加 |
| `persona_sim/panel/loader.py` | `prompt.systems` と `questions[].system` を読む。`_prompt_systems()`（空名・空文面を拒否）と `_reject_unknown_system_prompts()`（未定義の名前を指したら停止）を新設 |
| `persona_sim/run/prompt.py` | `[system]` を設問から引く。`replayed_messages()` は**列の最後＝いま聞いている設問**のものを使う |
| `persona_sim/panel/validate.py` | `_check_system_prompts()`。どの設問からも指されていない `systems` を `W_UNUSED_SYSTEM_PROMPT` で警告 |
| `app/config/ui_config.yaml` | `main_survey.prompt.systems` に `purchase_intent` / `novelty`。既定2問に `system:` を紐づけ |
| `examples/survey_sample.yaml` | 同内容を書き写し、`q_intent_*` / `q_novelty_*` に `system:`（`q_reason_*` は既定のまま） |
| `SPEC.md` | §3 スキーマ例・設問フィールド表、§6.1「設問ごとに `[system]` を使い分ける」、§6.2 prefix cache の注記 |
| `docs/GUIDE_SURVEY_DEFINITION.md` | §2 に書き方の節（実例は自動検証の対象になる完全な定義で記述） |
| `docs/SPEC_UI.md` | §2 に、既定設問が `system` を持つことと画面に出さない理由 |

**UI 側のコードは1行も変えていない。** `app/views/survey_design.py` の
`{**default, "text": ..., "options": ...}` と `uiconfig/build.py::_expand_questions()` の
`{**dict(question), ...}` が `system` をそのまま運び、`uiconfig/loader.py` は `prompt` を
素通しの `Mapping` として扱うため。**この素通し構造が効いていることは
`test_the_system_prompt_survives_slot_expansion` で固定した**——落とすと案ごとに違う
`[system]` で聞くこと（1案目だけ書き分けが効く 等）になり、症状が出にくい。

## 2. 設計判断の記録

- **設問に書くのは名前で、文面ではない。** 設問はコンセプト数だけ `slot` 展開されるので、
  文面を持たせると同じ長文が展開数ぶん複製され、直すときに1箇所取りこぼすと案によって
  違うプロンプトで聞いたことになる。
- **未定義の名前は `validate` ではなく読み込みで止める。** `run` は `validate` を通らずに
  実行できる（`cli.py`）。既定へ黙って落とすと、書き分けたつもりの設問が既定の `[system]`
  で聞かれ、実行後に `prompt_sample.md` を読み比べるまで気づけない。逆向き（書いたのに
  誰も指していない）は結果の意味を変えないので警告に留めた。
- **記憶を跨ぐ場合の `[system]` はいま聞いている設問のもの。** 先行設問のものを使うと、
  これから答えさせる設問が別の指示で聞かれる。列は毎ターン組み直す方式なので、
  先行ターンの回答は履歴として残ったまま切り替わる。`replayed_messages()` の docstring に
  3つ目の不変条件として書いた。
- **prefix cache は損なわれない。** 記憶を持たない調査の投入順は
  `(stimulus_id, question_id, persona_uuid)` で並ぶ（`session.py`）ため、同じ設問＝同じ
  `[system]` が連続する。§6.2 に明記した。

## 3. 検証結果

- `./scripts/run-tests.sh`（非 Spark）: **673件全件パス**（変更前662件、11件追加）。
- **変更前のテストが新しいコードでそのまま全件通る**ことを確認（`tests/` だけ変更前に
  戻して 662件パス）。既存の調査定義の入力が変わっていないことの裏づけ。
- 追加したテストは変更前のコードでは収集時点で落ちる（`PromptConfig.__init__() got an
  unexpected keyword argument 'systems'`）ことを確認。
- `uv run ruff check persona_sim app tests`: パス。
- `examples/survey_sample.yaml` を読み込んで `replayed_messages()` を実行し、`[system]` が
  実際に3通りになることを目視で確認した（LLM 呼び出しなし）。

| 設問 | `[system]` の末尾の指示 |
|---|---|
| `q_intent_1` | ふだんの買い物の仕方・使う場面・支払える金額に照らして、実際に手に取るかどうかで判断 |
| `q_novelty_1`（`remember: ["q_intent_1"]`） | すでに知っている商品と引き比べて、どこが違うのかで判断。買いたいかどうかは考えない |
| `q_reason_1`（`system` 省略） | 既定の `prompt.system` |

`q_novelty_1` のメッセージ列は `[system] / [user] / [assistant] / [user]` で、
`[assistant]` に購入意向の回答が残ったまま `[system]` だけが新規性用に替わっている。

## 4. 未対応・注意

- **Databricks 実環境での確認は未実施**（この環境に接続が無い）。Streamlit 画面の目視も未実施。
  画面のコードは変えていないが、既定設問に `system` が増えているので、投入前に
  「組み立てた調査定義を見る」で `system:` が載っていることを確認するとよい。
- **プロンプトの文面は暫定値。** 現行の1本を土台に、購入意向・新規性それぞれの判断軸を
  1〜2行足しただけ。issue の「精度が高まる」実測で使った文面そのものではないので、
  起票者の手元の文面に差し替えること。差し替えても構造は変わらない。
- **精度が上がったかは測っていない。** この変更は書き分けを可能にするところまで。
  効果の検証は実データでの比較になる。
- 見積もり（`uiconfig/cost.py`）は実測ベンチマークを持つだけなので無改修。文面を大きく
  伸ばす場合は `input_tokens_per_answer` を取り直すこと。
