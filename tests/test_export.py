"""表の整形とファイル出力（`SPEC_PHASE1.md` §7.1・§7.4）。

Spark を起動せずに、表の見た目と xlsx の構造だけを見る。
"""

from __future__ import annotations

import csv

import pytest

from persona_sim.aggregate.aggregate import (
    METRIC_MEAN,
    METRIC_OPTION,
    METRIC_TOP_BOX,
    AggregateResult,
    aggregate_rows,
)
from persona_sim.aggregate.crosstab import Answer, concept_summary, crosstabs
from persona_sim.aggregate.export import (
    CSV_ENCODING,
    RAW_SHEET,
    SUMMARY_SHEET,
    write_csv,
    write_workbook,
)
from persona_sim.aggregate.tables import concept_axis_table, stacked_crosstab_table
from persona_sim.panel.loader import survey_from_dict

openpyxl = pytest.importorskip("openpyxl")


def _answers() -> list[Answer]:
    return [
        Answer(
            persona_uuid=f"p{index}",
            stimulus_id="c1",
            question_id="q_intent",
            codes=(code,),
            attributes={"sex": sex},
        )
        for index, (code, sex) in enumerate([(1, "男"), (2, "男"), (5, "女"), (1, "女")])
    ]


def _result(survey) -> AggregateResult:
    answers = _answers()
    result = AggregateResult(survey_id=survey.survey_id, answers=len(answers))
    result.crosstabs = crosstabs(survey, answers, ["total", "sex"])
    result.topline = concept_summary(survey, answers, ["total"])
    result.by_segment = concept_summary(survey, answers, ["total", "sex"])
    result.tables = [
        concept_axis_table(
            survey,
            result.crosstabs,
            "q_intent",
            key="crosstab_q_intent",
            title="購入意向",
            with_segment=True,
        ),
        stacked_crosstab_table(
            survey, result.crosstabs, key="crosstab_all", title="クロス集計表"
        ),
    ]
    return result


# --------------------------------------------------------------------------- #
# 表の整形
# --------------------------------------------------------------------------- #


def test_the_table_formats_percentages_and_means(survey_dict):
    survey = survey_from_dict(survey_dict)
    table = _concept_axis(survey, segments=("total",))
    total = next(row for row in table.rows if row[0] == "コンセプトA")

    assert total[1] == 4  # n
    assert total[3] == "50.0%"  # 「ぜひ」を2/4人
    assert total[-2] == "75.0%"  # T2B = 1と2 = 3/4人
    assert total[-1] == "3.75"  # (5+4+1+5)/4


def test_blank_when_a_metric_does_not_apply(survey_dict):
    """順序尺度でない設問の平均は `-`。空欄にすると0と読み違える。"""
    survey_dict["questions"][0].pop("top_box")
    survey = survey_from_dict(survey_dict)
    table = _concept_axis(survey, segments=("total",))
    total = next(row for row in table.rows if row[0] == "コンセプトA")
    assert total[-1] == "-"
    assert total[-2] == "-"


# --------------------------------------------------------------------------- #
# コンセプトを表側にした表（`docs/issues/20260805002.md`）
# --------------------------------------------------------------------------- #


def _concept_axis(survey, **kwargs):
    return concept_axis_table(
        survey,
        _result(survey).crosstabs,
        "q_intent",
        key="concept_axis_q_intent",
        title="購入意向",
        **kwargs,
    )


def test_concept_axis_table_puts_concepts_down_the_side(survey_dict):
    survey = survey_from_dict(survey_dict)
    table = _concept_axis(survey, segments=("total",))

    assert table.columns[:3] == ("コンセプト", "n", "n(フラグ除外後)")
    assert table.columns[3] == "1. ぜひ"
    assert table.columns[-2:] == ("T2B", "平均")
    # 回答があるのは c1 だけだが、定義にある3コンセプトぶんの行が全体1行ずつ出る。
    assert [row[0] for row in table.rows] == ["コンセプトA", "コンセプトB", "コンセプトC"]


