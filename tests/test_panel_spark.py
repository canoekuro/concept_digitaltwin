"""パネル構築の結合テスト（Spark が必要）。

重点は `AGENTS.md` の不変条件が実際の分散実行で守られること。

- seed と各バージョンが揃えば同一結果が再現できる
- **並列度を変えても結果が変わらない**（乱数サンプリングを使っていないことの証明）
- 同一ペルソナがパネルに2回現れない
- 全ペルソナが全コンセプトを定義順に評価する（反実仮想モナディック固定・§5）
"""

from __future__ import annotations

from collections import Counter

import pytest

from persona_sim.config import StorageConfig
from persona_sim.errors import InsufficientCandidatesError
from persona_sim.panel.build import PANEL_COLUMNS, build_panel
from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.sampling import select_members
from tests.conftest import base_survey_dict

pytestmark = pytest.mark.spark

PERSONA_SCHEMA = (
    "uuid string, sex string, age int, prefecture string, region string, "
    "area string, marital_status string, education_level string"
)


@pytest.fixture(scope="session")
def personas(spark):
    """合成ペルソナ 400 件。性別は交互、年齢は 20〜59 を巡回させる。"""
    rows = [
        (
            f"u{index:04d}",
            "男" if index % 2 == 0 else "女",
            20 + (index // 2) % 40,
            "東京都",
            "関東地方",
            "東日本",
            "未婚",
            "大学卒 文系",
        )
        for index in range(400)
    ]
    return spark.createDataFrame(rows, PERSONA_SCHEMA).cache()


def _survey(slots: int = 3):
    data = base_survey_dict(slots=slots)
    data["panel"]["size"] = 30
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20s", "sex": "男", "age_min": 20, "age_max": 29, "n": 15},
        {"cell_id": "F_20s", "sex": "女", "age_min": 20, "age_max": 29, "n": 15},
    ]
    return survey_from_dict(data)


def _members(spark, personas, survey):
    from persona_sim.panel.build import _assigned_stimuli

    members, report = select_members(spark, personas, survey)
    assigned = _assigned_stimuli(members, survey)
    rows = assigned.select("persona_uuid", "cell_id", "cell_rank", "assigned_stimuli").collect()
    return rows, report


def test_seed_reproducibility(spark, personas):
    """同じ seed で2回実行したら、選ばれる人も割り当ても完全に一致する。"""
    survey = _survey()
    first, _ = _members(spark, personas, survey)
    second, _ = _members(spark, personas, survey)

    def key(rows):
        return sorted(
            (r["persona_uuid"], r["cell_id"], r["cell_rank"], tuple(r["assigned_stimuli"]))
            for r in rows
        )

    assert key(first) == key(second)


def test_result_is_independent_of_parallelism(spark, personas):
    """パーティション数と shuffle 並列度を変えても結果が同じ。

    `DataFrame.sample(seed=…)` を使っているとここで壊れる。
    """
    survey = _survey()
    baseline, _ = _members(spark, personas.repartition(1), survey)

    previous = spark.conf.get("spark.sql.shuffle.partitions")
    try:
        spark.conf.set("spark.sql.shuffle.partitions", "13")
        other, _ = _members(spark, personas.repartition(7), survey)
    finally:
        spark.conf.set("spark.sql.shuffle.partitions", previous)

    def key(rows):
        return sorted((r["persona_uuid"], r["cell_id"], r["cell_rank"]) for r in rows)

    assert key(baseline) == key(other)


def test_different_seed_selects_different_people(spark, personas):
    survey = _survey()
    data = base_survey_dict()
    data["panel"]["size"] = 30
    data["panel"]["seed"] = 12345
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20s", "sex": "男", "age_min": 20, "age_max": 29, "n": 15},
        {"cell_id": "F_20s", "sex": "女", "age_min": 20, "age_max": 29, "n": 15},
    ]
    other_survey = survey_from_dict(data)

    first = {r["persona_uuid"] for r in _members(spark, personas, survey)[0]}
    second = {r["persona_uuid"] for r in _members(spark, personas, other_survey)[0]}
    assert first != second


def test_quotas_are_filled_and_conditions_hold(spark, personas):
    rows, report = _members(spark, personas, _survey())
    assert report.achieved == {"M_20s": 15, "F_20s": 15}
    assert len(rows) == 30
    assert report.weights == {"M_20s": 1.0, "F_20s": 1.0}


def test_no_duplicate_persona_with_overlapping_cells(spark, personas):
    """セル条件が重なっても、同一ペルソナはパネルに1回しか現れない（§5.0）。"""
    data = base_survey_dict()
    data["panel"]["size"] = 30
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20s", "sex": "男", "age_min": 20, "age_max": 29, "n": 15},
        {"cell_id": "M_young", "sex": "男", "age_min": 20, "age_max": 34, "n": 15},
    ]
    rows, report = _members(spark, personas, survey_from_dict(data))

    uuids = [r["persona_uuid"] for r in rows]
    assert len(uuids) == len(set(uuids)) == 30
    assert report.overlap_excluded  # 重なりを検知して警告材料を残していること


def test_every_persona_gets_every_stimulus_in_definition_order(spark, personas):
    """反実仮想モナディック固定（§5）。重複が無いことは配列の形から自明に成り立つ。"""
    rows, _ = _members(spark, personas, _survey())
    for row in rows:
        assigned = list(row["assigned_stimuli"])
        assert assigned == ["c1", "c2", "c3"]
        assert len(assigned) == len(set(assigned))


