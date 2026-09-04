"""集計の実行（`SPEC_PHASE1.md` §7）。

`responses` を読んで §7.1 の表を作り、§7.4 のファイル一式に書き出す。

**集計結果はテーブルに保存しない。** 保存した表と生データが食い違ったとき、
どちらが正しいのかを決める手立てが無いため。読むたびに数え直す。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from persona_sim.aggregate import frame
from persona_sim.aggregate import segments as segment_axes
from persona_sim.aggregate.crosstab import (
    Answer,
    ConceptSummaryRow,
    Crosstab,
    concept_summary,
    crosstabs,
    tabulated_measures,
)
from persona_sim.aggregate.tables import Table, concept_axis_table, stacked_crosstab_table
from persona_sim.config import StorageConfig
from persona_sim.panel.quotas import allocate_cell_sizes
from persona_sim.panel.sampling import (
    ROLE_CANDIDATE,
    ROLE_MAIN,
    ROLE_RESERVE,
    ROLE_SCREENED_OUT,
)
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.panel.screening import incidence_from_panels
from persona_sim.run import flags as flag_names
from persona_sim.run.run import FLAG_WARNING_THRESHOLD

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import SparkSession

#: 集計時のトップライン軸。`output.segments` に何が指定されていても必ず出す。
TOPLINE_SEGMENTS = (segment_axes.TOTAL,)


@dataclass
class AggregateResult:
    survey_id: str
    crosstabs: list[Crosstab] = field(default_factory=list)
    topline: list[ConceptSummaryRow] = field(default_factory=list)
    by_segment: list[ConceptSummaryRow] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    #: 書き出したファイル。
    written: list[Path] = field(default_factory=list)
    #: E3 / E4 などの注記（§11）。集計表と標準出力の両方に出す。
    notes: list[str] = field(default_factory=list)
    answers: int = 0


def aggregate_survey(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    output_dir: str,
    *,
    formats: Sequence[str] | None = None,
) -> AggregateResult:
    """集計してファイル一式を書く。

    書き出す形式は `output.formats`（`csv` / `xlsx`）で決まる。`formats` を明示すると
    調査定義より優先する。

    **集計結果はテーブルに保存しない。** 毎回 `responses` から数え直す——保存した表と
    生データが食い違ったとき、どちらが正しいのかを決める手立てが無いため。
    """
    from persona_sim.aggregate.export import write_outputs

    selected = tuple(formats if formats is not None else survey.output.formats)
    answers = frame.load_answers(spark, survey, storage)
    panel_rows = frame.load_panel_rows(spark, survey, storage)
    result = build_result(survey, answers, panel_rows)

    result.written = write_outputs(spark, survey, storage, result, output_dir, formats=selected)
    return result


def build_result(
    survey: SurveyDefinition,
    answers: Sequence[Answer],
    panel_rows: Sequence[Mapping[str, Any]],
) -> AggregateResult:
    """読み込み済みの材料から集計結果を組み立てる。**Spark を使わない。**

    Spark 経由（`aggregate_survey`）と SQL Warehouse 経由（Web UI）で、集計の算術を
    共有するための入口。読み方が違っても同じ数字になることを、ここを通すことで保証する。

    `panel_rows` は `cell_id` / `role` / `weight` を持つ行の並び（`frame.load_panel_rows`
    と同じ形）。パネル構成表を出さないなら空で渡してよい。
    """
    output_segments = _segments(survey)

    result = AggregateResult(survey_id=survey.survey_id, answers=len(answers))
    result.crosstabs = crosstabs(survey, answers, output_segments)
    result.topline = concept_summary(survey, answers, TOPLINE_SEGMENTS)
    result.by_segment = concept_summary(survey, answers, output_segments)
    result.notes = _notes(answers) + _off_target_notes(survey, answers)
    result.tables = _build_tables(survey, panel_rows, result)
    return result


def _segments(survey: SurveyDefinition) -> tuple[str, ...]:
    """集計に使う軸。`total` は必ず含める（トップラインが無いと比較できない）。"""
    configured = tuple(survey.output.segments)
    if segment_axes.TOTAL in configured:
        return configured
    return (segment_axes.TOTAL, *configured)


def _notes(answers: Sequence[Any]) -> list[str]:
    """E3 / E4 の注記（§11）。閾値を超えたら集計表に必ず残す。"""
    notes: list[str] = []
    total = len(answers)
    if total == 0:
        return notes
    flagged = sum(1 for answer in answers if answer.flagged)
    rate = flagged / total
    if rate > FLAG_WARNING_THRESHOLD:
        notes.append(
            f"品質フラグの立った回答が全体の {rate:.1%}（{flagged} / {total} 件）ある。"
            f"「{'」「'.join(flag_names.QUALITY_FLAGS)}」のいずれかが立った回答で、"
            "集計からは除外していない（フラグ除外後の n を併記してある）"
        )
    return notes


def _off_target_notes(survey: SurveyDefinition, answers: Sequence[Answer]) -> list[str]:
    """調査定義の割り付けセルに無い属性値を表面化させる（§11）。

    集計する回答は、その調査の割り付けセルに収まっているはずのもの。収まっていない
    なら、パネルと `responses` の対応が壊れているか、**別の調査の結果を読んでいる**。
    どちらも数字そのものは出てしまうので、黙っていると対象外の回答者を含んだ集計表を
    そのまま読むことになる。止めはせず（品質フラグと同じ扱い）、注記で必ず残す。

    セル側に指定が無い属性は照合しない。条件を書いていない以上、どの値も対象外ではない。
    """
    notes: list[str] = []
    cells = survey.panel.quotas.cells
    if not cells:
        return notes

    defined_sex = {cell.conditions.sex for cell in cells}
    if None not in defined_sex:
        unexpected = _unexpected(answers, "sex", {str(value) for value in defined_sex})
        if unexpected:
            notes.append(
                f"調査定義の割り付けは {'・'.join(sorted(str(s) for s in defined_sex))} のみだが、"
                f"集計対象に {'・'.join(unexpected)} の回答者が含まれている。"
                "パネルと回答の対応が壊れているか、別の調査の結果を見ている可能性がある"
            )

    unexpected_cells = _unexpected(answers, "cell_id", {cell.cell_id for cell in cells})
    if unexpected_cells:
        notes.append(
            f"調査定義に無い割り付けセル（{'・'.join(unexpected_cells)}）の回答が"
            "集計対象に含まれている。`panels` に別の調査の行が残っていないか確認すること"
        )
    return notes


def _unexpected(answers: Sequence[Answer], axis: str, defined: set[str]) -> list[str]:
    """`answers` に現れた `axis` の値のうち、`defined` に無いもの。

    値を持たない回答は数えない。その属性を集めていない（`output.segments` に
    入っていない）だけで、対象外だとは言えない。
    """
    observed = {
        str(answer.attributes[axis])
        for answer in answers
        if answer.attributes.get(axis) not in (None, "")
    }
    return sorted(observed - defined)


# --------------------------------------------------------------------------- #
# 表の組み立て
# --------------------------------------------------------------------------- #


def _build_tables(
    survey: SurveyDefinition,
    panel_rows: Sequence[Mapping[str, Any]],
    result: AggregateResult,
) -> list[Table]:
    # measure ごとに1表（選択肢ラベルつき）。設問IDではなく measure で束ねるのは、
    # 同じ問いが slot ごとに別IDへ展開されるため（`tabulated_measures()`）。
    tables = [
        concept_axis_table(
            survey,
            result.crosstabs,
            question.measure_key,
            key=f"crosstab_{question.measure_key}",
            title=f"{question.measure_key}: {question.text}",
            with_segment=True,
        )
        for question in tabulated_measures(survey)
    ]
    # 全設問を1枚に積んだ表。コンセプトを measure 横断で見比べるのはこちら。
    tables.append(
        stacked_crosstab_table(
            survey,
            result.crosstabs,
            key="crosstab_all",
            title="クロス集計表（コンセプト × セグメント）",
        )
    )
    tables.append(panel_composition_table_from_rows(survey, panel_rows))
    return tables



def panel_composition_table_from_rows(
    survey: SurveyDefinition, panel_rows: Sequence[Mapping[str, Any]]
) -> Table:
    """パネル構成表を `panels` の行から組み立てる。**Spark を使わない。**

    `metadata._panel_summary` と同じ材料（`allocate_cell_sizes` /
    `incidence_from_panels`）を使う。数え方を二重に実装しない。
    """
    rows_by_role: dict[tuple[str, str], int] = {}
    weights: dict[str, float] = {}
    for row in panel_rows:
        key = (str(row["cell_id"]), str(row["role"]))
        rows_by_role[key] = rows_by_role.get(key, 0) + 1
        if row["role"] == ROLE_MAIN and row["weight"] is not None:
            weights[str(row["cell_id"])] = float(row["weight"])

    requested = allocate_cell_sizes(survey)
    incidence = incidence_from_panels(panel_rows)
    screener = survey.screening
    asks = screener is not None and screener.asks
    infers = screener is not None and screener.infers
    # ask は実測、infer は推定。どちらも数字は出せるが、意味が違うので注記で分ける。
    has_rate = asks or infers

    columns = ("cell_id", "目標", "確定", "予備", "非通過", "未判定", "通過率", "weight")
    body: list[list[object]] = []
    for cell in survey.panel.quotas.cells:
        cell_id = cell.cell_id
        rate = incidence.get(cell_id)
        body.append(
            [
                cell_id,
                requested.get(cell_id, 0),
                rows_by_role.get((cell_id, ROLE_MAIN), 0),
                rows_by_role.get((cell_id, ROLE_RESERVE), 0),
                rows_by_role.get((cell_id, ROLE_SCREENED_OUT), 0),
                rows_by_role.get((cell_id, ROLE_CANDIDATE), 0),
                f"{rate:.1%}" if (has_rate and rate is not None) else "-",
                f"{weights.get(cell_id, 1.0):.4f}",
            ]
        )

    notes = []
    if infers:
        notes.append(
            "mode: infer のため通過率は**推定値**。本人には聞いておらず、"
            "別の判定で条件合致の蓋然性が高いとされた割合であって実測値ではない（§4.2）"
        )
    elif not asks:
        notes.append(
            "スクリーニングを実行していない（screener 無し、または mode: assume）ため"
            "通過率は測定していない。1.0 ではなく未測定として扱うこと（§9）"
        )
    return Table(
        key="panel_composition",
        title="パネル構成とインシデンス",
        columns=columns,
        rows=body,
        notes=tuple(notes),
    )
