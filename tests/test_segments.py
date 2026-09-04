"""集計軸の解決（`persona_sim.aggregate.segments`）。"""

from __future__ import annotations

import pytest

from persona_sim.aggregate import segments
from persona_sim.errors import SurveyDefinitionError


def test_total_has_no_parts():
    assert segments.parts("total") == ()
    assert segments.value_of("total", {"sex": "男"}) == "全体"


def test_single_axis():
    assert segments.parts("age_band_10") == ("age_band_10",)
    assert segments.value_of("age_band_10", {"age_band_10": "30代"}) == "30代"


def test_composite_axis_joins_values():
    assert segments.parts("sex_x_age_band_10") == ("sex", "age_band_10")
    value = segments.value_of("sex_x_age_band_10", {"sex": "女", "age_band_10": "40代"})
    assert value == "女 × 40代"


def test_cell_id_is_available_as_an_axis():
    assert segments.unknown_axes("cell_id") == ()


def test_unknown_axis_is_reported():
    assert segments.unknown_axes("sex_x_favourite_colour") == ("favourite_colour",)


def test_validate_segments_rejects_typos():
    """綴り違いを黙って捨てると、軸が消えたことに実行後まで気づけない。"""
    with pytest.raises(SurveyDefinitionError, match="age_bands_10"):
        segments.validate_segments(["total", "age_bands_10"])


def test_validate_segments_accepts_the_sample_survey_axes():
    segments.validate_segments(
        ["total", "sex", "age_band_10", "sex_x_age_band_10", "cell_id"]
    )


def test_required_columns_deduplicates_and_keeps_order():
    columns = segments.required_columns(["total", "sex", "sex_x_age_band_10"])
    assert columns == ("sex", "age_band_10")


def test_missing_value_is_explicit():
    assert segments.value_of("sex", {"sex": None}) == "(不明)"
    assert segments.value_of("sex", {}) == "(不明)"
    assert segments.value_of("sex", {"sex": ""}) == "(不明)"
