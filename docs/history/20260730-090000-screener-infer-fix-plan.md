# `screener.mode: infer` が効かない問題の修正 — 計画

- **日付**: 2026/07/30 09:00
- **起票**: `docs/issues/screeningの問題.md`

## 背景

判定プロンプトを「選択型」（条件に当てはまる人を選ぶ）から「除外型」（条件に明確に
反する人だけを除く）に書き換えても、結果が1件も変わらず通過率0.9%のまま
`ScreenerShortfallError` になる、という報告。

コードを追ったところ、独立した3つの欠陥が重なっていた。

1. **`build_judge_messages()`（`persona_sim/panel/infer.py`）が設定不能な一文を割り込ませていた。**
   user メッセージは常に `…条件 → logic_note → prompt.rule` の順で、`logic_note` は
   `screening.logic` が `all` である限り「すべての条件を満たす人物を選んでください。」に固定。
   YAML から触れず、system prompt より近く `rule` の直前という最も効く位置にある。
   `app/config/ui_config.yaml` は既に除外型の prompt に書き換わっていたが、同ファイルの
   `logic: "all"` のせいで直前に真逆の指示が入っていた。**これが主因。**
2. **判定結果が再利用され、プロンプトを変えても聞き直していなかった。**
   `screener_responses` は `(survey_id, persona_uuid, question_id)` で upsert されるだけで、
   判定設定はキーに入らず、無効化する経路がリポジトリ内に一つも無かった。
3. **到達不能でも倍率を上げ続けていた。** 通過率が構造的に低いとき `oversample_factor` を
   4→8→16→32 と倍にしても通過率は変わらず必要数に届かない。判定の呼び出し費用だけが増える。

## 方針

上記3点を潰す。**「判定材料の無い条件に `infer` を使っている」という方式そのものの妥当性は
スコープ外**（判定カードに飲用頻度の手がかりが無い）。利用側の判断に委ね、issue に論点として残す。

### 1. `logic_note` の廃止と、`infer` での `logic` 禁止

`logic_note` を消すと `screening.logic` は `infer` で完全に無効になる。効かない設定を黙って
残すのは `AGENTS.md` が禁じる「設定したつもりの記録」なので、書けないようにして止める。

- `infer.py`: `build_judge_messages()` から `logic_note` を削除。user メッセージは
  人物一覧 → 対象者条件 → `prompt.rule` だけ
- `schema.py`: `UNUSED_INFER_SCREENER_KEYS`（既存の `MISPLACED_SCREENER_KEYS` に倣う移行ヒント）
- `loader.py`: `mode: infer` かつ `logic` キーがあれば `SurveyDefinitionError`。既定のまま（キー無し）は通す
- `uiconfig/build.py`: `mode != "infer"` のときだけ `screening["logic"]` を書く
- 両 YAML（`app/config/ui_config.yaml` / `examples/survey_sample.yaml`）を追随

### 2. 判定設定のフィンガープリント + `screen --force`

自動無効化（ハッシュ）と手動の逃げ道（`--force`）の両方。

- `screening.py`: 純関数 `screening_fingerprint(survey)`。正規化 JSON の sha256 先頭16桁。
  含めるのは mode / conditions / questions / batch_size / 判定モデル / prompt / persona_card。
  **`oversample_factor` と `concurrency` は含めない**（再試行のたびに全件聞き直しになる）
- `SCREENER_SCHEMA` に `config_hash string` を追加、`to_screener_dataframe()` に引数を足す
- `read_screener_codes(..., config_hash=)`: 指定時はその値の行だけ。列を持たない旧テーブルは
  空を返して全件再判定（自己修復的マイグレーション）。呼び出し3箇所すべてに現在の指紋を渡す
  （`load_premises` を漏らすと古い判定を根拠に前提ブロックを配ってしまう）
- `delta.py`: `merge_upsert(..., evolve_schema=False)`。True で `withSchemaEvolution()`
- `screen_survey(..., force=False)`: 確定済みパネルは、指紋不一致か `--force` のとき
  `build_panel()` で候補に戻してから判定し直す
- `cli.py`: `screen --force`、判定設定の指紋と再判定した旨を出力

### 3. 到達可能性による早期停止

- `screening.py`: 純関数 `unreachable_cells(finalized, requested, factor, attempts_left)`。
  到達可能の条件 `通過率 × 必要数 × 上限倍率 >= 必要数` は**必要数が相殺されて
  `通過率 × 上限倍率 >= 1`** になる。通過0は自動的に到達不能（固定閾値は持たない）
- `screen_survey`: 各試行の `finalized.complete` 判定直後に呼び、該当があれば E2 を即時送出
- `_shortfall_message`: 第3の理由分岐と、セルごとの必要候補数の見積もり

## 検証

- `./scripts/run-tests.sh`（非 Spark）と `-m spark`（pyspark / delta-spark を導入して実行）
- 更新: `test_infer.py` の `test_judge_messages_reflect_logic` を「logic 由来の文言が出ない」
  「user メッセージが `prompt.rule` で終わる」に置き換え
- 新規: `test_survey_loader.py`（infer+logic の拒否 / ask では従来どおり）、
  `test_screening.py`（指紋の感度と `oversample_factor` 非依存 / `unreachable_cells` の境界）、
  `test_screening_spark.py`（指紋変更・`--force`・旧テーブル自己修復・早期停止）、
  `test_uiconfig.py`（infer で `logic` を書かない）
