"""集約と出力（`SPEC_PHASE1.md` §7）。

`responses` を読んでクロス集計表・コンセプト比較表を作り、`aggregates` テーブルと
§7.4 のファイル一式に書き出す。

算術は `crosstab` / `segments` に閉じ込めてある（pyspark に依存しない）。Spark は
`frame` の読み込み・結合だけで使う。集計の数値そのものを Spark 抜きでテストできるようにするため。
"""

from persona_sim.aggregate.aggregate import AggregateResult, aggregate_survey

__all__ = ["AggregateResult", "aggregate_survey"]
