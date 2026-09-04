"""プロンプト組み立てに必要なペルソナ属性の読み込み。

`run`（本調査）と `screen`（スクリーニング）の両方から使う。片方に置くと
もう片方から import することになり、循環参照になる。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.storage import delta
from persona_sim.storage.locator import PERSONAS_BASE, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import SparkSession

#: 調査定義が何を指定していても必ず集める列。
#:
#: `uuid` は突き合わせに要る。属性行と総括は既定で載るので、既定のまま実行したときに
#: 1回のクエリで済むようにしてある。設定で外した列がここに残っていても害は無い
#: （プロンプトに出ないだけ）。逆に**足りないと黙って空になる**ので、
#: `required_persona_columns()` で調査定義側の指定を必ず足す。
FIXED_PERSONA_COLUMNS = (
    "uuid",
    "sex",
    "age",
    "prefecture",
    "marital_status",
    "education_level",
    "occupation_raw",
    "persona",
)


def required_persona_columns(survey: SurveyDefinition) -> tuple[str, ...]:
    """調査定義が要求するペルソナ列（本調査＋スクリーニング）。

    属性行が設定可能になったため、`FIXED_PERSONA_COLUMNS` だけでは足りない。
    `region` や `employment_status` を属性行に入れた調査で列を集め忘れると、
    エラーにならず**その項目が消えたカード**でモデルに聞いてしまう。
    スクリーニング側の指定も同じ理由で足す（判定カードも同じ dict から描く）。
    """
    columns: list[str] = list(survey.persona_card.column_names())
    if survey.screening is not None:
        columns.extend(survey.screening.persona_card.column_names())
    return tuple(dict.fromkeys(columns))


def load_personas(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    persona_uuids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """ペルソナカードに必要な列だけをドライバへ集める。"""
    return load_persona_rows(
        spark,
        storage,
        persona_uuids,
        columns=[*FIXED_PERSONA_COLUMNS, *required_persona_columns(survey)],
    )


def load_persona_rows(
    spark: SparkSession,
    storage: StorageConfig,
    persona_uuids: Sequence[str],
    *,
    columns: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """指定した列だけを `personas_base` からドライバへ集める。

    調査定義の型に依存しない層。**「パネルに載っているのに `personas_base` に無い」を
    必ず落とす**のがこの層の役目で、呼ぶ側ごとに読み直すとその検知が片方にしか載らず、
    空のカードのままモデルに聞いてしまう経路ができる。

    `uuid` は必ず取る（突き合わせに要る）。重複した列名は詰める。
    """
    from pyspark.sql import functions as F

    wanted_columns = list(dict.fromkeys(["uuid", *columns]))
    # isin に数千件を並べると式が巨大になるので、小さな DataFrame との結合で絞る。
    wanted = spark.createDataFrame([(uuid,) for uuid in persona_uuids], "uuid string")
    personas = (
        delta.read_table(spark, locator(PERSONAS_BASE, storage))
        .join(F.broadcast(wanted), on="uuid", how="inner")
        .select(*wanted_columns)
        .collect()
    )
    loaded = {row["uuid"]: row.asDict() for row in personas}

    missing = set(persona_uuids) - set(loaded)
    if missing:
        raise PersonaSimError(
            f"パネルに載っているペルソナが personas_base に無い（{len(missing)} 件）。"
            "personas_base を作り直した場合はパネルも作り直すこと"
        )
    return loaded
