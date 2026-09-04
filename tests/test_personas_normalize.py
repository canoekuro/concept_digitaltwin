"""`persona_sim.personas.normalize` の純 Python 実装の単体テスト（Spark 不要）。

根拠は `docs/schema/occupation-parsing.md`（実データ 125,000 行での検証結果）。
"""

import pytest

from persona_sim.personas.normalize import (
    age_band_5,
    age_band_10,
    parse_occupation,
)


@pytest.mark.parametrize(
    ("value", "industry", "scale", "role", "employment"),
    [
        ("介護福祉業 中堅", "介護福祉業", "中堅", None, "就業中"),
        ("介護福祉業 中堅 (現在は引退)", "介護福祉業", "中堅", None, "引退"),
        ("小売業 中小 経営 (現在は引退)", "小売業", "中小", "経営", "引退"),
        ("地方公務員", "地方公務員", None, None, "就業中"),
        ("農業", "農業", None, None, "就業中"),
        ("学生", "学生", None, None, "就業中"),
        ("卸売業 大手 (現在は離職)", "卸売業", "大手", None, "離職"),
        ("建設業 大手 経営", "建設業", "大手", "経営", "就業中"),
    ],
)
def test_parse_occupation(value, industry, scale, role, employment):
    result = parse_occupation(value)
    assert result.industry == industry
    assert result.scale == scale
    assert result.role == role
    assert result.employment_status == employment


def test_parse_occupation_none():
    result = parse_occupation(None)
    assert result.industry == ""
    assert result.scale is None
    assert result.role is None
    assert result.employment_status == "就業中"


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (18, "15-19"),
        (20, "20-24"),
        (23, "20-24"),
        (29, "25-29"),
        (100, "100歳以上"),
    ],
)
def test_age_band_5(age, expected):
    assert age_band_5(age) == expected


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (19, "10代"),
        (20, "20代"),
        (67, "60代"),
        (100, "100歳以上"),
    ],
)
def test_age_band_10(age, expected):
    assert age_band_10(age) == expected
