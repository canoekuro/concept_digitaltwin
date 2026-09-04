# パネル抽出の再計算爆発を解消する（結果）

- **日付**: 2026/08/06 03:07
- **計画**: [20260806-030737-panel-selection-performance-plan.md](20260806-030737-panel-selection-performance-plan.md)

## 変更内容

### `persona_sim/panel/quotas.py`

`filter_condition` の直上に `SELECTION_COLUMNS`（`uuid`, `sex`, `age`, `prefecture`,
`region`, `area`, `marital_status`, `education_level`）を追加。
`filter_condition` に列を足したらここにも足すこと、という注意書きと、
取りこぼしを検出するテスト名をコメントに残した。

### `persona_sim/panel/sampling.py`

`select_members` を書き換えた。**セル内の抽出ロジック（`_rank_key` の `sha2`、
`orderBy(...).limit(needed)`、`row_number().over(Window.orderBy(...)) - 1`）は変えていない。**

- 母集団を `SELECTION_COLUMNS` に射影して1回だけ `cache()` し、候補数カウント・重なり検知・
  全セルのフィルタで共有する。`try` / `finally` で必ず `unpersist()` する。
- `picked.count()` → `picked.collect()`。確定行を driver 側のリストに積み、`achieved` は
  `len()` から取る（Spark の action 数はセルごとに1回のままで変わらない）。
- anti-join の除外集合は、前セルの DataFrame ではなく確定済み uuid のリストから
  `spark.createDataFrame` で作り直して `F.broadcast` で結合する。
- `parts` の union 連鎖を廃止し、`spark.createDataFrame(rows, MEMBER_SCHEMA)` で1回だけ
  `members` を組む（`finalize_panel` と同じパターン）。
- 「各セルの結果は cache 済みなので再計算は起きない」という**事実と食い違っていたコメント**を、
  「前セルの DataFrame を繋ぐと計算木が入れ子で積み上がる」という実態の説明に差し替えた。
  モジュール冒頭の docstring にも抽出コストの前提を明記した。

### `tests/test_panel_spark.py`

- `test_selection_cost_grows_linearly_with_cells`: `setJobGroup` ＋ `statusTracker` で
  ステージ数を数え、2セルと6セルを比較する。
- `test_selection_columns_cover_every_filter_field`: `PersonaFilter` の全フィールドを使う
  定義で抽出を通し、`SELECTION_COLUMNS` の取りこぼしを検出する。

## 検証結果

### 出力の同一性（最重要）

重なるセルを含む10セル（男女 × 10歳刻み4バンド ＋ 範囲の広い2セル）・同一 seed で
修正前後の `select_members` を実行し、JSON に落として突き合わせた。

- `(persona_uuid, cell_id, cell_rank, role)` 78行が**完全一致**
- `achieved` / `raw_candidates` / `overlap_excluded`（250件）/ `weights` も完全一致

重複除外の経路まで通っており、選ばれる人も順位も変わっていない。

### コスト

セル数を変えて `select_members` のステージ数を実測した（pyspark 4.0.1 / local）。

| セル数 | 修正前 | 修正後 |
|---:|---:|---:|
| 2 | 12 | 12 |
| 4 | 32 | 18 |
| 6 | 106 | 24 |
| 8 | 396 | 30 |

修正後は `3 × セル数 + 6` の**きれいな線形**。修正前は二次ですらなく、
セルを2つ増やすごとにおよそ3.5倍に伸びる**指数的**な増え方だった。
8セルで 396 → 30 ステージ（約13分の1）。実運用の10セルではさらに差が開く。

Spark UI に出ていた三角数状のタスク数は、単一ジョブ内では同一の Exchange が再利用されて
二次に抑えられていた分で、セルごとの action をまたぐと再利用が効かず指数的に伸びていた。

### テスト

- `uv run ruff check persona_sim tests` … 通過
- `./scripts/run-tests.sh`（非Spark） … 505 passed
- Spark 結合テスト（`pytest tests/ -m spark`） … 全件通過
  - `test_seed_reproducibility` / `test_result_is_independent_of_parallelism` /
    `test_no_duplicate_persona_with_overlapping_cells` /
    `test_disjoint_is_balanced_across_stimuli` が通ることが不変条件の担保
- 新しい回帰テストが修正前のコードで **failed**、修正後で **passed** になることを確認した
  （落ちないなら閾値が甘いという確認）

## 未対応事項

- **`screening.py` のリトライ**は `build_panel` を最大4回呼ぶ。候補を増やすための設計であって
  冗長呼び出しではないため、そのままにした。1回あたりが安くなった分の恩恵は受ける。
- **`Window.orderBy` に `partitionBy` が無い**ため、実行時に
  `No Partition Defined for Window operation` の警告が出る。対象は `limit(needed)` 後の
  数行〜数百行なので実害は無いが、警告自体は残る。セル単位で `partitionBy` を入れると
  `cell_rank` の決め方が変わりうるので、今回は触っていない。
- `personas_base` のレイアウト最適化（Z-order 等）は未着手。今回の症状の原因ではない。
- 実機（Databricks）での実測は未実施。ローカルのステージ数で線形性を確認した段階。
