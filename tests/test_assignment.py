"""コンセプト割り当て（`persona_sim.panel.assignment`）。

`AGENTS.md` の不変条件2つを守っていることを確認する。

- 同一ペルソナは同一コンセプトに1回しか回答しない → 割り当てに重複が無い
- `sample_overlap: disjoint` のセル内均等割り当てを省略しない → 評価者数が均等
"""

from __future__ import annotations

from collections import Counter

import pytest

from persona_sim.panel.assignment import assignment_table
from persona_sim.panel.schema import Design, Rotation, SampleOverlap

IDS = ("c1", "c2", "c3", "c4")


def _table(**kwargs) -> tuple[tuple[str, ...], ...]:
    return assignment_table(Design(**kwargs), IDS)


@pytest.mark.parametrize(
    "design_kwargs",
    [
        {"sample_overlap": SampleOverlap.SAME},
        {"sample_overlap": SampleOverlap.SAME, "rotation": Rotation.BALANCED},
        {"sample_overlap": SampleOverlap.DISJOINT},
        {"sample_overlap": SampleOverlap.ALLOW_OVERLAP, "stimuli_per_persona": 2},
        {"sample_overlap": SampleOverlap.ALLOW_OVERLAP, "stimuli_per_persona": 3},
    ],
)
def test_no_row_contains_a_duplicate(design_kwargs):
    """どの設定でも、同じ人に同じコンセプトが2回当たらない（§5.0）。"""
    for row in _table(**design_kwargs):
        assert len(row) == len(set(row))


def test_same_assigns_every_stimulus():
    for row in _table(sample_overlap=SampleOverlap.SAME):
        assert set(row) == set(IDS)


def test_same_without_rotation_keeps_definition_order():
    for row in _table(sample_overlap=SampleOverlap.SAME, rotation=Rotation.NONE):
        assert row == IDS


def test_balanced_rotation_is_a_latin_square():
    """各提示位置に各コンセプトがちょうど1回ずつ現れる（順序効果の相殺）。"""
    table = _table(sample_overlap=SampleOverlap.SAME, rotation=Rotation.BALANCED)
    assert len(table) == len(IDS)
    for position in range(len(IDS)):
        assert sorted(row[position] for row in table) == sorted(IDS)


def test_disjoint_assigns_exactly_one():
    table = _table(sample_overlap=SampleOverlap.DISJOINT)
    assert all(len(row) == 1 for row in table)


def test_disjoint_is_balanced_round_robin():
    """セル内順位で巡回するので、各コンセプトの評価者数が均等になる。"""
    table = _table(sample_overlap=SampleOverlap.DISJOINT)
    counts = Counter(row[0] for row in table)
    assert set(counts) == set(IDS)
    assert max(counts.values()) - min(counts.values()) == 0


@pytest.mark.parametrize("per_persona", [2, 3])
def test_allow_overlap_is_balanced(per_persona):
    """m 件ずつ巡回して配るので、評価者数の差は最大1に収まる。"""
    table = _table(
        sample_overlap=SampleOverlap.ALLOW_OVERLAP, stimuli_per_persona=per_persona
    )
    assert all(len(row) == per_persona for row in table)
    counts = Counter(stimulus for row in table for stimulus in row)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert sum(counts.values()) == len(IDS) * per_persona


def test_table_length_equals_stimuli_count():
    """割り当ては cell_rank % K だけで決まるので K 通りしかない。"""
    assert len(_table(sample_overlap=SampleOverlap.SAME)) == len(IDS)


def test_rotation_random_returns_canonical_set():
    """random の並べ替えはペルソナ単位で行うため、表は定義順に揃えておく。"""
    table = _table(sample_overlap=SampleOverlap.ALLOW_OVERLAP, stimuli_per_persona=2,
                   rotation=Rotation.RANDOM)
    for row in table:
        assert list(row) == sorted(row, key=IDS.index)


def test_empty_stimuli_is_rejected():
    with pytest.raises(ValueError):
        assignment_table(Design(), [])
