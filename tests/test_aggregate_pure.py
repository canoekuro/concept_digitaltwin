"""Spark を使わない集計経路（`persona_sim.aggregate`）。

CLI は Spark で、Web UI は SQL Warehouse で行を読む。**読み方が違っても同じ数字になる**
必要があるので、両者が通る継ぎ目（`answers_from_rows` / `build_result`）をここで固める。
Spark を起動しないため、集計の意味が変わる変更はこのファイルが最初に落ちる。
"""

from __future__ import annotations

import pytest

from persona_sim.aggregate.aggregate import build_result, panel_composition_table_from_rows
from persona_sim.aggregate.frame import answers_from_rows
from persona_sim.panel.loader import survey_from_dict
from tests.conftest import base_survey_dict


def _survey_dict() -> dict:
    data = base_survey_dict()
    data["output"]["segments"] = ["total", "sex"]
    return data


@pytest.fixture
def survey():
    return survey_from_dict(_survey_dict())


def _row(**overrides) -> dict:
    """`responses ⋈ panels ⋈ personas_base` の1行。"""
    row = {
        "persona_uuid": "u1",
        "stimulus_id": "c1",
        "question_id": "q_intent_1",
        "answer_codes": [1],
        "options_order": [1, 2, 3, 4, 5],
        "weight": 1.0,
        "flags": [],
        "answer_text": None,
        "sex": "女",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# 行 → Answer
# --------------------------------------------------------------------------- #


def test_codes_are_mapped_back_to_definition_order(survey):
    """提示順の番号を定義順に戻す。飛ばすとシャッフルした設問で意味が変わる（§2.3）。"""
    answers = answers_from_rows([_row(answer_codes=[1], options_order=[3, 1, 2, 4, 5])], survey)
    assert answers[0].codes == (3,)


def test_missing_weight_becomes_one(survey):
    answers = answers_from_rows([_row(weight=None)], survey)
    assert answers[0].weight == 1.0


def test_quality_flags_mark_the_answer(survey):
    flagged = answers_from_rows([_row(flags=["parse_error"])], survey)[0]
    clean = answers_from_rows([_row(flags=["inferred"])], survey)[0]
    assert flagged.flagged is True
    # inferred は品質フラグではない（判定の由来を示すだけ）
    assert clean.flagged is False


def test_attributes_are_limited_to_the_segment_axes(survey):
    answers = answers_from_rows([_row(sex="女")], survey)
    assert answers[0].attributes == {"sex": "女"}


def test_rows_for_questions_outside_the_definition_are_dropped(survey):
    """調査定義から消えた設問の過去レコードを黙って混ぜない。"""
    answers = answers_from_rows([_row(), _row(question_id="q_removed")], survey)
    assert len(answers) == 1


def test_open_text_is_carried_through(survey):
    answers = answers_from_rows(
        [_row(question_id="q_reason_1", answer_codes=[], options_order=[], answer_text="安いから")],
        survey,
    )
    assert answers[0].text == "安いから"
    assert answers[0].codes == ()


# --------------------------------------------------------------------------- #
# 集計結果の組み立て
# --------------------------------------------------------------------------- #


def test_build_result_produces_every_table(survey):
    rows = [
        _row(persona_uuid="u1", answer_codes=[1]),
        _row(persona_uuid="u2", answer_codes=[2], sex="男"),
        _row(persona_uuid="u1", stimulus_id="c2", answer_codes=[5]),
    ]
    panel_rows = [
        {"cell_id": "M_20s", "role": "main", "weight": 1.0},
        {"cell_id": "F_20s", "role": "main", "weight": 1.0},
    ]
    result = build_result(survey, answers_from_rows(rows, survey), panel_rows)

    assert result.survey_id == survey.survey_id
    assert result.answers == 3
    # コンセプト × 設問 ごとに1表（回答のある c1 / c2 × q_intent）
    assert {(c.stimulus_id, c.measure) for c in result.crosstabs} == {
        ("c1", "q_intent"),
        ("c2", "q_intent"),
        ("c3", "q_intent"),
    }
    keys = [table.key for table in result.tables]
    # measure ごとに1表 ＋ 全設問を積んだ表 ＋ パネル構成表。
    assert "crosstab_q_intent" in keys
    assert "crosstab_all" in keys
    assert "panel_composition" in keys


def test_build_result_segments_always_include_total(survey):
    """トップラインが無いとコンセプト間を比べられない。"""
    data = _survey_dict()
    data["output"]["segments"] = ["sex"]
    without_total = survey_from_dict(data)
    result = build_result(without_total, answers_from_rows([_row()], without_total), [])
    axes = {row.segment for table in result.crosstabs for row in table.rows}
    assert "total" in axes


def test_build_result_accepts_empty_panel_rows(survey):
    """パネル構成表を出さない経路（構成が要らない画面）でも落ちないこと。"""
    result = build_result(survey, answers_from_rows([_row()], survey), [])
    assert result.tables


def test_flag_rate_over_the_threshold_is_noted(survey):
    """閾値を超えたら止めずに表面化させる（§11 E3/E4）。"""
    rows = [_row(persona_uuid=f"u{i}", flags=["parse_error"]) for i in range(10)]
    result = build_result(survey, answers_from_rows(rows, survey), [])
    assert any("品質フラグ" in note for note in result.notes)


# --------------------------------------------------------------------------- #
# 定義と実データの食い違い
# --------------------------------------------------------------------------- #


def _male_only_survey():
    data = _survey_dict()
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20s", "sex": "男", "age_min": 20, "age_max": 29, "n": 4}
    ]
    return survey_from_dict(data)


