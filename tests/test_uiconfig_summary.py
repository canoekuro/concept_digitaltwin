"""対象者サマリー（`persona_sim.uiconfig.summary`）。

調査設計ページと結果閲覧ページが同じ文言で「誰に聞くのか」を出すための純関数。
Streamlit を起動せずに文言そのものを押さえる。
"""

from __future__ import annotations

from persona_sim.panel.loader import survey_from_dict
from persona_sim.uiconfig.summary import target_summary
from tests.conftest import base_survey_dict


def _survey(cells: list[dict], size: int = 500):
    built = base_survey_dict()
    built["panel"]["size"] = size
    built["panel"]["quotas"] = {"mode": "proportion", "cells": cells}
    return survey_from_dict(built)


def _cells(sex: str, bands: list[tuple[int, int]]) -> list[dict]:
    share = 1.0 / len(bands)
    return [
        {
            "cell_id": f"{sex}_{low}_{high}",
            "sex": sex,
            "age_min": low,
            "age_max": high,
            "proportion": share,
        }
        for low, high in bands
    ]


def test_a_single_sex_survey_says_so():
    survey = _survey(_cells("男", [(20, 29), (30, 39), (40, 49), (50, 59), (60, 69)]))
    assert target_summary(survey) == "男性 20〜69歳（5セル・計 500名）"


def test_each_sex_keeps_its_own_age_range():
    """性別ごとに違う年齢範囲を指定できる（`docs/SPEC_UI.md` §3.1）。"""
    cells = [
        *_cells("男", [(20, 29), (30, 39)]),
        *_cells("女", [(40, 49), (50, 59)]),
    ]
    summary = target_summary(_survey(cells))
    assert summary.startswith("男性 20〜39歳 / 女性 40〜59歳")
    assert "4セル" in summary


def test_the_order_follows_the_cells():
    cells = [*_cells("女", [(20, 29)]), *_cells("男", [(20, 29)])]
    assert target_summary(_survey(cells)).startswith("女性 20〜29歳 / 男性 20〜29歳")


def test_a_cell_without_a_sex_condition_is_not_shown_as_a_sex():
    cells = [{"cell_id": "all", "age_min": 20, "age_max": 69, "proportion": 1.0}]
    assert target_summary(_survey(cells)).startswith("性別指定なし 20〜69歳")


def test_a_cell_without_ages_says_so():
    cells = [{"cell_id": "m", "sex": "男", "proportion": 1.0}]
    assert target_summary(_survey(cells)).startswith("男性 年齢指定なし")
