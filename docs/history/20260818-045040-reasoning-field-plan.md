# 選択式設問に reasoning フィールドを足せるようにする（計画）

- 日時: 2026-08-18 04:50:40 UTC
- 対象: `persona_sim/panel/{schema,loader,validate}.py` /
  `persona_sim/run/{parsing,session,run}.py` / `persona_sim/llm/fake.py` /
  `persona_sim/storage/warehouse.py` / `persona_sim/metadata.py` /
  `app/config/ui_config.yaml` / `SPEC_PHASE1.md` §2.3・§3・§6.3・§13 /
  `docs/GUIDE_SURVEY_DEFINITION.md` / `tests/`
- 起点: 「コンセプト調査で言語化させてから新規性を答えさせたい」という相談。
  記憶（`remember`）で言語化設問を挟む方式は既存機能で組めるが、1設問ぶん
  API 呼び出しが増える。**同じ呼び出しの中で理由を書かせる**経路を用意する。

## 1. 現状と問題

選択式設問は番号だけを返させる（`prompt.rules.single` =「番号のみで答えてください。」、
`model.max_tokens` = 8〜100、構造化出力は `{"answer": integer}`）。理由を書かせる手段は
2つあるが、どちらも使えない。

- **思考モード（thinking）** は `AGENTS.md`／`SPEC_PHASE1.md` §13 の不変条件で常に OFF。
  加えて `docs/issues/202607281100.md` のとおり、そもそもエンドポイントへ送られていない。
  仮に効かせられても**思考内容が記録に残らない**ので、§9.1 の再現性の考え方と噛み合わない。
- **自由回答設問を前に置く**（`remember` で繋ぐ）方式は今日でも書けるが、設問が1つ増え、
  記憶で繋がるぶん並列度が落ちる。

つまり「1回の呼び出しで、理由を書いてから番号を選ばせ、その理由を残す」経路が無い。

## 2. 方針（利用者と確認済み）

構造化出力のスキーマに `reasoning` を足し、**プロパティ順で `reasoning` を先に置く**。
生成順がそのまま「理由 → 番号」になる。思考モードと違い、内容は `responses` に列として残る。

調査定義に足すキーは3箇所。

```yaml
questions:
  - id: "q_novelty_1"
    type: "single"
    reasoning: true              # 既定 false
    reasoning_max_length: 80     # 既定 80。指示文の {max_length} に入る

main_survey:
  model:
    max_tokens_reasoning: 512    # reasoning 有効な選択式設問の予算（max_tokens_open と同じ立て付け）
    structured_output: "always"  # reasoning 有効時は必須（下記）
  prompt:
    rules:
      reasoning: "まず、そう考えた理由を{max_length}文字以内で書き、そのうえで当てはまる番号を1つ選んでください。"
```

確認して決めた点が3つある。

1. **`structured_output: always` を必須にする。** `auto` のフォールバック（正規表現パース）は
   最初に現れた数字を採るので、**理由文の中の数字を回答として拾う**。黙って劣化する経路を
   残さないため、`reasoning: true` の設問がある調査定義は `always` 以外を `validate` で止める。
2. **refusal 判定から reasoning 本文を外す。** 理由の散文はこちらが書かせたものなので、
   「わかりません」等が拒否の目印にならない（購入意向の既定選択肢に「わからない」がある以上、
   誤検出は高頻度で起きる）。`refusal` は `QUALITY_FLAGS` に含まれ「フラグ除外後の n」を
   減らすため、放置すると集計の n が実態より小さくなる。真の拒否は番号が取れず
   `parse_error` で表面化する。
3. **Web UI の既定設問は `reasoning: false` のまま。** 文面（`prompt.rules.reasoning`）と
   予算（`max_tokens_reasoning`）だけ `ui_config.yaml` に用意し、有効化は調査定義側で選ぶ。
   費用と所要時間が数倍になり、画面の見積もり式（`estimation_benchmarks.survey_answer.
   output_tokens_per_answer: 15`）が合わなくなるため、既定 ON は実測の後で判断する。

