# パネル抽出の再計算爆発を解消する（計画）

- **日付**: 2026/08/06 03:07
- **対象**: `persona_sim/panel/sampling.py`, `persona_sim/panel/quotas.py`, `tests/test_panel_spark.py`

## 背景

Databricks で 200サンプルの調査を実行したところ、`build_panel` が13分を超えても終わらなかった。
Spark UI では単一ジョブが15ステージ・2700タスクを抱え、ステージのタスク数が

```
36 → 63 → 99 → 144 → 198 → 261 → 333 → 414 → 504 → 603
```

（差分が 27, 36, 45, … と 9 ずつ増える三角数）と並んでいた。合計 2655 で、
ジョブ全体の 2700タスクとほぼ一致する。

ステージが10本なのは、割り付けが `性別 × 10歳刻み`（`persona_sim/uiconfig/allocation.py`
の `build_quotas`）で **男女 × 5バンド = 10セル**だったため。

## 原因

`select_members`（`persona_sim/panel/sampling.py`）のセルループ。

- `selected` は「これまでの全セルの `picked`」の union で、`picked_k` 自身も `selected_{k-1}`
  との anti-join を含む。k番目のセルの計算木に 1〜k-1番目の計算が入れ子で入る。
- コメントは「各セルの結果は cache 済みなので再計算は起きない」と書いていたが、
  `persona_sim/` 全体に `cache()` / `persist()` / `checkpoint()` は**1箇所も無かった**。
  前提が崩れたままコメントだけが残っていた。
- `picked.count()` がセルごとに action を打つため、この再計算がセル数だけ繰り返される。

**コストを決めているのはサンプル数ではなくセル数。** `性別 × 5歳刻み`（20セル）では
さらに悪化する。`persona_sim/panel/screening.py` はリトライで `build_panel` を最大4回呼ぶため、
そこでも同じコストが重なる。

## 方針

出力（選ばれる人・`cell_rank`・割り当て・ウェイト）は**現行と完全に同一**に保つ。
`AGENTS.md` の不変条件（seed再現性、並列度非依存、パネル内 `persona_uuid` 一意、
`disjoint` の round-robin 均等、「記述順で**先に選ばれた人**を後続セルから除外」）は変えない。

セル条件から一括で振り分ける（when チェーン）書き換えは、重複除外が
「条件に合う人を除外」に変わり §5.0 の意味が変わるため採らない。ループ構造は維持する。

1. **`quotas.py`**: 抽出に必要な列 `SELECTION_COLUMNS`（`uuid` ＋ `filter_condition` が
   参照する7列）を `filter_condition` の直上に定数化する。`personas_base` は26列あり
   長文の生成列を含むため、抽出ではここまで絞る。
2. **`sampling.py`**:
   - 母集団を `SELECTION_COLUMNS` に射影して1回だけ `cache()`。`try` / `finally` で
     必ず `unpersist()` する。
   - `picked.count()` を `picked.collect()` に置き換え、確定行 `(persona_uuid, cell_id,
     cell_rank)` を driver 側のリストに積む。`achieved` は `len()` から取る（action の数は不変）。
   - anti-join の除外集合は、前セルの DataFrame ではなく確定済み uuid のリストから
     `spark.createDataFrame` で作り直し、`F.broadcast` で結合する。
   - `parts` の union 連鎖をやめ、最後に `spark.createDataFrame` で1回だけ `members` を組む。
     `finalize_panel` が既に同じパターンを使っている。
   - セル内の抽出（`sha2` の `_rank_key` / `orderBy(...).limit(needed)` /
     `row_number().over(Window.orderBy(...)) - 1`）は一切変えない。
   - 嘘になっていたコメントを実態に合わせて書き直す。
3. **`tests/test_panel_spark.py`**: 正しさのテストはこの症状を全部通過してしまうため、
   コストを直接見る回帰テストを追加する。`setJobGroup` ＋ `statusTracker` でステージ数を数え、
   2セルと6セルを比較する。あわせて `SELECTION_COLUMNS` の取りこぼしを検出するテストを足す。

## 検証

1. `uv run ruff check persona_sim tests`
2. `./scripts/run-tests.sh`（非Spark）
3. Spark 結合テスト（CI と同じ版を uv で導入して実行）
4. **出力の同一性**: 修正前後で同一 seed・同一セル定義（重なるセルを含む10セル）の
   `select_members` を回し、`(persona_uuid, cell_id, cell_rank, role)` とレポートの
   完全一致を突き合わせる
5. 新しい回帰テストが修正前のコードで落ち、修正後に通ることを確認する

## スコープ外

- `screening.py` のリトライで `build_panel` を複数回呼ぶ構造（候補を増やすための設計）
- when チェーンによる全セル1パス化（§5.0 の意味が変わる）
- `personas_base` のレイアウト最適化（今回の症状の原因ではない）
