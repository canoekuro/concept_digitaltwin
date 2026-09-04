"""SparkSession の取得。

Databricks 上では既にセッションが張られているのでそれを再利用し、
ローカルでは Delta 拡張を有効にしたセッションを作る。
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from persona_sim.config import spark_master

if TYPE_CHECKING:  # pragma: no cover - 型注釈のためだけに読む
    from pyspark.sql import SparkSession


def get_spark(app_name: str = "persona-sim") -> SparkSession:
    """有効な SparkSession を返す。

    既存セッションがあればそれを使う（Databricks ノートブック・調査を想定）。
    無ければ Delta 拡張つきのローカルセッションを作る。
    """
    from pyspark.sql import SparkSession

    existing = SparkSession.getActiveSession()
    if existing is not None:
        return existing

    from delta import configure_spark_with_delta_pip

    # ワーカーはドライバと同じ Python を使う必要がある（マイナーバージョンが違うと起動時に失敗する）。
    # venv 内から実行したときに素の `python3` を拾わないよう、未設定なら明示する。
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    builder = (
        SparkSession.builder.appName(app_name)
        .master(spark_master())
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
    )
    return configure_spark_with_delta_pip(builder).getOrCreate()