def test_the_two_table_shapes_agree(survey_dict):
    """同じ数字が2通りに出ないこと。並べ替えているだけで集計はやり直していない。"""
    survey = survey_from_dict(survey_dict)
    concept = next(row for row in _concept_axis(survey, segments=("total",)).rows
                   if row[0] == "コンセプトA")
    stacked = stacked_crosstab_table(
        survey, _result(survey).crosstabs, key="k", title="t"
    )
    piled = next(
        row for row in stacked.rows
        if row[0] == "q_intent" and row[1] == "コンセプトA" and row[3] == "全体"
    )

    # concept: [コンセプト, n, n(除外後), 選択肢…, T2B, 平均]
    # piled:   [設問, コンセプト, 軸, セグメント, n, n(除外後), 選択肢…, T2B, 平均]
    assert concept[1:3] == piled[4:6]
    assert concept[-2:] == piled[-2:]


def test_concept_axis_table_can_carry_the_segment_axis(survey_dict):
    survey = survey_from_dict(survey_dict)
    table = _concept_axis(survey, with_segment=True)

    assert table.columns[:4] == ("コンセプト", "軸", "セグメント", "n")
    assert {row[2] for row in table.rows} >= {"全体", "男", "女"}


def test_concept_axis_table_orders_concepts_by_the_definition(survey_dict):
    """並び順は調査定義が持つもの。集計結果の入れ物の都合で変わってはいけない。"""
    survey = survey_from_dict(survey_dict)
    shuffled = list(reversed(_result(survey).crosstabs))
    table = concept_axis_table(
        survey, shuffled, "q_intent", key="k", title="t", segments=("total",)
    )
    assert [row[0] for row in table.rows] == ["コンセプトA", "コンセプトB", "コンセプトC"]


def test_a_concept_without_answers_stays_in_the_table_as_zero(survey_dict):
    """黙って消すと、実査に出したのに出てこないのか出していないのかが読めない。"""
    survey = survey_from_dict(survey_dict)
    table = _concept_axis(survey, segments=("total",))

    # 回答があるのは c1 だけ。c2 / c3 も n=0 の行として残る。
    empty = next(row for row in table.rows if row[0] == "コンセプトB")
    assert empty[1] == 0
    assert empty[-1] == "-"
    assert any("コンセプトB" in note and "n=0" in note for note in table.notes)


def test_stacked_table_holds_every_question_with_a_question_column(survey_dict):
    """UI の Excel は1シート。設問列を足して縦に積む。"""
    survey_dict["questions"].append(
        {
            "id": "q_novelty",
            "text": "目新しいと思いますか。",
            "type": "single",
            "options": ["とても", "やや", "ふつう"],
            "top_box": [1],
        }
    )
    survey = survey_from_dict(survey_dict)
    table = stacked_crosstab_table(survey, _result(survey).crosstabs, key="k", title="t")

    assert table.columns[:4] == ("設問", "コンセプト", "軸", "セグメント")
    assert {row[0] for row in table.rows} == {"q_intent", "q_novelty"}
    # 選択肢列は最も多い設問（5択）に合わせる。ラベルは設問ごとに違うので番号だけ。
    assert [c for c in table.columns if c.startswith("選択肢")] == [
        f"選択肢{i}" for i in range(1, 6)
    ]


def test_stacked_table_blanks_options_a_question_does_not_have(survey_dict):
    """3択の設問の4・5列目は空欄。0.0% と書くと「誰も選ばなかった」に読める。"""
    survey_dict["questions"].append(
        {
            "id": "q_novelty",
            "text": "目新しいと思いますか。",
            "type": "single",
            "options": ["とても", "やや", "ふつう"],
            "top_box": [1],
        }
    )
    survey = survey_from_dict(survey_dict)
    table = stacked_crosstab_table(survey, _result(survey).crosstabs, key="k", title="t")

    novelty = next(row for row in table.rows if row[0] == "q_novelty")
    assert novelty[-3] == "-"  # 選択肢5
    assert novelty[-4] == "-"  # 選択肢4


