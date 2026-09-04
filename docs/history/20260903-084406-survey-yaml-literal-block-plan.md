# 投入する調査定義の複数行文字列を `|` で書き出す（計画）

- 日時: 2026-09-03 08:44:06 UTC
- 対象: `persona_sim/uiconfig/jobs.py` / `tests/test_jobs.py`
- 起点: `ui_config.yaml` に `main_survey.prompt.systems.purchase_intent` を書くと、
  投入される調査定義 YAML の表記が崩れて `\n` が並ぶ、という報告。

## 1. 原因

1. `ui_config.yaml` 側で `|` を使って書いても、**読み込んだ時点でただの改行入り
   文字列**になる。「`|` で書いた」という情報は残らない。
2. 書き出しは `jobs.survey_yaml()` の `yaml.safe_dump(allow_unicode=True,
   sort_keys=False)`。**PyYAML は `default_style` 未指定のとき、自分からは
   ブロックスタイル（`|` / `>`）を選ばない。** plain → single-quoted →
   double-quoted の順にしか試さないので、改行入り文字列は必ずクォートされる。
3. `purchase_intent` は箇条書きが `  - ` と字下げされている。PyYAML の
   `analyze_scalar()` は「改行の直後が空白で始まる」文字列に対して
   `allow_block_plain` と `allow_single_quoted` を両方 false にするため、
   **残る選択肢が double-quoted だけになる**。結果、`\n` エスケープと行継続の
   `\` が混じった1本の長いスカラーになる。

実際の出力（再現済み）:

```yaml
  purchase_intent: "あなたは、…します。\n回答時のルール:\n  - プロフィールに…ください。\n\
    \  - コンセプトの広告表現を…ください。\n  - 調査に好意的に…ありません。\n\
```

字下げを外すと今度は single-quoted になり、改行が空行に化けて別の形で読めなくなる。

## 2. 影響範囲

**データは壊れていない。** `yaml.safe_load()` で読み直すと元の文字列と完全に一致する
（確認済み）ので、ジョブが受け取るプロンプトは正しい。崩れているのは人が読むときの
見た目だけ。

ただし `jobs.py` の docstring が明言しているとおり、この YAML は「ジョブが読むだけで
なく、あとから人が『何を聞いたのか』を確認する対象」なので、その目的に対しては実害が
ある。同じ経路を通る**コンセプト文（`stimuli[].text`）**と `screening.prompt.system`
も同様に崩れる。

`runs.metadata_json` の `survey_definition`（`persona_sim/metadata.py`）は JSON なので
対象外。あちらで `\n` が見えるのは仕様どおり。

## 3. 方針

`survey_yaml()` に、**改行を含む文字列だけ `|` で出す representer** を足す。

- `yaml.SafeDumper` のサブクラス（`_SurveyDumper`）に `str` の representer を登録し、
  `"\n" in data` のときだけ `style="|"` を指定する。
- `|` にできない文字列（行末に空白がある、`\r` を含む）は PyYAML が自動的に
  double-quoted へ戻すので、強制指定でも壊れない。末尾に改行が無い文字列は `|-`。
- あわせて `width` を広げる。既定の80桁だと、1行に収まる長文（コンセプト文・設問文）が
  途中で折り返され、複数行を `|` にした意味が薄れる。

`ui_config.yaml` 側は触らない。原因は書き出し側にあり、設定の書き方を変えても
（字下げを外しても）別の崩れ方をするだけなので。

## 4. 検証

- `tests/test_jobs.py` に回帰テストを足す。
  - 複数行プロンプトが `|` で出て `\n` が本文に現れないこと
  - 見た目を変えても往復（`safe_load`）で元の文字列に戻ること
  - `|` にできない文字列（行末の空白・`\r`）でも往復すること
  - 1行に収まる長文が折り返されないこと
- 既存の3件（日本語がエスケープされない・往復・キー順の保持）が通り続けること
- `bash scripts/run-tests.sh` の非 Spark 全件と `ruff check`
- 実際の `app/config/ui_config.yaml` のプロンプトを流して目視確認する

## 5. やらないこと

- 所要時間見積もりの定数項（別途相談中の課題）はこの変更に含めない。
- `metadata_json` の表記には触らない（JSON であり、崩れていない）。