指示行は**設問タイプの指示行を置き換える**（併記しない）。「番号のみで答えてください。」と
「理由を書いてから番号を選んでください。」が同居すると矛盾する。

JSON Schema に `maxLength` は入れない。strict スキーマでの対応がエンドポイント依存で、
拒否されると `always` の下では調査そのものが停止する。字数は指示文で担保する。

## 3. 変更内容

| ファイル | 変更 |
|---|---|
| `panel/schema.py` | `Question.reasoning` / `.reasoning_max_length`、`ModelConfig.max_tokens_reasoning`、`PromptRules.reasoning`（`for_type()` からは引けない位置づけを docstring に明記） |
| `panel/loader.py` | `_question()` のキー許可に2つ追加、`_prompt_rules()` の `{max_length}` 許可を `open` と `reasoning` に |
| `run/parsing.py` | `answer_schema()` に `reasoning` を先頭プロパティで追加、`ParsedAnswer.reasoning`、reasoning 設問は JSON の `answer` のみ採用（数字の正規表現フォールバックを使わない）、refusal 判定は reasoning 本文を除いた文字列に対して行う |
| `run/prompt.py` | `question_block()` が reasoning 有効時に `rules.reasoning` を使う |
| `run/session.py` | `_ask()` の予算選択に `max_tokens_reasoning`、`ResponseRecord.answer_reasoning`、`_to_record()` |
| `run/run.py` | `RESPONSES_SCHEMA` に `answer_reasoning string`、`_to_dataframe()`、responses の `merge_upsert(..., evolve_schema=True)` |
| `panel/validate.py` | reasoning + `structured_output != always` はエラー、`open` / `numeric` に reasoning はエラー、reasoning 使用時は「ばらつきが縮みうる」旨の警告を1本 |
| `llm/fake.py` | スキーマに `reasoning` があれば `{"reasoning": …, "answer": N}` を返す |
| `storage/warehouse.py` | `RAW_RESPONSE_COLUMNS` に `answer_reasoning` |
| `metadata.py` | `max_tokens_reasoning` と reasoning 設問数を実行メタデータへ |
| `app/config/ui_config.yaml` | `prompt.rules.reasoning` の文面と `model.max_tokens_reasoning`。設問側は `reasoning` を書かない |
| `SPEC_PHASE1.md` | §2.3 列追加 / §3 フィールド / §6.3 パース規則 / §13 に「reasoning は思考モードとは別」の1項 |
| `docs/GUIDE_SURVEY_DEFINITION.md` | 書き方・注意（費用・refusal・比較実験の勧め） |

### 既存テーブルへの影響

`responses` に列が1本増える。`_apply_straightline()` が `RESPONSES_SCHEMA_COLUMNS` で
select するため、列の無い既存テーブルではそのままだと落ちる。responses の
`merge_upsert()` に `evolve_schema=True` を付けて吸収する（前例: `panel/screening.py`）。

## 4. 検証

- `./scripts/run-tests.sh`
- 追加・更新するテスト: `tests/test_parsing.py`（スキーマ・パース・refusal 除外）、
  `tests/test_prompt.py`（指示行の置き換え）、`tests/test_session.py`（予算選択と記録）、
  `tests/test_survey_loader.py`（新キー）、`tests/test_validate.py`（2つのエラー）、
  `tests/test_run_spark.py`（列の書き出し）、`tests/test_warehouse.py`（ローデータ列）
- `endpoint: fake` の smoke 定義に reasoning を足した派生で1本通し、`answer_reasoning` が
  埋まること・`responses_raw` に出ることを確認する

## 5. この計画で扱わないこと

- **精度の比較検証**（現行／言語化設問／reasoning の3条件）。実エンドポイントでの実査が要る。
  判断材料は T2B の水準だけでなく、ペルソナ間の分散・選択肢分布・セグメント間差・案の順位。
- Web UI の見積もり式（`estimation_benchmarks`）の作り直し。既定 OFF のため現行値のまま正しい。
- 思考モード（`model.thinking`）の扱い。`docs/issues/202607281100.md` は据え置き。
