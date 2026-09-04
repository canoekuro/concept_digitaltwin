# `screener.mode: infer` が効かない問題の修正 — 結果

- **日付**: 2026/07/30 09:00
- **計画**: [20260730-090000-screener-infer-fix-plan.md](20260730-090000-screener-infer-fix-plan.md)
- **起票**: `docs/issues/screeningの問題.md`

## 破壊的変更

**`screening.mode: infer` では `logic:` を書けなくなった。** 書いてある調査定義は読み込み時に
移行先を添えて停止する。判定の指示は `screening.prompt.rule` に一本化されたので、条件を
すべて満たす必要があるのかいずれかで足りるのかは、その文中に書く。
`app/config/ui_config.yaml` の `logic:` は残してよい（`persona_sim/uiconfig/build.py` が
`infer` のときは調査定義に書き出さない）。`mode: ask` の `logic:` は従来どおり有効。

既存 Delta テーブルの作り直しは**不要**。`screener_responses` に列が増えるが、列を持たない
既存テーブルは全件未判定として扱われ、次の `screen` で自己修復する。

## 変更内容

### 1. `logic_note` の廃止（主因）

`persona_sim/panel/infer.py` の `build_judge_messages()` が組み立てる user メッセージは、
これまで常にこの順だった。

```
■人物一覧 … / ■対象者条件 …
すべての条件を満たす人物を選んでください。   ← logic: "all" から生成。YAML から触れない
{screening.prompt.rule}                      ← ここだけ設定可能
```

割り込む一文は system prompt より近く `rule` の直前という最も効く位置にあり、
`prompt` を除外型に書き換えても真逆の指示に引き戻していた。`app/config/ui_config.yaml` は
既に除外型の prompt に書き換わっていたが、同ファイルの `logic: "all"` が効いていたため
症状が続いていた。

- `infer.py`: `logic_note` を削除。user メッセージは人物一覧 → 対象者条件 → `prompt.rule` だけ。
  「ここで文を足してはいけない」理由を docstring に残した
- `schema.py`: `UNUSED_INFER_SCREENER_KEYS`（移行ヒント。既存の `MISPLACED_SCREENER_KEYS` に倣う）
- `loader.py`: `mode: infer` かつ `logic` キーがあれば `SurveyDefinitionError`。
  **値ではなくキーの有無で見る**ので、既定のまま（書かない）は通る
- `uiconfig/build.py`: `mode != "infer"` のときだけ `screening["logic"]` を書き出す
- `app/config/ui_config.yaml` / `examples/survey_sample.yaml` を追随

### 2. 判定設定のフィンガープリント + `screen --force`

`screener_responses` は `(survey_id, persona_uuid, question_id)` で upsert されるだけで
判定設定をキーに持たず、無効化する経路がリポジトリ内に一つも無かった。同じ `survey_id` で
回し直すと、プロンプトやモデルを変えても古い判定がそのまま再利用されていた。

- `screening.py`: `screening_fingerprint(survey)` を新設。mode / conditions / questions /
  batch_size / 判定モデル / prompt / persona_card を正規化 JSON にして sha256 の先頭16桁。
  **`oversample_factor` と `concurrency` は含めない** — 前者は再試行で倍になるが判定の
  中身を変えないので、含めると再試行のたびに全件を聞き直すことになる
- `SCREENER_SCHEMA` に `config_hash string` を追加し、`to_screener_dataframe()` が全行に埋める
- `read_screener_codes(..., config_hash=)`: 指定時はその指紋の行だけを返す。
  列を持たない古いテーブルは空を返して全件再判定（自己修復的マイグレーション）。
  呼び出し3箇所すべてに現在の指紋を渡した（`load_premises` を漏らすと、古い判定を
  根拠に前提ブロックを配ってしまう）
- `storage/delta.py`: `merge_upsert(..., evolve_schema=False)`。True のとき
  `DeltaMergeBuilder.withSchemaEvolution()` を挟む。既定を False にしたのは、
  typo で増えた列が黙って本番テーブルに入るのを防ぐため
- `screen_survey(..., force=False)`: 確定済みパネルは、指紋不一致か `--force` のときだけ
  `build_panel()` で候補に戻してから判定し直す。**`force` が効くのは初回の試行だけ** —
  毎回効かせると、同じ実行の中で既に判定した候補まで再試行のたびに聞き直してしまう
- `cli.py`: `persona-sim screen --force`。判定設定の指紋と、再判定した旨を出力に足した
- `ScreeningResult` に `config_hash` / `refreshed` を追加

### 3. 到達可能性による再試行の早期停止

通過率が構造的に低いとき、`oversample_factor` を 4→8→16→32 と倍にしても通過率は変わらず
必要数には届かない。届かない再試行に判定の呼び出し費用だけがかかっていた。

