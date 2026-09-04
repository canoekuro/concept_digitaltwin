"""集計・出力の結合テスト（Spark が必要）。

重点は M5 の完了条件そのもの。

- `SPEC_PHASE1.md` §7.4 の出力一式が出ること
- `aggregates`（§2.5）の値が生データと合うこと
- `export` が `aggregates` だけから同じ表を作り直せること
"""

from __future__ import annotations

import csv

import pytest

from persona_sim.aggregate.aggregate import aggregate_survey
from persona_sim.aggregate.export import CSV_ENCODING
from persona_sim.config import StorageConfig
from persona_sim.panel.build import build_panel
from persona_sim.panel.loader import survey_from_dict
from persona_sim.run.run import run_survey
from persona_sim.storage import delta
from persona_sim.storage.locator import PERSONAS_BASE, RESPONSES, locator
from tests.conftest import base_survey_dict

pytestmark = pytest.mark.spark

openpyxl = pytest.importorskip("openpyxl")

PANEL_SIZE = 40
PERSONA_COUNT = 200

PERSONA_SCHEMA = (
    "uuid string, sex string, age int, age_band_5 string, age_band_10 string, "
    "prefecture string, region string, area string, marital_status string, "
    "education_level string, occupation_raw string, persona string, "
    "cultural_background string, professional_persona string, hobbies_and_interests string, "
    "culinary_persona string, source_version string"
)

#: §7.4 のファイル一式（crosstab_* は設問構成で本数が変わるので別に見る）。
EXPECTED_FILES = (
    "crosstab_all.csv",
    "open_ends.csv",
    "panel_composition.csv",
    "responses_raw.csv",
    "report.xlsx",
)


def _survey_dict(**overrides) -> dict:
    # コンセプト2件なので設問は slot 1・2 の2組。
    data = base_survey_dict(slots=2)
    data["panel"]["size"] = PANEL_SIZE
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20_40s", "sex": "男", "age_min": 20, "age_max": 49, "n": PANEL_SIZE // 2},
        {"cell_id": "F_20_40s", "sex": "女", "age_min": 20, "age_max": 49, "n": PANEL_SIZE // 2},
    ]
    data["stimuli"] = [
        {"id": "c1", "name": "コンセプトA", "text": "内容A"},
        {"id": "c2", "name": "コンセプトB", "text": "内容B"},
    ]
    data["main_survey"]["model"]["concurrency"] = 8
    data["output"] = {
        "segments": ["total", "sex", "sex_x_age_band_10", "cell_id"],
        "formats": ["csv", "xlsx"],
    }
    data.update(overrides)
    return data


