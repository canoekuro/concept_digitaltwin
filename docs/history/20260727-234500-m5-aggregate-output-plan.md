# 計画: M5 集計・出力（クロス集計表・コンセプト比較表・xlsx）

- **日時**: 2026/07/27 23:45
- **対象**: `SPEC_PHASE1.md` §7 / §2.5 / §12 M5
- **完了条件**: §7.4 の出力一式が出る

## 1. 目的

`responses` から `SPEC_PHASE1.md` §7 のクロス集計表・コンセプト比較表を作り、
`aggregates` テーブル（§2.5）と §7.4 のファイル一式を書き出す。これで
ジョブ定義 → パネル → 回答生成 → 集計表 がフェーズ1として1本つながる。

## 2. 現状分析と前提

- M1〜M4・M6 は実装済み。`persona-sim validate / panel / screen / run` が通る。
  `aggregate` / `export` は「未実装」と表示して終了するスタブのまま。
- `storage/locator.py` の `AGGREGATES` は定義だけあって未使用。
- ノートブックの「11. 簡易集計」は pandas による暫定実装で、M5 で置き換える前提の注記つき。
- **`multi` 設問に記録の欠落がある**（調査で判明）。
  - `responses` は `answer_code`（単一 int）しか持たず、`parse_answer` が取った
    `parsed.codes` が `_to_record` で捨てられている。
  - `multi` は `parsed.code is None` かつ OPEN/NUMERIC 以外なので**必ず `parse_error` が立ち**、
    `_needs_retry` で毎回3回リトライし、`_completed_keys` が完了とみなさないため
    再開のたびにやり直す。
  - この状態では `multi` を集計できない。**M5 の中で直す**（利用者の判断）。

## 3. 提案変更

### 3.1 `multi` の記録を直す（M3 の欠落）

- `responses` / `screener_responses` に **`answer_codes array<int>`** を追加する。
  `answer_code` は単一回答用の射影として残す（`multi` では null）。
- 「番号が取れなかった」の判定を `parsed.code` から `parsed.codes` に変える
  （`_answer_missing` に集約し、リトライ判定とフラグ判定で共有する）。
- スクリーナーの通過判定（`read_screener_codes`）も `answer_codes` を見る。
  `multi` のスクリーナー設問は仕様上サポート済みなのに、単一番号しか読めていなかった。
- `SPEC_PHASE1.md` §2.3 / §2.6 の表に列を足す。仕様とテーブルを食い違わせない。

### 3.2 集計コア（pyspark に依存しない）

新パッケージ `persona_sim/aggregate/`。`panel/schema.py` や `run/parsing.py` と同じ方針で、
**算術は純 Python に閉じ込め、Spark は読み込みと結合だけ**にする。数値そのものを
Spark 抜きの CI ジョブでテストできるようにするため。

| ファイル | 役割 |
|---|---|
| `segments.py` | `output.segments` の解決（`total` / 属性列 / `_x_` の合成軸） |
| `crosstab.py` | §7.1・§7.2 の算出。`Answer` → `Metric` の純関数 |
| `tables.py` | 表示用の中間表現 `Table`（CSV・xlsx に同じ形で流す） |
| `frame.py` | Spark: `responses` × `panels` × `personas_base` を結合して収集 |
| `aggregate.py` | 集計の実行と `aggregates` への書き出し |
| `export.py` | §7.4 のファイル一式（CSV は stdlib、xlsx は openpyxl） |

算出の決めごと:

- 提示順の番号を `options_order` で**定義順に戻してから**集計する（§2.3）
- `n` はウェイト適用前の実数、`%` はウェイト適用後（§7.1）
- 平均は逆順スコア化（`選択肢数 + 1 - 番号`）。**順序尺度のときだけ**出す
- 品質フラグの行は除外せず、`n` と「フラグ除外後の n」を併記する（§8）。
  `retried` は質のフラグではないので除外の対象にしない
- `multi` は分母＝回答者数（比率の合計は100%を超えうる）。平均は出さない
- `numeric` は比率を出さず平均のみ。`open` は §7.3 の長持ちテーブルへ

### 3.3 `aggregates` テーブル（§2.5）

ロング形式（`segment` / `segment_value` / `metric`(option|t2b|mean) / `option_code` / `value`）。
`job_id` 単位で消してから append するので再集計で二重にならない。**表示用に整形する前の生の値**を持つ。

### 3.4 CLI（§10.1）

- `persona-sim aggregate job.yaml` — 集計して `output.formats` に応じて書き出す
- `persona-sim export job.yaml --xlsx` — 集計をやり直さず `aggregates` から表を組み直す

### 3.5 `validate` に足す検査

`output.segments` の未知の軸、`output.formats` の未知の値で停止する。
黙って無視すると、指定した軸が消えたことに実行後まで気づけない。

### 3.6 依存

`report.xlsx` のため **openpyxl** を追加する。pandas は使わない
（CSV は stdlib `csv`、集計は純 Python）。

## 4. 検証計画

| 種別 | 内容 |
|---|---|
| 単体 | 比率・T2B・平均の手計算一致、ウェイトが n と % に与える影響の違い、フラグ行の扱い、`options_order` の読み替え、multi / numeric、セグメント展開 |
| 単体 | 軸の解決と未知の軸の停止、`validate` の新規検査 |
| 単体 | `multi` が `answer_codes` に記録され `parse_error` が立たないこと |
| 単体 | 表の整形（見出し・`%` 表記・`-`）、`aggregates` のロング行、CSV の BOM、xlsx のシート構成 |
| 結合（Spark） | **§7.4 の一式が出ること（完了条件）**、`aggregates` が生データと合うこと、再集計で二重にならないこと、`export` が `aggregates` だけから同じ表を戻せること、multi ジョブが run → 再開 → 集計まで通ること |
| 全体 | `ruff check .` / `pytest -m "not spark"` / `pytest -m spark` / ノートブックの全セル実行 / CLI 通し |

## 5. リスク

| # | リスク | 対応 |
|---|---|---|
| 1 | 既存 `responses` に `answer_codes` が無く追記できない | README と result に再作成の注意を明記 |
| 2 | ウェイトと n の取り違えで % と n が食い違う | 手計算の期待値を単体テストに固定。ウェイト 1.0 以外の例を必ず含める |
| 3 | ドライバへ集める設計が大規模ジョブで詰まる | 集める列を限定し、`responses_raw.csv` は streaming。M7 で実測 |
| 4 | openpyxl が実行環境に無い | 遅延 import と明示的なエラー。CSV だけでも成果物として成立させる |

## 6. 実装の分担

`segments.py` / `tables.py` / `export.py` は仕様が確定しており委譲可能。
`crosstab.py`（ウェイトとフラグ併記）と `multi` の記録修正はメインが直接実装する。
