"""ローデータの読める化（`persona_sim.aggregate.rawdata`）。

`responses` は `stimulus_id` と**提示順**の選択肢番号しか持たない。調査定義から
コンセプト名と選択肢ラベルを引いて足す部分だけを、Spark 抜きで見る。
"""

from __future__ import annotations

import pytest

from persona_sim.aggregate.rawdata import (
    ANSWER_LABELS_COLUMN,
    STIMULUS_NAME_COLUMN,
    labelled_csv_rows,
)
from persona_sim.panel.loader import survey_from_dict
from tests.conftest import base_survey_dict


@pytest.fixture
def survey():
    return survey_from_dict(base_survey_dict())


def _row(**overrides) -> dict:
    row = {
        "persona_uuid": "u1",
        "stimulus_id": "c1",
        "question_id": "q_intent_1",
        "answer_codes": [1],
        "options_order": [1, 2, 3, 4, 5],
        "flags": [],
        "sex": "女",
    }
    row.update(overrides)
    return row


def test_concept_name_sits_next_to_the_id(survey):
    columns, rows = labelled_csv_rows(survey, [_row()])
    assert columns.index(STIMULUS_NAME_COLUMN) == columns.index("stimulus_id") + 1
    assert rows[0][columns.index(STIMULUS_NAME_COLUMN)] == "コンセプトA"
    # 元の列は消さない。stimulus_id で突き合わせる用途が残る。
    assert rows[0][columns.index("stimulus_id")] == "c1"


def test_answers_are_shown_as_numbered_options(survey):
    columns, rows = labelled_csv_rows(survey, [_row(answer_codes=[2])])
    assert rows[0][columns.index(ANSWER_LABELS_COLUMN)] == "2. やや"


def test_labels_are_read_after_mapping_back_to_definition_order(survey):
    """提示順のまま引くと、シャッフルした設問で別の選択肢の名前が付く（§2.3）。"""
    columns, rows = labelled_csv_rows(
        survey, [_row(answer_codes=[1], options_order=[3, 1, 2, 4, 5])]
    )
    assert rows[0][columns.index(ANSWER_LABELS_COLUMN)] == "3. どちらとも"


def test_out_of_range_codes_keep_their_number(survey):
    """何が返ってきたか追えなくなるので、読めない番号も消さない。"""
    columns, rows = labelled_csv_rows(survey, [_row(answer_codes=[9])])
    assert rows[0][columns.index(ANSWER_LABELS_COLUMN)] == "9. (不明)"


def test_questions_without_options_get_an_empty_label(survey):
    columns, rows = labelled_csv_rows(
        survey, [_row(question_id="q_reason_1", answer_codes=[], options_order=[])]
    )
    assert rows[0][columns.index(ANSWER_LABELS_COLUMN)] == ""


def test_multiple_choices_are_joined(survey):
    columns, rows = labelled_csv_rows(survey, [_row(answer_codes=[1, 2])])
    assert rows[0][columns.index(ANSWER_LABELS_COLUMN)] == "1. ぜひ|2. やや"


def test_arrays_are_flattened_for_the_sheet(survey):
    columns, rows = labelled_csv_rows(survey, [_row(flags=["parse_error", "refusal"])])
    assert rows[0][columns.index("flags")] == "parse_error|refusal"


def test_an_unknown_concept_is_marked_not_blank(survey):
    """空欄にすると、コンセプトが付かなかったことに気づけない。"""
    columns, rows = labelled_csv_rows(survey, [_row(stimulus_id="zzz")])
    assert rows[0][columns.index(STIMULUS_NAME_COLUMN)] == "(不明)"


def test_nothing_to_write(survey):
    assert labelled_csv_rows(survey, []) == ((), [])