def test_an_answer_outside_the_defined_sex_is_noted():
    """男性のみの割り付けなのに女性の回答が混じるのは、パネルと回答の対応が
    壊れているか、別の調査の結果を見ている。黙って集計しない。"""
    male_only = _male_only_survey()
    rows = [_row(persona_uuid="u1", sex="男"), _row(persona_uuid="u2", sex="女")]
    result = build_result(male_only, answers_from_rows(rows, male_only), [])
    assert any("女" in note and "割り付け" in note for note in result.notes)


def test_a_survey_that_matches_its_definition_is_not_noted():
    male_only = _male_only_survey()
    rows = [_row(persona_uuid="u1", sex="男"), _row(persona_uuid="u2", sex="男")]
    result = build_result(male_only, answers_from_rows(rows, male_only), [])
    assert result.notes == []


def test_a_cell_missing_from_the_definition_is_noted():
    data = _survey_dict()
    data["output"]["segments"] = ["total", "sex", "cell_id"]
    survey = survey_from_dict(data)
    rows = [_row(persona_uuid="u1", cell_id="M_20s"), _row(persona_uuid="u2", cell_id="X_99s")]
    result = build_result(survey, answers_from_rows(rows, survey), [])
    assert any("X_99s" in note for note in result.notes)


def test_cells_without_a_sex_condition_are_not_checked():
    """条件を書いていない以上、どの値も対象外ではない。"""
    data = _survey_dict()
    data["panel"]["quotas"]["cells"] = [{"cell_id": "all", "age_min": 20, "age_max": 29, "n": 4}]
    survey = survey_from_dict(data)
    result = build_result(survey, answers_from_rows([_row(sex="女")], survey), [])
    assert result.notes == []


# --------------------------------------------------------------------------- #
# パネル構成表
# --------------------------------------------------------------------------- #


def test_panel_composition_counts_roles(survey):
    panel_rows = [
        {"cell_id": "M_20s", "role": "main", "weight": 1.0},
        {"cell_id": "M_20s", "role": "main", "weight": 1.0},
        {"cell_id": "M_20s", "role": "screened_out", "weight": None},
        {"cell_id": "F_20s", "role": "main", "weight": 1.0},
    ]
    table = panel_composition_table_from_rows(survey, panel_rows)
    by_cell = {row[0]: row for row in table.rows}
    assert by_cell["M_20s"][2] == 2  # 確定
    assert by_cell["M_20s"][4] == 1  # 非通過
    assert by_cell["F_20s"][2] == 1
