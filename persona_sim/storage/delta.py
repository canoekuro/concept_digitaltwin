"""Delta テーブルの読み書きヘルパ。

`TableLocator` の形（カタログ／パス）を吸収し、呼び出し側が分岐しないようにする。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from persona_sim.storage.locator import TableLocator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession


def read_table(spark: SparkSession, loc: TableLocator) -> DataFrame:
    if loc.uses_catalog:
        return spark.read.table(loc.identifier)
    return spark.read.format("delta").load(loc.path)


def write_table(df: DataFrame, loc: TableLocator, *, mode: str = "overwrite") -> None:
    """テーブルを書き出す。

    `mode="overwrite"` はスキーマの変更も許可する（`overwriteSchema`）。
    調査単位の追記は `mode="append"` を使う。
    """
    writer = df.write.format("delta").mode(mode)
    if mode == "overwrite":
        writer = writer.option("overwriteSchema", "true")
    if loc.uses_catalog:
        writer.saveAsTable(loc.identifier)
    else:
        writer.save(loc.path)


def merge_upsert(
    spark: SparkSession,
    df: DataFrame,
    loc: TableLocator,
    keys: Sequence[str],
    *,
    evolve_schema: bool = False,
) -> None:
    """主キーで upsert する（§6.4）。

    同じキーの行があれば置き換え、無ければ挿入する。再実行しても行が二重にならない。
    テーブルがまだ無い場合は単純に作る。

    `evolve_schema=True` は、既存テーブルに無い列が `df` にあるとき列を足させる。
    列を増やしたリリースを、既にあるテーブルの上に載せられるようにするためのもの。
    既定を False にしてあるのは、typo で増えた列が黙って本番テーブルに入るのを防ぐため。
    """
    if not table_exists(spark, loc):
        write_table(df, loc, mode="append")
        return

    from delta.tables import DeltaTable

    table = (
        DeltaTable.forName(spark, loc.identifier)
        if loc.uses_catalog
        else DeltaTable.forPath(spark, loc.path)
    )
    condition = " AND ".join(f"target.`{key}` = source.`{key}`" for key in keys)
    builder = table.alias("target").merge(df.alias("source"), condition)
    if evolve_schema:
        builder = builder.withSchemaEvolution()
    builder.whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()


def table_exists(spark: SparkSession, loc: TableLocator) -> bool:
    from delta.tables import DeltaTable

    if loc.uses_catalog:
        return spark.catalog.tableExists(loc.identifier)
    return DeltaTable.isDeltaTable(spark, loc.path)


def table_version(spark: SparkSession, loc: TableLocator) -> int | None:
    """現在の Delta バージョン。`runs` に記録して再現性を担保する（§9）。"""
    if not table_exists(spark, loc):
        return None
    from delta.tables import DeltaTable

    table = (
        DeltaTable.forName(spark, loc.identifier)
        if loc.uses_catalog
        else DeltaTable.forPath(spark, loc.path)
    )
    history = table.history(1).select("version").collect()
    return int(history[0]["version"]) if history else None


def delete_survey_rows(spark: SparkSession, loc: TableLocator, survey_id: str) -> None:
    """指定調査の行を消す。パネルを作り直すときに使う。"""
    if not table_exists(spark, loc):
        return
    from delta.tables import DeltaTable
    from pyspark.sql import functions as F

    table = (
        DeltaTable.forName(spark, loc.identifier)
        if loc.uses_catalog
        else DeltaTable.forPath(spark, loc.path)
    )
    # 述語は Column で組む。survey_id を文字列に埋め込むと引用符で壊れる。
    table.delete(F.col("survey_id") == F.lit(survey_id))
