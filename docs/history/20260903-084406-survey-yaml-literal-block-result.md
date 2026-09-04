# 投入する調査定義の複数行文字列を `|` で書き出す（結果）

- 日時: 2026-09-03 08:44:06 UTC
- 計画: [20260903-084406-survey-yaml-literal-block-plan.md](20260903-084406-survey-yaml-literal-block-plan.md)

## 1. 変更点

### `persona_sim/uiconfig/jobs.py`

`yaml.safe_dump()` を、`str` の representer を差し替えた `_SurveyDumper` による
`yaml.dump()` に置き換えた。

- `_literal_str()` … 改行を含む文字列にだけ `style="|"` を指定する。
  PyYAML は `default_style` 未指定のときブロックスタイルを候補にしないので、
  明示しないと複数行のプロンプトが `\n` エスケープの1本のスカラーに潰れる。
- `_NO_WRAP_WIDTH = 4096` … 既定の80桁での折り返しを止める。1行に収まる
  コンセプト文・設問文が途中で割れると、複数行を `|` にした意味が薄れるため。

`allow_unicode=True` と `sort_keys=False` は従来どおり。**なぜ PyYAML が
double-quoted に落ちるのか**（字下げされた行があると plain も single-quoted も
使えなくなる）を `_SurveyDumper` の docstring に残した。設定側の書き方の問題と
誤読して `ui_config.yaml` の字下げを外しに行くと、改行が空行に化けるだけで
解決しないため。

### `tests/test_jobs.py`

`ui_config.yaml` の `systems` と同じ形（字下げされた箇条書きを含む）の
`MULTILINE_PROMPT` を足し、回帰テストを4件追加した。

| テスト | 何を守るか |
|---|---|
| `test_multiline_prompts_are_written_as_literal_blocks` | `system: \|` で出て、本文に `\n` が現れない |
| `test_multiline_prompts_round_trip` | 見た目を変えてもジョブが受け取る文字列は元のまま |
| `test_a_string_that_cannot_be_a_literal_block_still_round_trips` | 行末の空白・`\r` を含む文字列でも壊れない（PyYAML が引用形式へ戻す） |
| `test_long_single_line_text_is_not_wrapped` | 1行に収まる長文が折り返されない |

## 2. 結果

投入される YAML の該当箇所:

```yaml
main_survey:
  prompt:
    systems:
      purchase_intent: |
        あなたは、提示された人物の立場で、実際の消費行動を想像しながら調査に回答します。
        回答時のルール:
          - プロフィールに明示されていないブランド嗜好、購入経験、所得などを事実として勝手に補完しないでください。
          - コンセプトの広告表現をそのまま事実として受け入れず、商品内容をもとに判断してください。
```

`ui_config.yaml` に書いた見た目がそのまま残る。コンセプト文（`stimuli[].text`）と
`screening.prompt.system` も同じ経路なので併せて直った。1行に収まる長い
`screening.prompt.system` は plain のまま、折り返されずに出る。

## 3. 検証

- `bash scripts/run-tests.sh tests/test_jobs.py` … 21件全件パス（変更前17件、4件追加）
- `bash scripts/run-tests.sh` … 973件パス / 3件失敗。失敗3件
  （`test_jtbd_definition.py::test_load_study_from_sample_yaml`、
  `test_jtbd_notebook.py::test_notebooks_do_not_ask_for_a_run_id[01_run_jtbd_research.ipynb]`、
  `test_run_survey_notebook.py::test_token_usage_is_reported`）は
  **この変更の前から同じく失敗している**（`git stash` した状態で再実行して確認済み）。
  本件とは無関係の既存の失敗で、別途対応が要る。
- `ruff check persona_sim/uiconfig/jobs.py tests/test_jobs.py` … パス
- 実際の `app/config/ui_config.yaml` から `screening.prompt` / `main_survey.prompt` /
  複数行の `stimuli[].text` を含む定義を組み立てて `survey_yaml()` に通し、出力の
  目視と往復一致を確認済み。

**Databricks への実投入は未実施**（この環境に接続が無いため）。確認は
`survey_yaml()` の直接呼び出しによる。

## 4. 積み残し

- UI の「想定所要時間」が小さい N で常に 0 と出る件は別課題として残っている。
  比例式に固定オーバーヘッドの項が無いこと（`persona_sim/uiconfig/cost.py`）と、
  表示が分単位0桁であること（`app/views/survey_design.py`）の2点。実績値の
  取り直しが要るので本件には含めていない。
