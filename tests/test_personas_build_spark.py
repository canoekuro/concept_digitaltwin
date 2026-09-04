"""`persona_sim.personas.build` の Spark 式が参照実装（`normalize.py`）と一致することを検証する。

Spark を起動するため `-m spark` でのみ実行する。設計判断はしない。既に確定した
分解規則（`docs/schema/occupation-parsing.md`）どおりに Spark 式が動くかだけを見る。
"""

import pytest

from persona_sim.personas import normalize
from persona_sim.personas.build import age_band_columns, occupation_columns

pytestmark = pytest.mark.spark

#: 分解パターンを網羅する代表値（`docs/schema/occupation-parsing.md` 参照）。
OCCUPATION_SAMPLES = [
    "介護福祉業 中堅",
    "介護福祉業 中堅 (現在は引退)",
    "小売業 中小 経営 (現在は引退)",
    "地方公務員",
    "農業",
    "学生",
    "国家公務員",
    "漁業",
    "林業",
    "卸売業 大手 (現在は離職)",
    "建設業 大手 経営",
    "小売業 中堅 (現在は離職)",
]


def test_occupation_columns_match_reference(spark):
    from pyspark.sql import Row

    rows = [Row(occupation_raw=value) for value in OCCUPATION_SAMPLES]
    df = spark.createDataFrame(rows)

    columns = occupation_columns()
    result = df.select(
        "occupation_raw",
        columns["occupation_industry"].alias("occupation_industry"),
        columns["occupation_scale"].alias("occupation_scale"),
        columns["occupation_role"].alias("occupation_role"),
        columns["employment_status"].alias("employment_status"),
    ).collect()

    assert len(result) == len(OCCUPATION_SAMPLES)
    for row in result:
        expected = normalize.parse_occupation(row["occupation_raw"])
        assert row["occupation_industry"] == expected.industry
        assert row["occupation_scale"] == expected.scale
        assert row["occupation_role"] == expected.role
        assert row["employment_status"] == expected.employment_status


def test_age_band_columns_match_reference(spark):
    from pyspark.sql import Row

    ages = list(range(18, 101))
    rows = [Row(age=age) for age in ages]
    df = spark.createDataFrame(rows)

    columns = age_band_columns()
    result = df.select(
        "age",
        columns["age_band_5"].alias("age_band_5"),
        columns["age_band_10"].alias("age_band_10"),
    ).collect()

    assert len(result) == len(ages)
    for row in result:
        assert row["age_band_5"] == normalize.age_band_5(row["age"])
        assert row["age_band_10"] == normalize.age_band_10(row["age"])