def test_stacked_table_notes_carry_the_option_legend(survey_dict):
    """選択肢列が番号だけなので、ラベルは凡例で示さないと読めない。"""
    survey = survey_from_dict(survey_dict)
    table = stacked_crosstab_table(survey, _result(survey).crosstabs, key="k", title="t")
    legend = next(note for note in table.notes if note.startswith("q_intent の選択肢"))
    assert "1. ぜひ" in legend
    assert "5. まったく" in legend


# --------------------------------------------------------------------------- #
# `aggregates` のロング行（§2.5）
# --------------------------------------------------------------------------- #


def test_aggregate_rows_hold_raw_values(survey_dict):
    survey = survey_from_dict(survey_dict)
    rows = aggregate_rows(_result(survey))
    columns = (
        "survey_id stimulus_id question_id segment segment_value n n_unflagged "
        "metric option_code option_label value"
    ).split()
    keyed = [dict(zip(columns, row, strict=True)) for row in rows]

    total_options = [
        row
        for row in keyed
        if row["metric"] == METRIC_OPTION
        and row["segment"] == "total"
        and row["stimulus_id"] == "c1"
    ]
    assert len(total_options) == 5
    assert total_options[0]["value"] == pytest.approx(0.5)
    assert total_options[0]["option_label"] == "ぜひ"

    top_box = next(
        row for row in keyed if row["metric"] == METRIC_TOP_BOX and row["segment"] == "total"
    )
    assert top_box["value"] == pytest.approx(0.75)
    mean = next(row for row in keyed if row["metric"] == METRIC_MEAN and row["segment"] == "total")
    assert mean["value"] == pytest.approx(3.75)


def test_aggregate_rows_cover_every_segment(survey_dict):
    survey = survey_from_dict(survey_dict)
    rows = aggregate_rows(_result(survey))
    segments = {row[3] for row in rows}
    values = {row[4] for row in rows}
    assert segments == {"total", "sex"}
    assert {"全体", "男", "女"} <= values


# --------------------------------------------------------------------------- #
# ファイル出力
# --------------------------------------------------------------------------- #


