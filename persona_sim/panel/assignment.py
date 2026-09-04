"""提示設計に沿ったコンセプトの割り当て（`SPEC_PHASE1.md` §5）。

**ここを間違えると結果の意味が変わる。** 特に `sample_overlap: disjoint` の
セル内均等割り当て（round-robin）は `AGENTS.md` の不変条件。

割り当ては2段階に分けて考える。

1. **どのコンセプトを見るか**: セル内順位 `cell_rank` を起点に、定義順の配列から
   長さ m の巡回窓を取る。これによりコンセプトごとの評価者数が均等になる（差は最大1）。
2. **どの順で見るか**: `rotation` で決める。

窓の起点は `cell_rank % K` だけで決まるので、割り当ては **K 通りしかない**。
その対応表を純関数として作り（`assignment_table`）、Spark 側は結合するだけにしてある。
Spark 抜きで割り当ての正しさを検証できるようにするため。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from persona_sim.panel.schema import Design, Rotation

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

#: `cell_rank` から割り当て表を引くための結合キー。
ROTATION_INDEX = "rotation_index"


def assignment_table(design: Design, stimulus_ids: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """`cell_rank % K` ごとの `assigned_stimuli` を返す（要素数は K）。

    どの行にも重複は含まれない（§5.0 の大前提）。
    `rotation: random` の場合、ここでは定義順の集合を返し、
    実際の並べ替えはペルソナごとに Spark 側で行う。
    """
    ids = tuple(stimulus_ids)
    total = len(ids)
    if total == 0:
        raise ValueError("コンセプトが1件も無い")

    per_persona = design.stimuli_count_per_persona(total)
    rows = []
    for start in range(total):
        indices = [(start + offset) % total for offset in range(per_persona)]
        if design.rotation is not Rotation.BALANCED:
            # 提示順を指定しない（none）／ペルソナ単位で並べ替える（random）場合は、
            # 表としては定義順に揃えておく。
            indices.sort()
        rows.append(tuple(ids[index] for index in indices))
    return tuple(rows)


def apply_assignment(
    members: DataFrame,
    design: Design,
    stimulus_ids: Sequence[str],
    seed: int,
) -> DataFrame:
    """`members`（`cell_rank` を持つ）に `assigned_stimuli` を付与する。"""
    from pyspark.sql import functions as F

    table = assignment_table(design, stimulus_ids)
    total = len(table)

    spark = members.sparkSession
    lookup = spark.createDataFrame(
        [(index, list(ids)) for index, ids in enumerate(table)],
        f"{ROTATION_INDEX} int, assigned_stimuli array<string>",
    )

    assigned = members.withColumn(ROTATION_INDEX, F.col("cell_rank") % F.lit(total)).join(
        F.broadcast(lookup), on=ROTATION_INDEX, how="inner"
    )

    if design.rotation is Rotation.RANDOM:
        assigned = assigned.withColumn("assigned_stimuli", _shuffled(seed))

    return assigned.drop(ROTATION_INDEX)


def _shuffled(seed: int):
    """ペルソナごとに決定論的な順序へ並べ替える式。

    `F.shuffle` は実行のたびに結果が変わるので使えない（再現性の不変条件）。
    コンセプトIDとペルソナ uuid・seed からハッシュを作り、その昇順に並べる。
    """
    from pyspark.sql import functions as F

    keyed = F.transform(
        F.col("assigned_stimuli"),
        lambda stimulus: F.struct(
            F.sha2(
                F.concat_ws("|", F.col("persona_uuid"), F.lit(str(seed)), stimulus), 256
            ).alias("sort_key"),
            stimulus.alias("stimulus_id"),
        ),
    )
    return F.transform(F.array_sort(keyed), lambda item: item["stimulus_id"])
