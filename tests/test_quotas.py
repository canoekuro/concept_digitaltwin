"""割り付けの整数配分とウェイト（`persona_sim.panel.quotas`）。"""

from __future__ import annotations

from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.quotas import allocate_cell_sizes, cell_weights
from tests.conftest import base_survey_dict


def _proportion_survey(proportions: dict[str, float], size: int):
    data = base_survey_dict()
    data["panel"]["size"] = size
    data["panel"]["quotas"]["mode"] = "proportion"
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": cell_id, "proportion": value} for cell_id, value in proportions.items()
    ]
    return survey_from_dict(data)


def test_count_mode_uses_given_numbers(survey_dict):
    assert allocate_cell_sizes(survey_from_dict(survey_dict)) == {"M_20s": 2, "F_20s": 2}


def test_proportion_allocation_sums_to_panel_size():
    """端数が出ても合計は panel.size にちょうど一致する（最大剰余法）。"""
    survey = _proportion_survey({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, size=10)
    allocated = allocate_cell_sizes(survey)
    assert sum(allocated.values()) == 10
    assert sorted(allocated.values()) == [3, 3, 4]


def test_proportion_allocation_is_deterministic():
    """剰余が同点でも cell_id 昇順で決まるので、実行ごとにぶれない。"""
    survey = _proportion_survey({"c": 1 / 3, "a": 1 / 3, "b": 1 / 3}, size=10)
    first = allocate_cell_sizes(survey)
    assert first == allocate_cell_sizes(survey)
    assert first["a"] == 4  # 同点は cell_id 昇順で先頭が取る


def test_proportion_allocation_gives_largest_remainder_priority():
    """端数の大きいセルから順に1人ずつ配る。

    size=7 のとき理論値は a=3.50 / b=1.82 / c=1.68。切り捨てで 3/1/1（計5）となり、
    残り2人は剰余の大きい b(0.82) → c(0.68) の順に配られる。a(0.50) には回らない。
    """
    survey = _proportion_survey({"a": 0.5, "b": 0.26, "c": 0.24}, size=7)
    allocated = allocate_cell_sizes(survey)
    assert sum(allocated.values()) == 7
    assert allocated == {"a": 3, "b": 2, "c": 2}


def test_weights_are_one_when_quotas_are_filled(survey_dict):
    survey = survey_from_dict(survey_dict)
    weights = cell_weights(survey, {"M_20s": 2, "F_20s": 2})
    assert weights == {"M_20s": 1.0, "F_20s": 1.0}


def test_weights_correct_for_skewed_composition(survey_dict):
    """構成が崩れたセルは 目標比率 / 実比率 で補正する（§4.3）。"""
    survey = survey_from_dict(survey_dict)
    weights = cell_weights(survey, {"M_20s": 1, "F_20s": 3})
    assert weights["M_20s"] == 2.0  # 目標 0.5 / 実績 0.25
    assert weights["F_20s"] == 2 / 3  # 目標 0.5 / 実績 0.75


def test_weight_is_zero_for_empty_cell(survey_dict):
    survey = survey_from_dict(survey_dict)
    assert cell_weights(survey, {"M_20s": 4, "F_20s": 0})["F_20s"] == 0.0


def test_proportion_allocation_does_not_hand_out_extras_when_proportions_exceed_one():
    """比率の合計が1を超えても、切り捨てた人数より多くは配らない。

    切り捨ての時点で `panel.size` を超えていると「余りを配る」枚数が負になり、
    `order[:remaining]` が「末尾 n 個を除く全部」に反転して**余分に配ってしまう**。
    ここでは理論値が 4/4/4（計12 > size=10）なので、余りは1人も配られてはならない。

    合計1.0 は `validate._check_quotas()` が止めるが、`warehouse.fetch_survey()`
    （`runs.metadata_json` からの復元）は `validate_static()` を通さないので、
    この関数だけで守る必要がある。
    """
    survey = _proportion_survey({"a": 0.4, "b": 0.4, "c": 0.4}, size=10)
    assert allocate_cell_sizes(survey) == {"a": 4, "b": 4, "c": 4}
