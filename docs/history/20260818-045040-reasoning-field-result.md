# 選択式設問に reasoning フィールドを足せるようにした（結果）

- 日時: 2026-08-18 04:50:40 UTC
- 計画: [20260818-045040-reasoning-field-plan.md](20260818-045040-reasoning-field-plan.md)

## 1. 入れたもの

選択式の設問で、構造化出力のスキーマに `reasoning` を**番号より前のプロパティとして**足し、
理由を書かせてから番号を選ばせる。生成は前から進むので、順序がそのまま「考えてから答える」
順になる。書かれた理由は `responses.answer_reasoning` に残る。

```yaml
questions:
  - id: "q_novelty_1"
    type: "single"
    reasoning: true              # 既定 false
    reasoning_max_length: 80     # 既定 80

main_survey:
  model:
    max_tokens_reasoning: 512
    structured_output: "always"  # reasoning を使うなら必須
  prompt:
    rules:
      reasoning: "まず、そう考えた理由を{max_length}文字以内で書き、そのうえで当てはまる番号を選んでください。"
```

思考モード（`model.thinking`）は **OFF のまま**。不変条件（`AGENTS.md` / §13）は変えていない。
違いは、理由が記録に残り、予算で抑えられ、どの設問で使ったかが実行メタデータに残ること。

## 2. 変更点と、そう決めた理由

| ファイル | 変更 |
|---|---|
| `panel/schema.py` | `Question.reasoning` / `.reasoning_max_length`（既定は `DEFAULT_REASONING_MAX_LENGTH`）、`ModelConfig.max_tokens_reasoning`、`PromptRules.reasoning` と `for_question()` |
| `panel/loader.py` | 新キーの受け入れ。`{max_length}` を埋められるキーを `open` と `reasoning` に |
| `run/parsing.py` | `answer_schema()` に `reasoning` を先頭プロパティで追加、`ParsedAnswer.reasoning`、数字走査の抑止、`_refusal_target()` |
| `run/prompt.py` | `question_block()` が `rules.for_question()` を使い、reasoning 設問では字数を差し込む |
| `run/session.py` | `_budget_for()`、`ResponseRecord.answer_reasoning` |
| `run/run.py` | `RESPONSES_SCHEMA` に `answer_reasoning string`、responses の `merge_upsert(evolve_schema=True)`、straightline の select をテーブルが持つ列に限定 |
| `panel/validate.py` | `_check_reasoning()`（2つのエラーと `W_REASONING` 警告） |
| `panel/infer.py` | スクリーニング判定のレコードは `answer_reasoning=None` |
| `llm/fake.py` | reasoning を要求するスキーマには理由つきの JSON を返す |
| `storage/warehouse.py` | `RAW_RESPONSE_COLUMNS` に `answer_reasoning` |
| `metadata.py` | `model.reasoning_questions` と `model.max_tokens_reasoning` |
| `app/config/ui_config.yaml` / `examples/survey_sample.yaml` | 文面と予算だけ用意し、設問側は有効にしない |
| `SPEC_PHASE1.md` §2.3・§3・§6.3・§8・§13 / `docs/GUIDE_SURVEY_DEFINITION.md` §4（新設。以降を繰り下げ） | 仕様と書き方 |
| `AGENTS.md` | 不変条件「思考モードは常に OFF」に、理由を書かせたい場合の行き先を追記（計画には無かったが、レビュー時に真っ先に読まれる場所なので揃えた） |

決めごとは3つ。いずれも利用者と確認済み。

1. **`structured_output: always` を必須にした**（`validate` がエラー）。`auto` の
   フォールバック（正規表現パース）は最初に現れた数字を採るので、**理由文の中の
   「205円」「3人家族」を回答番号として拾う**。パース失敗がもっともらしい回答に
   化けるのが最悪の壊れ方なので、黙って落ちる経路を残さない。あわせて reasoning 設問では
   数字の走査そのものを止め、JSON の `answer` からしか番号を採らないようにした。
2. **refusal 判定から理由の本文を外した**。理由の散文はこちらが書かせたものなので、
   「わかりません」「判断できません」が拒否の目印にならない（購入意向の既定選択肢に
   「わからない」がある以上、言い換えは高頻度で出る）。`refusal` は `QUALITY_FLAGS` に
   含まれ「フラグ除外後の n」を減らすので、誤検出は集計の n を実態より小さくする。
   真の拒否は番号を返せないので `parse_error` で表面化する。**理由の外側**に書かれた
   前置き・説教は判定対象に残している。
3. **Web UI の既定設問は `reasoning: false` のまま。** 文面と予算だけ `ui_config.yaml` に
   用意した。費用と所要時間が数倍になり、画面の見積もり式
   （`estimation_benchmarks.survey_answer.output_tokens_per_answer: 15`）が合わなくなる。

JSON Schema に `maxLength` は入れていない。strict スキーマでの対応がエンドポイント依存で、
拒否されると `always` の下では調査そのものが止まる。字数は指示文で伝え、打ち切りは
`max_tokens_reasoning` が担う。