@pytest.fixture(scope="session")
def personas_frame(spark):
    rows = [
        (
            f"p{index:04d}",
            "男" if index % 2 == 0 else "女",
            20 + (index // 4) % 40,
            "20-24",
            f"{20 + ((index // 4) % 40) // 10 * 10}代",
            "東京都",
            "関東地方",
            "東日本",
            "未婚",
            "大学卒 文系",
            "小売業 中堅",
            f"人物{index} の要約。",
            "下町育ち。",
            "販売企画を担当。",
            "登山と映画。",
            "外食は週2回。",
            "test@fixture",
        )
        for index in range(PERSONA_COUNT)
    ]
    return spark.createDataFrame(rows, PERSONA_SCHEMA).cache()


@pytest.fixture(scope="module")
def aggregated(spark, request, tmp_path_factory):
    """`panel` → `run` → `aggregate` まで通した状態を1度だけ作る。"""
    personas = request.getfixturevalue("personas_frame")
    tmp_path = tmp_path_factory.mktemp("aggregate")
    survey = survey_from_dict(_survey_dict())
    storage = StorageConfig(warehouse=str(tmp_path / "warehouse"))

    delta.write_table(personas, locator(PERSONAS_BASE, storage))
    build_panel(spark, survey, storage)
    run_survey(spark, survey, storage)

    output_dir = str(tmp_path / "outputs")
    result = aggregate_survey(spark, survey, storage, output_dir)
    return survey, storage, output_dir, result


# --------------------------------------------------------------------------- #
# 完了条件: §7.4 の一式が出る
# --------------------------------------------------------------------------- #


def test_every_output_file_is_written(aggregated):
    survey, _, output_dir, _ = aggregated
    from pathlib import Path

    destination = Path(output_dir) / survey.survey_id
    for name in EXPECTED_FILES:
        assert (destination / name).exists(), f"{name} が出ていない"

    # 選択式1問なので measure ごとの表は1枚。コンセプトはその表側に並ぶ。
    # 自由回答は §7.3 の open_ends.csv へ回る。
    crosstabs = sorted(path.name for path in destination.glob("crosstab_*.csv"))
    assert crosstabs == ["crosstab_all.csv", "crosstab_q_intent.csv"]


def test_crosstab_csv_has_a_row_for_every_segment(aggregated):
    survey, _, output_dir, _ = aggregated
    from pathlib import Path

    path = Path(output_dir) / survey.survey_id / "crosstab_q_intent.csv"
    with open(path, encoding=CSV_ENCODING, newline="") as handle:
        rows = list(csv.DictReader(handle))

    axes = {row["軸"] for row in rows}
    assert axes == {"全体", "sex", "sex_x_age_band_10", "cell_id"}
    total = next(row for row in rows if row["軸"] == "全体" and row["コンセプト"])
    assert int(total["n"]) > 0
    assert total["T2B"].endswith("%")


def test_open_ends_carry_persona_attributes(aggregated):
    survey, _, output_dir, _ = aggregated
    from pathlib import Path

    path = Path(output_dir) / survey.survey_id / "open_ends.csv"
    with open(path, encoding=CSV_ENCODING, newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    # 自由回答は slot ごとに別IDへ展開される（measure はどれも q_reason）。
    assert all(row["question_id"].startswith("q_reason") for row in rows)
    assert all(row["answer_text"] for row in rows)
    # §7.3: 主要属性を付与した長持ちテーブル。
    assert all(row["sex"] in ("男", "女") for row in rows)
    assert all(row["cell_id"] for row in rows)


def test_responses_raw_holds_every_record(spark, aggregated):
    survey, storage, output_dir, _ = aggregated
    from pathlib import Path

    from pyspark.sql import functions as F

    expected = (
        delta.read_table(spark, locator(RESPONSES, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .count()
    )
    path = Path(output_dir) / survey.survey_id / "responses_raw.csv"
    with open(path, encoding=CSV_ENCODING, newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == expected
    assert "answer_codes" in rows[0]


def test_panel_composition_reports_the_achieved_cells(aggregated):
    survey, _, output_dir, _ = aggregated
    from pathlib import Path

    path = Path(output_dir) / survey.survey_id / "panel_composition.csv"
    with open(path, encoding=CSV_ENCODING, newline="") as handle:
        rows = {row["cell_id"]: row for row in csv.DictReader(handle)}

    assert set(rows) == {"M_20_40s", "F_20_40s"}
    for row in rows.values():
        assert int(row["目標"]) == PANEL_SIZE // 2
        assert int(row["確定"]) == PANEL_SIZE // 2
        # スクリーナーが無い調査なので通過率は未測定（1.0 と書かない）。
        assert row["通過率"] == "-"


def test_report_xlsx_contains_a_sheet_per_table(aggregated):
    survey, _, output_dir, result = aggregated
    from pathlib import Path

    workbook = openpyxl.load_workbook(Path(output_dir) / survey.survey_id / "report.xlsx")
    assert workbook.sheetnames[0] == "概要"
    assert len(workbook.sheetnames) == 1 + len(result.tables)


# --------------------------------------------------------------------------- #
# 集計の値が生データと合っていること
#
# 集計結果はテーブルに保存しないので、検査対象は `responses` から数え直した
# `result.crosstabs`（`compute_metric()` を通った値そのもの）。
# --------------------------------------------------------------------------- #


def _metric(result, stimulus_id, measure, segment="total"):
    table = next(
        t for t in result.crosstabs if t.stimulus_id == stimulus_id and t.measure == measure
    )
    return next(row.metric for row in table.rows if row.segment == segment)


def test_the_counts_match_the_raw_responses(spark, aggregated):
    survey, storage, _, result = aggregated
    from pyspark.sql import functions as F

    metric = _metric(result, "c1", "q_intent")
    assert len(metric.percentages) == 5

    answered = (
        delta.read_table(spark, locator(RESPONSES, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .filter(F.col("stimulus_id") == F.lit("c1"))
        # 集計は measure 単位なので、同じ問いの slot 違いをすべて数える
        # （c1 を1番目に見た人は q_intent_1、2番目に見た人は q_intent_2 に答えている）。
        .filter(F.col("question_id").isin([q.id for q in survey.questions if q.measure_key == "q_intent"]))
        .filter(F.size(F.col("answer_codes")) > 0)
        .count()
    )
    assert metric.n == answered
    # ウェイトが全て 1.0 なので、比率の合計は 1.0 になる。
    assert sum(metric.percentages) == pytest.approx(1.0)


def test_top_box_equals_the_sum_of_its_options(aggregated):
    _, _, _, result = aggregated
    metric = _metric(result, "c1", "q_intent")

    top_two = sum(metric.percentages[:2])
    assert metric.top_box == pytest.approx(top_two)
    assert 1.0 <= metric.mean <= 5.0


def test_reaggregating_gives_the_same_numbers(spark, aggregated):
    """毎回 `responses` から数え直すので、何度やっても同じ表になる。"""
    survey, storage, output_dir, before = aggregated

    after = aggregate_survey(spark, survey, storage, output_dir)

    assert after.answers == before.answers
    assert [(t.key, t.rows) for t in after.tables] == [(t.key, t.rows) for t in before.tables]


# --------------------------------------------------------------------------- #
# 複数回答（multi）
# --------------------------------------------------------------------------- #


def test_multi_question_survives_run_resume_and_aggregation(spark, personas_frame, tmp_path):
    """multi が毎回やり直しにならず、集計まで到達すること。"""
    data = _survey_dict()
    # multi も slot ごとに展開する（全 slot に設問が要る。抜けると E6）。
    data["questions"].extend(
        {
            "id": f"q_uses_{slot}",
            "slot": slot,
            "measure": "q_uses",
            "text": "どの場面で飲みたいと思いますか。",
            "type": "multi",
            "options": ["夕食時", "入浴後", "外出先"],
        }
        for slot in (1, 2)
    )
    survey = survey_from_dict(data)
    storage = StorageConfig(warehouse=str(tmp_path / "warehouse"))
    delta.write_table(personas_frame, locator(PERSONAS_BASE, storage))
    build_panel(spark, survey, storage)

    first = run_survey(spark, survey, storage)
    assert first.sessions_skipped == 0

    # 2回目は全セッションが完了済みなので、1件も実行しない（§6.4）。
    second = run_survey(spark, survey, storage)
    assert second.sessions_skipped == second.sessions_total
    assert second.records_written == 0

    result = aggregate_survey(spark, survey, storage, str(tmp_path / "outputs"))
    multi_tables = [t for t in result.crosstabs if t.measure == "q_uses"]
    assert len(multi_tables) == 2
    total = next(row for row in multi_tables[0].rows if row.segment == "total")
    assert total.metric.n > 0
    assert sum(total.metric.percentages) == pytest.approx(1.0)
