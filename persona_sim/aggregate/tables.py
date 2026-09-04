"""集計結果の表への整形（`SPEC_PHASE1.md` §7.1・§7.2）。

`Table` は CSV・xlsx のどちらにも同じ形で流せる中間表現。**表示用の整形はここに集約する**。
仕様の表記（`12.3%` / `3.21`）にそろえて人が読む用に振る。機械可読な生の値が要るなら
`crosstab.Crosstab`（`compute_metric()` を通った値）を直接読むこと。

このモジュールは pyspark に依存しない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from persona_sim.aggregate import segments as segment_axes
from persona_sim.aggregate.crosstab import BLANK, Crosstab, tabulated_measures
from persona_sim.panel.schema import SurveyDefinition

#: 表側の見出し。
AXIS_COLUMN = "軸"
SEGMENT_COLUMN = "セグメント"
QUESTION_COLUMN = "設問"
CONCEPT_COLUMN = "コンセプト"
N_COLUMN = "n"
N_UNFLAGGED_COLUMN = "n(フラグ除外後)"

#: 表頭の末尾。
TOP_BOX_COLUMN = "T2B"
MEAN_COLUMN = "平均"


@dataclass
class Table:
    """1つの表。CSV1ファイル、または xlsx の1シートに対応する。"""

    #: ファイル名・シート名の元になる識別子。
    key: str
    #: 人が読む表題。
    title: str
    columns: tuple[str, ...]
    rows: list[list[object]] = field(default_factory=list)
    notes: tuple[str, ...] = ()


def format_percentage(value: float | None) -> str:
    return BLANK if value is None else f"{value:.1%}"


def format_mean(value: float | None) -> str:
    return BLANK if value is None else f"{value:.2f}"


def option_column(index: int, label: str) -> str:
    """選択肢の列見出し。番号は**定義順**（`to_defined_codes` を通したあと）。"""
    return f"{index}. {label}"


# --------------------------------------------------------------------------- #
# 集計表
#
# **集計はやり直さない。** 材料は `compute_metric()` を通った `Crosstab` の値そのままで、
# ここでは並べ替えと表示用の整形しかしない。表の形は2つだけ持つ——1 measure を
# 選択肢ラベルつきで読む `concept_axis_table()` と、全設問を1枚に積む
# `stacked_crosstab_table()`。同じ数字が何通りにも出る余地を作らないため。
# --------------------------------------------------------------------------- #


def concept_axis_table(
    survey: SurveyDefinition,
    crosstabs: Sequence[Crosstab],
    measure: str,
    *,
    key: str,
    title: str,
    segments: Sequence[str] | None = None,
    with_segment: bool = False,
) -> Table:
    """1つの measure について、コンセプトを表側・選択肢を表頭にした表。

    `segments` を渡すとその軸だけに絞る（`(total,)` なら全体1行ずつ）。渡さなければ
    `crosstabs` が持つ軸をすべて出す。`with_segment` で軸・セグメント列の有無を切り替える。

    束ねる単位が設問IDでなく measure なのは、同じ問いが slot ごとに別IDへ展開されるため
    （`tabulated_measures()` を参照）。
    """
    question = next(
        (q for q in survey.questions if q.measure_key == measure), None
    )
    labels = tuple(question.options) if question is not None else ()

    columns = [CONCEPT_COLUMN]
    if with_segment:
        columns.extend([AXIS_COLUMN, SEGMENT_COLUMN])
    columns.extend([N_COLUMN, N_UNFLAGGED_COLUMN])
    columns.extend(option_column(index, label) for index, label in enumerate(labels, start=1))
    columns.extend([TOP_BOX_COLUMN, MEAN_COLUMN])

    matched = _by_stimulus(crosstabs, measure)
    rows: list[list[object]] = []
    notes: list[str] = []
    seen_notes: set[str] = set()
    empty: list[str] = []

    for stimulus in survey.stimuli:
        table = matched.get(stimulus.id)
        selected = [
            row
            for row in (table.rows if table is not None else ())
            if segments is None or row.segment in segments
        ]
        if not selected:
            empty.append(stimulus.name)
            rows.append(_empty_row(stimulus.name, len(labels), with_segment=with_segment))
            continue
        for row in selected:
            values: list[object] = [stimulus.name]
            if with_segment:
                values.extend([segment_axes.label(row.segment), row.segment_value])
            values.extend(_metric_cells(row.metric, len(labels)))
            rows.append(values)
        for note in table.notes:
            if note not in seen_notes:
                seen_notes.add(note)
                notes.append(note)

    if question is not None:
        notes.insert(0, f"{measure}: {question.text}")
    if empty:
        notes.append(_empty_note(empty))
    return Table(key=key, title=title, columns=tuple(columns), rows=rows, notes=tuple(notes))


def stacked_crosstab_table(
    survey: SurveyDefinition,
    crosstabs: Sequence[Crosstab],
    *,
    key: str,
    title: str,
) -> Table:
    """全設問を1表に縦積みしたクロス集計表（コンセプト × セグメント）。

    設問ごとに選択肢ラベルが違うので、**選択肢列は番号だけ**に揃え、ラベルは
    `notes` の凡例に回す。列数は最も選択肢の多い設問に合わせ、足りないぶんは空欄にする。
    シートを1枚に収めるための形なので、ラベルを含む表が要るなら `concept_axis_table()` を使う。
    """
    questions = tabulated_measures(survey)
    width = max((len(question.options) for question in questions), default=0)

    columns = [
        QUESTION_COLUMN,
        CONCEPT_COLUMN,
        AXIS_COLUMN,
        SEGMENT_COLUMN,
        N_COLUMN,
        N_UNFLAGGED_COLUMN,
        *(f"選択肢{index}" for index in range(1, width + 1)),
        TOP_BOX_COLUMN,
        MEAN_COLUMN,
    ]

    rows: list[list[object]] = []
    notes: list[str] = []
    empty: list[str] = []
    for question in questions:
        notes.append(f"{question.measure_key}: {question.text}")
        if question.options:
            legend = " / ".join(
                option_column(index, label)
                for index, label in enumerate(question.options, start=1)
            )
            notes.append(f"{question.measure_key} の選択肢: {legend}")
        matched = _by_stimulus(crosstabs, question.measure_key)
        for stimulus in survey.stimuli:
            table = matched.get(stimulus.id)
            if table is None or not table.rows:
                empty.append(f"{stimulus.name} / {question.measure_key}")
                rows.append(
                    [question.measure_key, *_empty_row(stimulus.name, width, with_segment=True)]
                )
                continue
            for row in table.rows:
                rows.append(
                    [
                        question.measure_key,
                        stimulus.name,
                        segment_axes.label(row.segment),
                        row.segment_value,
                        *_metric_cells(row.metric, width),
                    ]
                )
    if empty:
        notes.append(_empty_note(empty))
    return Table(key=key, title=title, columns=tuple(columns), rows=rows, notes=tuple(notes))


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _by_stimulus(crosstabs: Sequence[Crosstab], measure: str) -> dict[str, Crosstab]:
    """その measure のクロス集計表を `stimulus_id` 引きにする。

    呼び出し側は `survey.stimuli` の順に引く。`crosstabs` の並びに頼らない――
    コンセプトの並び順は調査定義が持つもので、集計結果の入れ物の都合で変わってはいけない。
    """
    return {
        table.stimulus_id: table for table in crosstabs if table.measure == measure
    }


def _empty_row(stimulus_name: str, width: int, *, with_segment: bool) -> list[object]:
    """回答が1件も無いコンセプトの行。

    `group_by_segments` は回答からセグメント値を作るので、回答が無いコンセプトは
    行そのものが立たない。**黙って消すとコンセプトが表から消える**。実査に出したのに
    出てこないのか、そもそも出していないのかが読み分けられなくなるので、
    n=0 の行として残す（`SPEC_PHASE1.md` §11 と同じ扱い）。
    """
    values: list[object] = [stimulus_name]
    if with_segment:
        values.extend([BLANK, BLANK])
    values.extend([0, 0])
    values.extend(BLANK for _ in range(width))
    values.extend([BLANK, BLANK])
    return values


def _empty_note(empty: Sequence[str]) -> str:
    return f"回答が1件も無いため n=0 で出している: {'・'.join(empty)}"


def _metric_cells(metric, width: int) -> list[object]:
    """`n / n(フラグ除外後) / 各選択肢 / T2B / 平均` のセル。

    `width` に足りない選択肢は空欄（`BLANK`）で埋める。0.0% と書くと「誰も選ばなかった」に
    読めるが、そもそも存在しない選択肢なので別物。
    """
    percentages = list(metric.percentages)
    cells: list[object] = [metric.n, metric.n_unflagged]
    for index in range(width):
        cells.append(format_percentage(percentages[index]) if index < len(percentages) else BLANK)
    cells.append(format_percentage(metric.top_box))
    cells.append(format_mean(metric.mean))
    return cells