def test_each_stimulus_is_evaluated_by_everyone(spark, personas):
    """コンセプトごとの評価者数が揃う——全員が全案を見るので定義から等しい。"""
    rows, _ = _members(spark, personas, _survey())
    counts = Counter(
        stimulus_id for row in rows for stimulus_id in row["assigned_stimuli"]
    )
    assert set(counts) == {"c1", "c2", "c3"}
    assert len(set(counts.values())) == 1 == len({len(rows)}) or max(counts.values()) == len(rows)


def test_a_single_concept_survey_assigns_just_that_one(spark, personas):
    rows, _ = _members(spark, personas, _survey(slots=1))
    assert all(list(row["assigned_stimuli"]) == ["c1"] for row in rows)


def test_insufficient_candidates_raises_e1(spark, personas):
    data = base_survey_dict()
    data["panel"]["size"] = 500
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20s", "sex": "男", "age_min": 20, "age_max": 29, "n": 500}
    ]
    with pytest.raises(InsufficientCandidatesError) as excinfo:
        select_members(spark, personas, survey_from_dict(data))
    assert "M_20s" in str(excinfo.value)


def _stage_count(spark, group, run):
    """`run()` の実行中に走った Spark ステージ数を数える。"""
    context = spark.sparkContext
    context.setJobGroup(group, group)
    try:
        run()
    finally:
        # pyspark 4.0 に clearJobGroup は無い。別のグループに移して実質的に閉じる。
        context.setJobGroup(f"{group}__closed", "")

    tracker = context.statusTracker()
    return sum(
        len(tracker.getJobInfo(job_id).stageIds) for job_id in tracker.getJobIdsForGroup(group)
    )


def _survey_with_cells(count):
    """男女 × 10歳刻みで `count` セルの調査定義を作る（`personas` は 20〜59歳）。"""
    bands = [
        (sex, code, lower)
        for lower in (20, 30, 40, 50)
        for sex, code in (("男", "M"), ("女", "F"))
    ][:count]
    data = base_survey_dict()
    data["panel"]["size"] = 5 * count
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": f"{code}_{lower}s", "sex": sex, "age_min": lower, "age_max": lower + 9, "n": 5}
        for sex, code, lower in bands
    ]
    return survey_from_dict(data)


def test_selection_cost_grows_linearly_with_cells(spark, personas):
    """抽出コストがセル数に線形であること。

    セルごとに前セルの DataFrame を繋ぐと、その計算木が後続セルに入れ子で積み上がり、
    コストがセル数に対して急激に増える。**この症状は正しさのテストを全部通過してしまう**
    ので、コストそのものをここで見張る。

    実測（pyspark 4.0.1 / local）: 確定行を driver 側に取り出す実装は 2セル=12・6セル=24
    ステージ（2.0倍）。前セルを繋いでいた実装は 2セル=12・6セル=106 ステージ（8.8倍）で、
    8セルでは 396 ステージまで伸びた。
    """
    small = _stage_count(
        spark, "cells2", lambda: select_members(spark, personas, _survey_with_cells(2))
    )
    large = _stage_count(
        spark, "cells6", lambda: select_members(spark, personas, _survey_with_cells(6))
    )

    assert small > 0
    assert large <= small * 3, (
        f"セル数を3倍にしてステージ数が {large / small:.1f} 倍（{small} → {large}）。"
        "線形なら約2倍にとどまる"
    )


def test_selection_columns_cover_every_filter_field(spark, personas):
    """`PersonaFilter` の全フィールドを使う定義が抽出を通ること。

    抽出は `quotas.SELECTION_COLUMNS` へ射影してから条件を当てるので、
    `filter_condition` が見る列がその定数から漏れていると列を解決できずに落ちる。
    """
    data = base_survey_dict()
    data["panel"]["size"] = 3
    data["panel"]["filters"] = {"prefecture_in": ["東京都"]}
    data["panel"]["quotas"]["cells"] = [
        {
            "cell_id": "all_fields",
            "sex": "男",
            "age_min": 20,
            "age_max": 59,
            "prefecture_in": ["東京都"],
            "region_in": ["関東地方"],
            "area_in": ["東日本"],
            "marital_status_in": ["未婚"],
            "education_level_in": ["大学卒 文系"],
            "n": 3,
        }
    ]

    _, report = select_members(spark, personas, survey_from_dict(data))
    assert report.achieved == {"all_fields": 3}


def test_build_panel_writes_expected_columns(spark, personas, tmp_path):
    """`panels` は §2.2 の列を、その順序で持つ。"""
    from persona_sim.storage import delta
    from persona_sim.storage.locator import PERSONAS_BASE, locator

    storage = StorageConfig(warehouse=str(tmp_path))
    delta.write_table(personas, locator(PERSONAS_BASE, storage))

    survey = _survey()
    result = build_panel(spark, survey, storage)
    assert result.panel.columns == list(PANEL_COLUMNS)

    written = delta.read_table(spark, locator("panels", storage))
    assert written.count() == 30
    assert result.personas_version == 0


def test_build_panel_is_idempotent(spark, personas, tmp_path):
    """作り直しても同じ調査の行が二重にならない（§6.4）。"""
    from persona_sim.storage import delta
    from persona_sim.storage.locator import PERSONAS_BASE, locator

    storage = StorageConfig(warehouse=str(tmp_path))
    delta.write_table(personas, locator(PERSONAS_BASE, storage))

    survey = _survey()
    build_panel(spark, survey, storage)
    build_panel(spark, survey, storage)

    written = delta.read_table(spark, locator("panels", storage))
    assert written.count() == 30
