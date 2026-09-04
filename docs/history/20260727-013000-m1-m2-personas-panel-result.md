# 実施結果: M1 `personas_base` / M2 ジョブ定義・validate・panel

- **日時**: 2026/07/27 02:00
- **計画**: [20260727-013000-m1-m2-personas-panel-plan.md](20260727-013000-m1-m2-personas-panel-plan.md)

## 変更内容

### 1. 仕様の改訂（`SPEC_PHASE1.md` 0.6 → 0.7）

提示設計を `design.type` から**3軸**へ置き換えた。

| 軸 | フィールド | 値（**太字**が既定） |
|---|---|---|
| 誰が何を見るか | `design.sample_overlap` | `disjoint` / `allow_overlap` / **`same`** |
| どう見せるか | `design.presentation` | **`sequential`** / `simultaneous` |
| 覚えているか | `design.memory` | **`none`** / `within_stimulus` / `full_session` |

- §5 を全面改稿。§5.0（同一ペルソナは同一コンセプトに1回しか回答しない）を新設し、
  §5.2 で第1軸を m の連続体として定義、§5.3 で反実仮想モナディックを説明、
  §5.4 に旧呼称との対応表を置いた
- §3 のジョブ定義スキーマを3軸に差し替え（`type` と `balance_within_cell` を削除）
- §4.1 を決定論的ハッシュ順の抽出として具体化し、セル間の逐次除外を常時適用と明記
- §6.2 のプロンプト順序を `design.memory` 基準の記述に変更
- §9 の `run_metadata.json` 例、§10.1 の見積もり出力、§11 の E6、§12 の M3 完了条件、§13 の不変条件を更新
- **§2.2 / §2.3 / §6.4 は変更なし。** 同一ペルソナ×同一コンセプトが起きないため、
  既存の複合キーと冪等 upsert がそのまま成立する

`AGENTS.md` の不変条件に「同一ペルソナは同一コンセプトに1回しか回答しない」を追加し、
「monadic ではセル内均等割り当てを省略しない」を `sample_overlap: disjoint` の条件に読み替えた。

### 2. 実装（新規パッケージ `persona_sim`）

| モジュール | 内容 |
|---|---|
| `cli` | `validate` / `panel` / `personas build`。`run` / `aggregate` / `export` は未実装を明示して終了 |
| `config` / `spark` / `storage` | 置き場所の解決（環境変数）、SparkSession、Delta の読み書きと `TableLocator` |
| `personas/source` | HF からリビジョン固定でシャード取得 |
| `personas/normalize` | occupation 分解・年代バンドの**参照実装**（純 Python） |
| `personas/build` | 同じ語彙定数から組み立てた Spark 式と `personas_base` 構築 |
| `panel/schema` `panel/loader` | 3軸を含むジョブ定義。未知キー・廃止フィールド・非対応設問タイプで停止 |
| `panel/validate` | E1 / E6・不変条件・警告・セッション数見積もり。エラーは全件集めて返す |
| `panel/quotas` | 最大剰余法の整数配分、セル別ウェイト、属性条件 → Spark 述語 |
| `panel/sampling` | 決定論的ハッシュ順の選抜、セル間の逐次除外 |
| `panel/assignment` | K 通りの割り当て表（純関数）＋ Spark 側は結合するだけ |
| `panel/build` | `panels` への書き出しと構成サマリ |

`pyproject.toml` に `[project]` と CLI エントリポイント、`requirements.txt`（pyspark 4.0.1 /
delta-spark 4.3.1）を追加。CI は Spark 不要のジョブ（`-m "not spark"`）と
JDK 17 を入れる `spark-test` ジョブに分けた。

### 3. データ仕様の調査結果

`docs/schema/occupation-parsing.md` に `occupation` の分解規則を記録した。
実データ 125,000 行で「末尾から就業状態 → 役職 → 規模を剥がすと残りは必ず1トークン」が
成り立つことを確認している。

## 検証結果

### 単体テスト（Spark 不要）— 99 件すべて成功

3軸の整合（E6）、不変条件（`thinking` / 順序尺度のシャッフル）、割り当ての均等性と重複なし、
最大剰余法、occupation 分解、年代バンド、ローダの拒否条件、見積もり。

### Spark 結合テスト（`-m spark`）— 14 件すべて成功

- **seed 再現性**: 同一 seed で2回実行して選抜と割り当てが完全一致
- **並列度非依存**: パーティション数（1 と 7）と `spark.sql.shuffle.partitions` を変えても結果が一致。
  乱数サンプリングを使っていないことの証明
- セル条件が重なるジョブでも uuid が一意（30 行 / 30 ユニーク）
- `disjoint` でコンセプトごとの評価者数が均等（セル内で差 0）
- `allow_overlap` の割り当てに重複なし
- `rotation: random` でも seed が同じなら同じ順序
- E1 が `panel` 実行前に停止
- `panels` の列が §2.2 の順序どおり、作り直しても行が二重にならない
- 参照実装（純 Python）と Spark 式が代表値・全年齢で一致

### 実データ検証（1シャード / 125,000 行）

- **occupation のユニーク値 1,333 件すべてで、参照実装と Spark 式が一致（不一致 0 件）**
- `personas_base` を 26 列で構築。業種が空の行は 0 件
- `examples/job_sample.yaml` の `validate` がエラー 0・警告 0 で通過
- `panel` を2回実行して結果が完全一致し、`panels` の行数も二重にならない

## 未対応事項

- M3（回答生成）以降は未着手。`design.memory` と `design.presentation` の**実挙動は M3**。
  M2 ではスキーマ定義・検証・パネルへの反映まで
- スクリーニング（`panel.screener`）は M4。指定されたジョブは
  「黙って無視するとパネルの意味が変わる」ため `validate` がエラーで停止させる
- `runs` / `responses` / `aggregates` テーブルの実体は M3 以降
- `select_members` はセルごとに `cache()` を張る。呼び出し側での解放は行っていない
  （1ジョブ1プロセスの想定。常駐化する場合は要見直し）