- `screening.py`: 純関数 `unreachable_cells(finalized, requested, factor, attempts_left)`。
  到達可能の条件 `通過率 × 必要数 × 上限倍率 >= 必要数` は、**両辺の必要数が相殺されて
  `通過率 × 上限倍率 >= 1`** に落ちる（セルの大小によらず通過率と倍率だけで決まる）。
  通過率0のセルは自動的に到達不能になるので、固定閾値を持たなくてよい。
  判定していないセル（`judged == 0`）は通過率が出せないので対象外にする（0除算も避ける）
- `screen_survey`: 各試行の `finalized.complete` 判定直後に呼び、該当があれば §11 E2 を即時送出
- `_shortfall_message`: 第3の理由分岐と、セルごとの必要候補数の見積もりを追加

報告された事例では、4回の試行と32倍までのオーバーサンプルを費やす代わりに、初回で
こう止まる。

```
スクリーニング通過者が必要数に満たない（実インシデンスが低く、oversample_factor を
上限の 32 倍まで上げても必要数に届かないため、1 回で打ち切った）:
  M_20_29: 不足 7 名（通過 3 / 判定 320）
    必要な候補数の見積もり 約1067名（現在の通過率 0.9%）に対し、上限の 32 倍でも 320 名までしか増やせない
実インシデンス: M_20_29=0.9% / total=0.9%
```

## スコープ外とした論点

**通過率0.9%そのものは、実装の不具合ではなく方式の限界の可能性が高い。** 判定カード
（`screening.persona_card`）に載るのは属性行・総括文・`hobbies_and_interests` で、
「缶チューハイ・缶ハイボールを月1回以上飲む」を判断する材料は基本的に書かれていない。
書かれていないものを「蓋然性が高い」で厳格に選ばせれば、酒に言及したごく一部しか残らない。

上記1でプロンプトが効くようになるので、まず除外型の `prompt.rule` で通過率が妥当な水準に
上がるかを確認する必要がある。上がらない場合、飲用頻度のような条件は `infer` ではなく
`ask`（本人に聞く／インシデンスを実測できる）か `assume`（前提として与える）が筋。
この判断は利用側に委ね、`docs/issues/screeningの問題.md` に論点として残した。

## 仕様書の更新

- §2.6: `screener_responses` のカラム表に `config_hash` を追加し、何に使う列かを明記
- §4.2: `ask` の手順に早期停止と指紋による再判定を追記。`infer` の手順に
  「判定の指示は `prompt.rule` だけが出す」「仕組み側で文を足してはいけない」
  「そのため `infer` では `logic` を書けない」を追記
- §11 E2: 上限まで試さずに停止する条件を追記

## 検証

- 非 Spark（`./scripts/run-tests.sh`）: 452件全件パス（新規23件）
- Spark（`-m spark`、pyspark 4.0.1・delta-spark 4.3.1 を導入して実行）: 70件全件パス（新規7件）
- Databricks 実環境での確認は未実施

### 追加・更新したテスト

- `tests/test_infer.py`: `test_judge_messages_reflect_logic` を廃止し、
  「`logic` 由来の文言がプロンプトに現れない」「user メッセージが `prompt.rule` で終わる」に置き換え
- `tests/test_survey_loader.py`: `infer` + `logic` の拒否、`logic` 省略時は通ること、
  `ask` では従来どおり効くこと
- `tests/test_screening.py`: 指紋が prompt / model / persona_card / batch_size / conditions で
  変わり、**`oversample_factor` では変わらない**こと。`unreachable_cells` の境界
  （報告事例の 0.9%、通過0、到達可能な50%、残り試行回数の影響、判定0のセル）
- `tests/test_screening_spark.py`: プロンプト変更・判定モデル変更で再判定されること、
  `oversample_factor` だけの変更では再利用されること、`--force` で再判定されること、
  行に `config_hash` が入ること、列を持たない旧テーブルから自己修復すること、
  通過率0で上限まで試さずに打ち切ること
- `tests/test_uiconfig.py`: `infer` では `logic` を書き出さず、`ask` / `assume` では書くこと

### ついでに直したベースブランチの既存障害

`tests/test_uiconfig.py::test_built_survey_uses_the_screening_and_main_survey_blocks` は
**本変更の前から失敗していた**。`app/config/ui_config.yaml` の `batch_size: 30` に対し、
テストが期待値 20 を直書きしていたため。設定ファイル側を正とする方針を確認し、
`ui.screening.batch_size` との一致を見る形に改めた。この test の趣旨は「設定ファイルの
値がそのまま調査定義に渡ること」なので、数値を直書きすると `ui_config.yaml` を変える
たびに本質と関係のない失敗になる。
