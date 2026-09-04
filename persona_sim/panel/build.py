"""パネル構築（`SPEC_PHASE1.md` §2.2, §4, §5）。

`select_members`（誰を選ぶか）と `apply_assignment`（何をどの順で見せるか）を束ね、
`panels` テーブルに書き出す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.panel.assignment import apply_assignment
from persona_sim.panel.quotas import allocate_cell_sizes, cell_weights
from persona_sim.panel.sampling import (
    ROLE_CANDIDATE,
    ROLE_MAIN,
    ROLE_RESERVE,
    ROLE_SCREENED_OUT,
    SelectionReport,
    select_members,
)
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.storage import delta
from persona_sim.storage.locator import PANELS, PERSONAS_BASE, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

#: `panels` の列（§2.2）。順序も含めてこの通りに書き出す。
#: `cell_rank` はセル内の抽出順。コンセプト割り当ての起点であり、
#: スクリーニング後の確定（`finalize_panel`）で先着順を決めるのにも使う。
PANEL_COLUMNS = (
    "survey_id",
    "persona_uuid",
    "cell_id",
    "role",
    "cell_rank",
    "assigned_stimuli",
    "weight",
)


@dataclass
class PanelResult:
    panel: DataFrame
    selection: SelectionReport
    personas_version: int | None


def candidate_sizes(survey: SurveyDefinition) -> dict[str, int]:
    """判定を伴う方式で抽出する候補数（割り付け × オーバーサンプル倍率）。

    `ask` と `infer` はどちらも非通過者が出るのでオーバーサンプルする。
    `assume` は全員に条件を与えるので必要な人数ちょうどでよい。
    """
    screener = survey.screening
    factor = screener.oversample_factor if screener and screener.calls_llm else 1
    return {cell_id: size * factor for cell_id, size in allocate_cell_sizes(survey).items()}


def build_panel(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    *,
    write: bool = True,
    oversample_factor: int | None = None,
) -> PanelResult:
    """パネルを構築し、必要なら `panels` テーブルへ書き出す。

    判定を伴うスクリーナー（`ask` / `infer`）がある場合は、判定前の候補を
    `role='candidate'` で書く（誰が通過するかは `screen` を実行するまで分からない）。
    それ以外は今までどおり `main` を確定させてコンセプトを割り当てる。

    同じ `survey_id` の既存行は消してから書き直すので、作り直しても重複しない。
    """
    from pyspark.sql import functions as F

    personas_locator = locator(PERSONAS_BASE, storage)
    personas = delta.read_table(spark, personas_locator)

    screener = survey.screening
    screening = screener is not None and screener.calls_llm

    sizes = candidate_sizes(survey)
    if screening and oversample_factor is not None:
        base = allocate_cell_sizes(survey)
        sizes = {cell_id: size * oversample_factor for cell_id, size in base.items()}

    members, selection = select_members(
        spark,
        personas,
        survey,
        cell_sizes=sizes if screening else None,
        role=ROLE_CANDIDATE if screening else ROLE_MAIN,
    )

    if screening:
        # 判定前なのでコンセプトは割り当てない。誰が本調査に進むかまだ決まっていない。
        assigned = members.withColumn(
            "assigned_stimuli", F.array().cast("array<string>")
        )
    else:
        assigned = apply_assignment(
            members, survey.design, [stimulus.id for stimulus in survey.stimuli], survey.panel.seed
        )

    weight_map = F.create_map(
        *[
            item
            for cell_id, weight in selection.weights.items()
            for item in (F.lit(cell_id), F.lit(float(weight)))
        ]
    )
    panel = (
        assigned.withColumn("survey_id", F.lit(survey.survey_id))
        .withColumn("weight", weight_map[F.col("cell_id")])
        .select(*PANEL_COLUMNS)
    )

    if write:
        panels_locator = locator(PANELS, storage)
        delta.delete_survey_rows(spark, panels_locator, survey.survey_id)
        delta.write_table(panel, panels_locator, mode="append")

    return PanelResult(
        panel=panel,
        selection=selection,
        personas_version=delta.table_version(spark, personas_locator),
    )


@dataclass
class FinalizeResult:
    """スクリーニング後のパネル確定の結果。"""

    achieved: dict[str, int] = field(default_factory=dict)
    reserve: dict[str, int] = field(default_factory=dict)
    screened_out: dict[str, int] = field(default_factory=dict)
    shortfalls: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.shortfalls


def finalize_panel(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    passed: set[str],
) -> FinalizeResult:
    """判定結果からパネルを確定する（§4.2 の手順4〜6）。

    候補を抽出順（`cell_rank`）に見て、通過者の先頭から必要数を `main` に確定する。
    残りの通過者は `reserve`、非通過は `screened_out` として**残す**（削除しない）。

    **確定した `main` だけで `cell_rank` を 0 から振り直してから割り当てる。**
    オーバーサンプル時の順位のまま割り当てると、非通過者が抜けた穴の分だけ
    round-robin がずれて、コンセプトごとの評価者数が崩れる（§5.2）。
    """
    from pyspark.sql import functions as F

    panels_locator = locator(PANELS, storage)
    candidates = (
        delta.read_table(spark, panels_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .filter(F.col("role") == F.lit(ROLE_CANDIDATE))
        .select("persona_uuid", "cell_id", "cell_rank")
        .orderBy("cell_id", "cell_rank")
        .collect()
    )
    if not candidates:
        raise PersonaSimError(
            f"survey_id={survey.survey_id} に判定対象の候補が無い。先に `persona-sim panel` を実行すること"
        )

    requested = allocate_cell_sizes(survey)
    result = FinalizeResult()
    main_rows: list[tuple[str, str, int]] = []
    other_rows: list[tuple[str, str, str]] = []

    by_cell: dict[str, list[str]] = {}
    for row in candidates:
        by_cell.setdefault(row["cell_id"], []).append(row["persona_uuid"])

    # **割り付けにあるセルを必ず1周する。** `by_cell` だけを回すと、候補が1件も無いセルが
    # ループに入らず `shortfalls` に載らないまま `complete` が真になる
    # （＝そのセル0人のまま本調査に進む）。現状は `select_members()` が全セルで必要数を
    # 確保してから書くので候補ゼロは起きないが、充足判定がその前提に黙って依存する形にはしない。
    #
    # 割り付けに無いセルの候補（調査定義を変えた後などに残る）も落とさず回す。
    # 必要数0として `reserve` / `screened_out` に振られ、行としては残る。
    for cell_id in [*requested, *(c for c in by_cell if c not in requested)]:
        uuids = by_cell.get(cell_id, [])
        needed = requested.get(cell_id, 0)
        confirmed = 0
        for persona_uuid in uuids:  # 抽出順。先着で確定する
            if persona_uuid not in passed:
                other_rows.append((persona_uuid, cell_id, ROLE_SCREENED_OUT))
                continue
            if confirmed < needed:
                # ここで順位を振り直す。候補の順位をそのまま使ってはいけない。
                main_rows.append((persona_uuid, cell_id, confirmed))
                confirmed += 1
            else:
                other_rows.append((persona_uuid, cell_id, ROLE_RESERVE))

        result.achieved[cell_id] = confirmed
        result.reserve[cell_id] = sum(
            1 for uuid, cid, role in other_rows if cid == cell_id and role == ROLE_RESERVE
        )
        result.screened_out[cell_id] = sum(
            1 for uuid, cid, role in other_rows if cid == cell_id and role == ROLE_SCREENED_OUT
        )
        if confirmed < needed:
            result.shortfalls[cell_id] = needed - confirmed

    if not result.complete:
        return result

    weights = cell_weights(survey, result.achieved)
    weight_map = F.create_map(
        *[
            item
            for cell_id, weight in weights.items()
            for item in (F.lit(cell_id), F.lit(float(weight)))
        ]
    )

    members = spark.createDataFrame(
        main_rows, "persona_uuid string, cell_id string, cell_rank int"
    ).withColumn("role", F.lit(ROLE_MAIN))
    assigned = apply_assignment(
        members, survey.design, [stimulus.id for stimulus in survey.stimuli], survey.panel.seed
    )
    main_panel = (
        assigned.withColumn("survey_id", F.lit(survey.survey_id))
        .withColumn("weight", weight_map[F.col("cell_id")])
        .select(*PANEL_COLUMNS)
    )

    others = (
        spark.createDataFrame(other_rows, "persona_uuid string, cell_id string, role string")
        .withColumn("survey_id", F.lit(survey.survey_id))
        # 本調査に進まないので順位もコンセプトも持たない。行は残す（§4.2）。
        .withColumn("cell_rank", F.lit(None).cast("int"))
        .withColumn("assigned_stimuli", F.array().cast("array<string>"))
        .withColumn("weight", F.lit(1.0))
        .select(*PANEL_COLUMNS)
    )

    delta.delete_survey_rows(spark, panels_locator, survey.survey_id)
    delta.write_table(main_panel.unionByName(others), panels_locator, mode="append")
    return result


def composition_lines(survey: SurveyDefinition, selection: SelectionReport) -> list[str]:
    """パネル構成のサマリ。実行前に構成を目で確かめるために出す。"""
    lines = [
        f"{'セル':<16}{'要求':>8}{'実績':>8}{'候補':>10}{'ウェイト':>10}",
        "-" * 52,
    ]
    for cell in survey.panel.quotas.cells:
        cell_id = cell.cell_id
        lines.append(
            f"{cell_id:<16}"
            f"{selection.requested.get(cell_id, 0):>8}"
            f"{selection.achieved.get(cell_id, 0):>8}"
            f"{selection.raw_candidates.get(cell_id, 0):>10}"
            f"{selection.weights.get(cell_id, 0.0):>10.3f}"
        )
    lines.append("-" * 52)
    lines.append(f"{'合計':<16}{survey.panel.size:>8}{selection.total_achieved:>8}")
    return lines
