# 結果: M5 集計・出力（クロス集計表・コンセプト比較表・xlsx）

- **日時**: 2026/07/27 23:45
- **計画**: [20260727-234500-m5-aggregate-output-plan.md](20260727-234500-m5-aggregate-output-plan.md)
- **完了条件**: `SPEC_PHASE1.md` §7.4 の一式が出る → **達成**

## 1. 変更内容

計画どおり実装した。設計上の逸脱はない。

### 新規パッケージ `persona_sim/aggregate/`

| ファイル | 役割 |
|---|---|
| `segments.py` | 集計軸の解決。`total` / 属性列 / `_x_` の合成軸。未知の軸を検出する |
| `crosstab.py` | §7.1・§7.2 の算出。`Answer` → `Metric` の純関数のみ |
| `tables.py` | 表示用の中間表現 `Table`。整形（`12.3%` / `3.21`）をここに集約 |
| `frame.py` | Spark: `responses` × `panels` × `personas_base` の結合と収集 |
| `aggregate.py` | 集計の実行、`aggregates`（§2.5）の読み書き、表の組み立て |
| `export.py` | §7.4 のファイル一式。CSV は stdlib `csv`、xlsx は openpyxl |

### 既存への変更

- `run/session.py` / `run/run.py` / `panel/screening.py` — `answer_codes` の追加と
  `multi` の判定修正（下記）
- `panel/validate.py` — `output.segments` / `output.formats` の検査を追加
- `run/flags.py` — `QUALITY_FLAGS`（`retried` を含まない）を追加
- `cli.py` — `aggregate` / `export` を実装。スタブを削除
- `SPEC_PHASE1.md` — §2.3 / §2.6 に `answer_codes` を追加し、バージョンを 1.0 に
- `README.md` / `notebooks/quickstart.ipynb` — M5 の内容に更新

### 設計上の要点

- **算術を Spark から切り離した。** `crosstab.py` / `segments.py` / `tables.py` は
  pyspark に依存しない。Spark を使うのは `frame.py` の読み込み・結合だけで、集計の数値は
  Spark 抜きの CI ジョブで手計算と突き合わせられる。フェーズ1の規模
  （1,000人 × 10コンセプト × 数問 ＝ 数万行）ではドライバに集めて差し支えない。
  例外は `responses_raw.csv` で、件数が伸びうるので `toLocalIterator()` で流す。
- **人が読む値と機械可読な値を分けた。** CSV / xlsx は仕様の表記（`12.3%`）に整形する。
  生の比率は `aggregates` テーブルに持たせ、`export` はそこから表を組み直す。
  組み直せること自体が「`aggregates` に必要な情報が揃っている」検査になる。
- **平均は順序尺度のときだけ出す。** 非順序の選択肢に逆順スコアの平均を出しても意味がない。
  `Question.is_ordinal()`（`scale` 型か `top_box` を持つ）で判定し、それ以外は `-`。
- **フラグ除外後の n から `retried` を外した。** リトライは経緯であって、最終的に得られた
  回答の質ではない。除外の対象は `parse_error` / `out_of_range` / `refusal` / `straightline`。
- **`multi` の分母は回答者数。** 比率の合計が100%を超えるので、表に注記を出す。
- **軸の綴り違いで停止させる。** 黙って無視すると、指定した軸が集計表から消えたことに
  実行後まで気づけない。

### `multi` の記録の欠落を直した（M3 の積み残し）

調査中に見つけた欠陥で、利用者の判断により M5 に含めた。

- `responses` / `screener_responses` に `answer_codes array<int>` を追加した。
  `answer_code` は単一回答用の射影として残る（`multi` では null）。
- 「番号が取れなかった」の判定を `parsed.code` から `parsed.codes` に変えた
  （`_answer_missing` に集約）。従来は `multi` が**必ず** `parse_error` になり、
  毎回3回リトライしたうえ、再開のたびに全セッションをやり直していた。
- スクリーナーの通過判定も `answer_codes` を見るようにした。`multi` のスクリーナー設問は
  仕様上サポートされているのに、単一番号しか読めていなかった。

