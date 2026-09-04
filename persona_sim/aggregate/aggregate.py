"""集計の実行（`SPEC_PHASE1.md` §7）。

`responses` を読んで §7.1・§7.2 の表を作り、`aggregates` テーブル（§2.5）と
§7.4 のファイル一式に書き出す。
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
    CrosstabRow,
    Metric,
    concept_summary,
    crosstabs,
    tabulated_measures,
)
from persona_sim.aggregate.tables import Table, concept_summary_table, crosstab_table
from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
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
from persona_sim.storage import delta
from persona_sim.storage.locator import AGGREGATES, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import SparkSession

#: `aggregates` のスキーマ（§2.5）。表示用に整形する前の**生の値**を持つ。
#: `question_id` ではなく `measure` を持つ。設問は slot ごとに別IDへ展開されるので、
#: 設問IDで持つとコンセプト比較が slot の数だけに割れる（§8）。
AGGREGATES_SCHEMA = (
    "survey_id string, stimulus_id string, measure string, "
    "segment string, segment_value string, n int, n_unflagged int, "
    "metric string, option_code int, option_label string, value double"
)

#: `metric` 列に入る値。
METRIC_OPTION = "option"
METRIC_TOP_BOX = "t2b"
METRIC_MEAN = "mean"

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
    """集計して `aggregates` とファイル一式を書く。

    書き出す先は `output.formats`（`delta` / `csv` / `xlsx`）で決まる。`formats` を
    明示すると調査定義より優先する（`export --xlsx` が使う）。
    """
    from persona_sim.aggregate.export import FORMAT_DELTA, write_outputs

    selected = tuple(formats if formats is not None else survey.output.formats)
    answers = frame.load_answers(spark, survey, storage)
    panel_rows = frame.load_panel_rows(spark, survey, storage)
    result = build_result(survey, answers, panel_rows)

    if FORMAT_DELTA in selected:
        _write_aggregates(spark, survey, storage, result)

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
    tables = [crosstab_table(table) for table in result.crosstabs]
    tables.append(
        concept_summary_table(
            survey,
            result.topline,
            key="concept_summary",
            title="コンセプト比較（全体）",
            with_segment=False,
        )
    )
    tables.append(
        concept_summary_table(
            survey,
            result.by_segment,
            key="concept_summary_by_segment",
            title="コンセプト比較（セグメント別）",
            with_segment=True,
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


# --------------------------------------------------------------------------- #
# `aggregates` テーブル（§2.5）
# --------------------------------------------------------------------------- #


def aggregate_rows(result: AggregateResult) -> list[tuple]:
    """`aggregates` に書くロング形式の行。表示用の整形をしない生の値。"""
    rows: list[tuple] = []
    for table in result.crosstabs:
        for row in table.rows:
            metric = row.metric
            common = (
                result.survey_id,
                table.stimulus_id,
                table.measure,
                row.segment,
                row.segment_value,
                metric.n,
                metric.n_unflagged,
            )
            for index, value in enumerate(metric.percentages, start=1):
                label = (
                    table.option_labels[index - 1]
                    if index <= len(table.option_labels)
                    else None
                )
                rows.append((*common, METRIC_OPTION, index, label, float(value)))
            if metric.top_box is not None:
                rows.append((*common, METRIC_TOP_BOX, None, None, float(metric.top_box)))
            if metric.mean is not None:
                rows.append((*common, METRIC_MEAN, None, None, float(metric.mean)))
    return rows


def _write_aggregates(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    result: AggregateResult,
) -> None:
    """`aggregates` を書き直す。再集計で行が二重にならないよう、先に消してから足す。"""
    aggregates_locator = locator(AGGREGATES, storage)
    delta.delete_survey_rows(spark, aggregates_locator, survey.survey_id)
    rows = aggregate_rows(result)
    if not rows:
        return
    delta.write_table(
        spark.createDataFrame(rows, AGGREGATES_SCHEMA), aggregates_locator, mode="append"
    )


def read_aggregates(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> list[dict[str, Any]]:
    """`aggregates` から読み直す（`export` が集計をやり直さないため）。"""
    from pyspark.sql import functions as F

    aggregates_locator = locator(AGGREGATES, storage)
    if not delta.table_exists(spark, aggregates_locator):
        return []
    return [
        row.asDict()
        for row in delta.read_table(spark, aggregates_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .collect()
    ]


def result_from_aggregates(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> AggregateResult:
    """`aggregates` から集計結果を組み直す（`export` 用）。

    `responses` を読み直さずにファイルだけ作り直せる。ロング形式の行から表を戻せることが、
    `aggregates` に必要な情報が揃っている証拠にもなる。
    """
    rows = read_aggregates(spark, survey, storage)
    if not rows:
        raise PersonaSimError(
            f"survey_id={survey.survey_id} の集計結果が無い。先に `persona-sim aggregate` を実行すること"
        )

    result = AggregateResult(survey_id=survey.survey_id)
    result.crosstabs = _crosstabs_from_rows(survey, rows)
    result.topline = _concept_rows(survey, result.crosstabs, TOPLINE_SEGMENTS)
    result.by_segment = _concept_rows(survey, result.crosstabs, _segments(survey))
    result.tables = _build_tables(survey, frame.load_panel_rows(spark, survey, storage), result)
    return result


def _crosstabs_from_rows(survey: SurveyDefinition, rows: Sequence[dict[str, Any]]) -> list[Crosstab]:
    names = {stimulus.id: stimulus.name for stimulus in survey.stimuli}

    # (stimulus_id, measure) → (segment, segment_value) → 集計値の材料
    grouped: dict[tuple[str, str], dict[tuple[str, str], dict[str, Any]]] = {}
    for row in rows:
        key = (row["stimulus_id"], row["measure"])
        segment_key = (row["segment"], row["segment_value"])
        bucket = grouped.setdefault(key, {}).setdefault(
            segment_key,
            {"n": row["n"], "n_unflagged": row["n_unflagged"], "options": {}, "t2b": None, "mean": None},
        )
        metric = row["metric"]
        if metric == METRIC_OPTION and row["option_code"] is not None:
            bucket["options"][int(row["option_code"])] = float(row["value"])
        elif metric == METRIC_TOP_BOX:
            bucket["t2b"] = float(row["value"])
        elif metric == METRIC_MEAN:
            bucket["mean"] = float(row["value"])

    tables: list[Crosstab] = []
    for stimulus in survey.stimuli:
        for question in tabulated_measures(survey):
            buckets = grouped.get((stimulus.id, question.measure_key))
            if not buckets:
                continue
            count = len(question.options)
            crosstab_rows = [
                CrosstabRow(
                    segment=segment,
                    segment_value=value,
                    metric=Metric(
                        n=int(bucket["n"]),
                        n_unflagged=int(bucket["n_unflagged"]),
                        percentages=tuple(
                            bucket["options"].get(index, 0.0) for index in range(1, count + 1)
                        ),
                        top_box=bucket["t2b"],
                        mean=bucket["mean"],
                    ),
                )
                for (segment, value), bucket in sorted(buckets.items())
            ]
            tables.append(
                Crosstab(
                    stimulus_id=stimulus.id,
                    stimulus_name=names.get(stimulus.id, stimulus.id),
                    measure=question.measure_key,
                    question_text=question.text,
                    question_type=question.type,
                    option_labels=tuple(question.options),
                    rows=tuple(crosstab_rows),
                )
            )
    return tables


def _concept_rows(
    survey: SurveyDefinition, tables: Sequence[Crosstab], wanted: Sequence[str]
) -> list[ConceptSummaryRow]:
    """クロス集計表からコンセプト比較表を組み直す（§7.2 は §7.1 の指標の横持ち）。"""
    names = {stimulus.id: stimulus.name for stimulus in survey.stimuli}
    collected: dict[tuple[str, str, str], dict[str, Any]] = {}

    for table in tables:
        for row in table.rows:
            if row.segment not in wanted:
                continue
            key = (table.stimulus_id, row.segment, row.segment_value)
            entry = collected.setdefault(key, {"n": 0, "n_unflagged": 0, "metrics": {}})
            entry["n"] = max(entry["n"], row.metric.n)
            entry["n_unflagged"] = max(entry["n_unflagged"], row.metric.n_unflagged)
            entry["metrics"][table.measure] = (row.metric.top_box, row.metric.mean)

    return [
        ConceptSummaryRow(
            stimulus_id=stimulus_id,
            stimulus_name=names.get(stimulus_id, stimulus_id),
            segment=segment,
            segment_value=value,
            n=entry["n"],
            n_unflagged=entry["n_unflagged"],
            metrics=entry["metrics"],
        )
        for (stimulus_id, segment, value), entry in collected.items()
    ]