def test_csv_is_written_with_a_bom_so_excel_reads_japanese(tmp_path):
    path = write_csv(tmp_path / "t.csv", ("軸", "n"), [["全体", 4]])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    with open(path, encoding=CSV_ENCODING, newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [["軸", "n"], ["全体", "4"]]


def test_xlsx_has_a_summary_sheet_first_then_one_sheet_per_table(tmp_path, survey_dict):
    survey = survey_from_dict(survey_dict)
    result = _result(survey)
    path = write_workbook(tmp_path / "report.xlsx", survey, result.tables)

    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames[0] == SUMMARY_SHEET
    assert len(workbook.sheetnames) == 1 + len(result.tables)
    assert "crosstab_all" in workbook.sheetnames

    summary = workbook[SUMMARY_SHEET]
    assert summary["A1"].value == "調査ID"
    assert summary["B1"].value == survey.survey_id


def test_xlsx_sheet_names_stay_within_the_excel_limit(tmp_path, survey_dict):
    survey = survey_from_dict(survey_dict)
    result = _result(survey)
    for table in result.tables:
        table.key = "crosstab_" + "x" * 40

    workbook = openpyxl.load_workbook(
        write_workbook(tmp_path / "r.xlsx", survey, result.tables)
    )
    names = workbook.sheetnames
    assert all(len(name) <= 31 for name in names)
    assert len(set(names)) == len(names)


def test_notes_reach_the_sheet(tmp_path, survey_dict):
    survey = survey_from_dict(survey_dict)
    result = _result(survey)

    workbook = openpyxl.load_workbook(
        write_workbook(tmp_path / "r.xlsx", survey, result.tables, notes=["パース失敗が多い"])
    )
    body = [row[0] for row in workbook[SUMMARY_SHEET].iter_rows(values_only=True)]
    assert "パース失敗が多い" in body


def test_the_summary_sheet_carries_the_attribution_and_the_disclaimer(tmp_path, survey_dict):
    """帰属表示（§15.3）と免責（§15.1）は**配られるファイル側**に載せる。

    画面にしか出していないと、表だけ Excel で配られたときに両方とも落ちる。
    """
    survey = survey_from_dict(survey_dict)
    workbook = openpyxl.load_workbook(
        write_workbook(tmp_path / "r.xlsx", survey, _result(survey).tables)
    )
    body = "\n".join(
        str(row[0]) for row in workbook[SUMMARY_SHEET].iter_rows(values_only=True) if row[0]
    )

    assert "Nemotron-Personas-Japan" in body, "帰属表示（CC BY 4.0）が要る"
    assert "AIによるシミュレーション" in body, "免責が要る"


# --------------------------------------------------------------------------- #
# Web UI のダウンロード（`docs/SPEC_UI.md` §4.4）
#
# バッチの `report.xlsx` と同じ writer を通す。渡す表とローデータの有無が違うだけ。
# --------------------------------------------------------------------------- #


def _ui_workbook(tmp_path, survey, **kwargs):
    result = _result(survey)
    table = stacked_crosstab_table(survey, result.crosstabs, key="crosstab", title="クロス集計表")
    path = write_workbook(tmp_path / "ui.xlsx", survey, [table], **kwargs)
    return openpyxl.load_workbook(path)


def test_ui_workbook_carries_the_table_and_the_raw_data(tmp_path, survey_dict):
    """`st.download_button` は1ファイルしか返せないので同梱する。"""
    survey = survey_from_dict(survey_dict)
    workbook = _ui_workbook(
        tmp_path,
        survey,
        raw_columns=("persona_uuid", "stimulus_name"),
        raw_rows=[["u1", "コンセプトA"]],
    )

    assert workbook.sheetnames == [SUMMARY_SHEET, "crosstab", RAW_SHEET]
    raw = list(workbook[RAW_SHEET].iter_rows(values_only=True))
    assert raw[0] == ("persona_uuid", "stimulus_name")
    assert raw[1] == ("u1", "コンセプトA")


def test_ui_workbook_keeps_the_notes_on_the_first_sheet(tmp_path, survey_dict):
    """E3/E4 の注記は落とさない（`SPEC_PHASE1.md` §11）。表だけ読まれて終わらせない。"""
    survey = survey_from_dict(survey_dict)
    workbook = _ui_workbook(tmp_path, survey, notes=["品質フラグの立った回答が 30% ある"])

    body = [row[0] for row in workbook[SUMMARY_SHEET].iter_rows(values_only=True)]
    assert "品質フラグの立った回答が 30% ある" in body
    # 1枚目に載っていること。表のシートより後ろに置くと読まれずに終わる。
    assert workbook.sheetnames[0] == SUMMARY_SHEET


def test_ui_workbook_without_raw_data_omits_the_raw_sheet(tmp_path, survey_dict):
    survey = survey_from_dict(survey_dict)
    workbook = _ui_workbook(tmp_path, survey)
    assert RAW_SHEET not in workbook.sheetnames


def test_every_table_row_has_one_value_per_column(survey_dict):
    """`build_result()` が返す全ての表で、各行の長さが columns と一致する。

    `cli._print_table()` は `zip(..., strict=True)` で列と値を突き合わせるので、
    ずれた表があると表示時に `ValueError` で落ちる。守るべきなのは表示側ではなく
    **生成側**なので、ここで固定する。
    """
    from persona_sim.aggregate.aggregate import build_result

    survey = survey_from_dict(survey_dict)
    result = build_result(
        survey,
        _answers(),
        [{"cell_id": "M_20s", "role": "main", "weight": 1.0}],
    )

    assert result.tables
    for table in result.tables:
        assert table.columns
        for index, row in enumerate(table.rows):
            assert len(row) == len(table.columns), f"{table.key}: {index} 行目の長さが違う"
