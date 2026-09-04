"""`personas_base` の取り込みと正規化（`SPEC.md` §2.1）。

occupation 分解・年代バンドの Spark 式は `persona_sim.personas.normalize` の
語彙定数から組み立てる。**語彙をここに書き写さない。** 乖離を検出できなくなる
（`docs/schema/occupation-parsing.md`）。両実装が一致することは
`tests/test_personas_build_spark.py`（`-m spark`）で突き合わせて検証する。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_sim.config import StorageConfig
from persona_sim.personas import normalize
from persona_sim.personas.source import DEFAULT_REVISION, download_shards, source_version
from persona_sim.storage import delta
from persona_sim.storage.locator import PERSONAS_BASE, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import Column, DataFrame, SparkSession

#: `personas_base` の列（`SPEC.md` §2.1）。順序も含めてこの通りに書き出す。
PERSONAS_BASE_COLUMNS: tuple[str, ...] = (
    "uuid",
    "sex",
    "age",
    "age_band_5",
    "age_band_10",
    "prefecture",
    "region",
    "area",
    "marital_status",
    "education_level",
    "occupation_raw",
    "occupation_industry",
    "occupation_scale",
    "occupation_role",
    "employment_status",
    "persona",
    "cultural_background",
    "professional_persona",
    "sports_persona",
    "arts_persona",
    "travel_persona",
    "culinary_persona",
    "skills_and_expertise",
    "hobbies_and_interests",
    "career_goals_and_ambitions",
    "source_version",
)


# --------------------------------------------------------------------------- #
# occupation 分解
# --------------------------------------------------------------------------- #


def _suffix_pattern(tokens: Iterable[str]) -> str:
    """`tokens` のいずれかで終わることを見る Java 正規表現。`"\\s(A|B)$"` の形。

    丸括弧など正規表現の特殊文字を含むトークン（`(現在は引退)` 等）をエスケープする。
    """
    escaped = [re.escape(token) for token in tokens]
    return r"\s(" + "|".join(escaped) + ")$"


def occupation_columns() -> dict[str, Column]:
    """`occupation_raw` から業種・規模・役職・就業状態を導出する Spark 式。

    末尾から「就業状態 → 役職 → 規模」の順に既知の語を剥がし、残りを業種とする
    （`docs/schema/occupation-parsing.md`）。該当が無ければ `occupation_scale` /
    `occupation_role` は null にする。
    """
    from pyspark.sql import functions as F

    raw = F.coalesce(F.col("occupation_raw"), F.lit(""))

    employment_pattern = _suffix_pattern(normalize.EMPLOYMENT_TOKENS)
    employment_match = F.regexp_extract(raw, employment_pattern, 1)
    after_employment = F.regexp_replace(raw, employment_pattern, "")

    employment_status = F.lit(normalize.EMPLOYMENT_DEFAULT)
    for original, normalized_value in normalize.EMPLOYMENT_TOKENS.items():
        employment_status = F.when(
            employment_match == F.lit(original), F.lit(normalized_value)
        ).otherwise(employment_status)

    role_pattern = _suffix_pattern(normalize.ROLE_TOKENS)
    role_match = F.regexp_extract(after_employment, role_pattern, 1)
    after_role = F.regexp_replace(after_employment, role_pattern, "")
    occupation_role = F.when(role_match == "", F.lit(None)).otherwise(role_match)

    scale_pattern = _suffix_pattern(normalize.SCALE_TOKENS)
    scale_match = F.regexp_extract(after_role, scale_pattern, 1)
    occupation_industry = F.regexp_replace(after_role, scale_pattern, "")
    occupation_scale = F.when(scale_match == "", F.lit(None)).otherwise(scale_match)

    return {
        "occupation_industry": occupation_industry,
        "occupation_scale": occupation_scale,
        "occupation_role": occupation_role,
        "employment_status": employment_status,
    }


# --------------------------------------------------------------------------- #
# 年代バンド
# --------------------------------------------------------------------------- #


def age_band_columns() -> dict[str, Column]:
    """`age` から `age_band_5` / `age_band_10` を導出する Spark 式。

    `normalize.age_band_5` / `normalize.age_band_10` と同じ結果になる。
    100歳以上は両方とも `normalize.AGE_BAND_CENTENARIAN` にまとめる。
    """
    from pyspark.sql import functions as F

    age = F.col("age")
    centenarian = F.lit(normalize.AGE_BAND_CENTENARIAN)

    lower_5 = F.floor(age / F.lit(5)) * F.lit(5)
    band_5 = F.concat(lower_5.cast("string"), F.lit("-"), (lower_5 + F.lit(4)).cast("string"))
    age_band_5 = F.when(age >= F.lit(100), centenarian).otherwise(band_5)

    lower_10 = F.floor(age / F.lit(10)) * F.lit(10)
    band_10 = F.concat(lower_10.cast("string"), F.lit("代"))
    age_band_10 = F.when(age >= F.lit(100), centenarian).otherwise(band_10)

    return {"age_band_5": age_band_5, "age_band_10": age_band_10}


# --------------------------------------------------------------------------- #
# 正規化・構築
# --------------------------------------------------------------------------- #


def normalize_personas(df: DataFrame, version: str) -> DataFrame:
    """生の parquet を `personas_base` の形に整える。

    `occupation` を `occupation_raw` にリネームして分解列を足し、年代バンドと
    `source_version` を付けたうえで `PERSONAS_BASE_COLUMNS` の順に select する。
    """
    from pyspark.sql import functions as F

    enriched = df.withColumnRenamed("occupation", "occupation_raw")
    for name, column in occupation_columns().items():
        enriched = enriched.withColumn(name, column)
    for name, column in age_band_columns().items():
        enriched = enriched.withColumn(name, column)
    enriched = enriched.withColumn("source_version", F.lit(version))

    return enriched.select(*PERSONAS_BASE_COLUMNS)


@dataclass
class PersonasBuildResult:
    rows: int
    version: int | None
    source_version: str
    location: str


def build_personas_base(
    spark: SparkSession,
    storage: StorageConfig,
    *,
    shards: int = 1,
    revision: str = DEFAULT_REVISION,
    local_paths: list[str] | None = None,
) -> PersonasBuildResult:
    """parquet を読み、正規化し、`personas_base` を overwrite で書き出す。

    `local_paths` を渡した場合はダウンロードせずそのパスを読む（テスト・オフライン用）。
    """
    paths = (
        local_paths
        if local_paths is not None
        else download_shards(shards=shards, revision=revision)
    )

    raw = spark.read.parquet(*paths)
    version = source_version(revision)
    normalized = normalize_personas(raw, version)

    personas_locator = locator(PERSONAS_BASE, storage)
    delta.write_table(normalized, personas_locator, mode="overwrite")

    return PersonasBuildResult(
        # 書き出し済みの Delta を数える。`normalized` を数えると parquet の読み直しと
        # 正規表現の適用がもう一度走る（100万行規模では無視できない）。
        rows=delta.read_table(spark, personas_locator).count(),
        version=delta.table_version(spark, personas_locator),
        source_version=version,
        location=personas_locator.describe(),
    )