### 既存テーブルの扱い

`responses` に列が1本増えたので、responses への `merge_upsert()` に `evolve_schema=True` を
付けた（前例: `panel/screening.py`）。加えて `_apply_straightline()` の `select` を
「テーブルが実際に持っている列」に限定した。**書き込みが1行も起きなかった再開**
（全セッション済み）では列が増えないまま straightline の付け直しに入るため、
定義どおりの並びを機械的に要求すると落ちる。

## 3. 検証

- `./scripts/run-tests.sh`: **946件パス**（変更前916件、30件追加）。既存の
  `tests/test_jtbd_notebook.py::test_notebooks_do_not_ask_for_a_run_id[01_run_jtbd_research.ipynb]`
  1件は**変更前から落ちている**（`git stash` で確認済み。JTBD notebook のウィジェット残り
  で、今回の変更とは無関係）
- `pytest -m spark`: **106件パス**（変更前105件、1件追加。`tests/test_run_spark.py` は22件）
- `ruff check persona_sim app tests`: パス
- fake エンドポイントで端から端まで（`test_reasoning_answers_are_written_to_their_own_column`）:
  100人 × reasoning 設問で `answer_reasoning` が全件埋まり、番号も1件ずつ取れる

追加したテストで押さえた性質。

- スキーマのプロパティ順が `reasoning` → `answer`（後ろに置くと後付けの説明になる）
- reasoning 設問では素の数字を回答にしない（single / multi とも）
- 理由の中の「わかりません」で refusal を立てない／理由の外の拒否は拾う
- 予算が `max_tokens_reasoning` に切り替わり、他の設問の予算は変わらない
- `always` 以外の `structured_output`、`open` への `reasoning`、`reasoning_max_length: 0` が
  それぞれ `validate` で止まる
- 理由を使わない調査では列が空のままで、警告も出ない

## 4. 未対応事項

- **精度の比較検証は未実施。** 実エンドポイントが要る。現行／言語化設問を挟む方式／
  reasoning の3条件を同一 seed・同一パネルで回し、T2B の水準だけでなく**案の順位・
  選択肢分布の形・ペルソナ間の分散・セグメント間の差**を比べる必要がある。
  これが済むまで UI 既定は OFF のまま
- **画面の見積もり式は据え置き。** reasoning を既定 ON にするなら
  `ui.estimation_benchmarks` の作り直しが同時に要る
- `model.thinking` がエンドポイントに送られていない件（`docs/issues/202607281100.md`）は
  据え置き。今回の経路とは別問題
- CI は未確認

---

## 5. 追記: 字数と出力予算の整合検査（レビュー指摘）

「`reasoning_max_length` と `max_tokens_reasoning` は二重管理ではないか」という指摘への対応。

**二重管理ではない。** 単位も役割も違う。

| | `questions[].reasoning_max_length` / `.max_length` | `model.max_tokens_reasoning` / `.max_tokens_open` |
|---|---|---|
| 単位 | 文字 | トークン |
| 相手 | モデルへの指示文に入る目安 | エンドポイント側の絶対の打ち切り |
| 強さ | 守られないことがある | ここで切れる |

片方から他方を自動計算しないのは、文字とトークンの比がモデルとトークナイザで変わるため。
係数を実装に埋めると、「設定したつもり」の乖離が設定ファイルから実装へ移り、
設定を読んでも気づけなくなる。

**ただし指摘の芯は当たっていた**——両者の整合を**誰も検査していなかった**
（今回入れた reasoning だけでなく、既存の `max_length` / `max_tokens_open` も同様）。
`reasoning_max_length: 400` と `max_tokens_reasoning: 128` を並べて書いても通り、
実行して初めて予算の引き上げ再送（`llm/budget.py`）や `output_limit` フラグで分かる。

- `panel/validate.py` に `_check_output_budgets()` を追加。**reasoning と open の両方**を見る。
  目安は `文字数 × TOKENS_PER_JAPANESE_CHAR(1.5) + JSON_ENVELOPE_TOKENS(32)`
- **警告（`W_OUTPUT_BUDGET`）であってエラーにしない。** 文字→トークンの換算は推定でしかなく、
  推定値で調査を止めると実際には通る設定まで拒むことになる。Web UI にもそのまま出る
  （`app/views/survey_design.py` が warnings を表示する）
- 違いを設定ファイルとガイドに明記した: `app/config/ui_config.yaml` の `max_tokens_*`、
  `examples/survey_sample.yaml`、`docs/GUIDE_SURVEY_DEFINITION.md` §4（表を追加）、
  `SPEC_PHASE1.md` §6.3、`ModelConfig` の docstring

検証: 非 Spark **951件パス**（この追記で5件追加）・`ruff check` パス。Spark テストは
この追加が `validate` の警告1本だけで実行経路に触れないため再実行していない
（追記前の全件 106 パスを確認済み）。