## 2. 検証結果

| 検証 | 結果 |
|---|---|
| `ruff check .` | pass |
| `pytest -m "not spark"` | **236 passed**（M5 分の新規63件を含む） |
| `pytest -m spark` | **51 passed**（M5 分の新規12件を含む。既存39件も維持） |
| ノートブック全46セルの実行（nbclient） | pass。エラー0件 |
| CLI 通し（合成600人 / Fake エンドポイント） | pass |

### 単体テスト（Spark 不要）

- `test_crosstab.py` — 比率・T2B・平均を手計算と突き合わせ。ウェイトが `n` を動かさず `%` と
  平均だけを動かすこと、フラグ行が `n` に残り「フラグ除外後の n」からだけ落ちること、
  `options_order` の読み替え（シャッフル時・範囲外）、multi の分母、numeric の平均、
  非順序設問で平均を出さないこと、セグメント展開
- `test_segments.py` — 軸の分解・合成・未知の軸の停止・欠損値の扱い
- `test_export.py` — 表の見出しと整形、`aggregates` のロング行、CSV の BOM、
  xlsx のシート構成とシート名の31文字制限
- `test_session.py`（追加分）— `multi` が `answer_codes` に入り `parse_error` が立たないこと、
  番号が1つも取れないときは従来どおり立つこと

### Spark 結合テスト

- **§7.4 の7種類のファイルが出ること**（M5 の完了条件）
- `aggregates` の `n` が生データの有効回答数と一致し、比率の合計が 1.0 になること
- T2B が構成選択肢の比率の和と一致すること
- 再集計しても `aggregates` の行が二重にならないこと
- `export` が `responses` を読まずに `aggregates` だけから同じ表を戻せること
- `multi` を含むジョブが run → **再開で全セッションをスキップ** → 集計まで通ること
  （修正前は毎回やり直しになっていた）

### CLI 通し（合成データ）

`validate` → `panel` → `run` → `aggregate` → `export --xlsx` を通した。
60人 × 3コンセプト × 3設問 ＝ 540 セッション。集計結果の妥当性を手計算で確認した。

```
コンセプト   n   n(フラグ除外後)  q_intent T2B  q_intent 平均  q_novelty T2B  q_novelty 平均
コンセプトA  60  60               35.0%         2.80           40.0%          2.97
コンセプトB  60  60               50.0%         3.32           48.3%          3.18
コンセプトC  60  60               46.7%         3.15           36.7%          2.97
```

`crosstab_c1_q_intent.csv` の全体行は 20.0 / 15.0 / 16.7 / 21.7 / 26.7（%）で、
T2B = 35.0%（＝20.0 + 15.0）、平均 = 2.80（＝(5×12 + 4×9 + 3×10 + 2×13 + 1×16) / 60）。
いずれも手計算と一致する。

## 3. 未対応事項

- **既存の `responses` / `screener_responses` は作り直しが要る。** `answer_codes` 列が無い
  テーブルには追記できない。README に明記した。移行スクリプトは用意していない
  （フェーズ1は実運用前なので、`panel` からやり直すほうが確実）。
- **Databricks Model Serving への実通信は引き続き未検証**（この環境から疎通できない）。
- **M7 スループット調整は未着手。** 集計はドライバに集める設計なので、実データ規模での
  実測が要る。`responses_raw.csv` 以外は全件を collect している。
- **ウェイトが 1.0 以外のケースは単体テストのみ。** `quotas.mode: proportion` や
  スクリーニング後の構成崩れを含む結合テストは作っていない。
- スクリーナーの `numeric` / `scale` による通過判定は引き続き未対応（`single` / `multi` のみ）。
- 集計表のセグメント値の並びは文字列順（決定論的にするため）。割り付け定義順ではない。

## 4. 次

M7（並列化・スループット調整）。完了条件は「1,000人 × 10コンセプト × 3問が実用時間で完走」。
併せて、集計をドライバに集める設計がその規模で保つかを実測する。
