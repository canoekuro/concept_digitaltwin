"""SQL Warehouse 経由の読み取り（`persona_sim.storage.warehouse`）。

接続は張らない。**クエリの組み立てと行の後処理だけ**を見る。ここが正しければ、
残りは Spark 経由と同じ純関数（`answers_from_rows` / `build_result`）を通る。
"""

from __future__ import annotations

import json
import sys

import pytest

from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.panel.loader import survey_from_dict
from persona_sim.storage import warehouse
from tests.conftest import base_survey_dict

CATALOG = StorageConfig(catalog="main", schema="persona_lab")


@pytest.fixture
def survey():
    data = base_survey_dict()
    data["output"]["segments"] = ["total", "sex", "age_band_10", "cell_id"]
    return survey_from_dict(data)


# --------------------------------------------------------------------------- #
# テーブル名の解決
# --------------------------------------------------------------------------- #


def test_tables_are_fully_qualified():
    assert warehouse.table("responses", CATALOG) == "main.persona_lab.responses"


def test_path_mode_is_rejected_with_a_reason():
    """SQL Warehouse からはカタログ経由でしか引けない。"""
    path_mode = StorageConfig(warehouse="/tmp/persona_lab")
    with pytest.raises(PersonaSimError, match="Unity Catalog"):
        warehouse.table("responses", path_mode)


# --------------------------------------------------------------------------- #
# クエリの組み立て
# --------------------------------------------------------------------------- #


def test_runs_query_filters_by_survey_type():
    """ほかの調査種別が同じテーブルに入っても画面に混ざらないこと。"""
    query, parameters = warehouse.runs_query(CATALOG)
    assert "main.persona_lab.runs" in query
    assert "survey_type = :survey_type" in query
    assert parameters == {"survey_type": "concept"}


def test_runs_query_reads_the_name_from_a_column():
    """一覧のたびに metadata_json を掘らない（§2.4 で列にした）。"""
    query, _ = warehouse.runs_query(CATALOG)
    assert "survey_name" in query
    assert "get_json_object" not in query
    assert "metadata_json" not in query


def test_runs_query_selects_only_what_the_page_shows():
    """メトリクスを外したので、その材料も引かない（`docs/issues/20260805004.md`）。"""
    query, _ = warehouse.runs_query(CATALOG)

    for name in ("sessions_total", "sessions_ok", "sessions_failed", "sessions_skipped"):
        assert name not in query
    # 選択肢のラベルに要る3列と、中断の警告に要る1列は残す。
    for name in ("survey_id", "survey_name", "finished_at", "aborted_reason"):
        assert name in query


def test_runs_query_keeps_one_row_per_survey():
    """再実行された調査で、一覧の完了時刻と取得するデータがずれないこと。

    畳まないと、画面が `survey_id` をキーに辞書へ入れ直すところで後勝ちの上書きが
    起き、新しい順に並べているぶん最古の行が残る。調査定義は
    `survey_definition_query()` が最新1件から復元するので食い違う。
    """
    query, _ = warehouse.runs_query(CATALOG)
    assert "ROW_NUMBER() OVER (PARTITION BY survey_id ORDER BY finished_at DESC) = 1" in query


def test_answers_query_joins_panels_and_personas(survey):
    query, parameters = warehouse.answers_query(CATALOG, survey)
    assert "main.persona_lab.responses" in query
    assert "main.persona_lab.panels" in query
    assert "main.persona_lab.personas_base" in query
    # 本調査に進んだペルソナだけ（reserve / screened_out は回答していない）
    assert parameters["role"] == "main"


def test_answers_query_wraps_array_columns_in_json(survey):
    """コネクタの複合型の扱いに依存しないよう、配列は文字列で受け取る。"""
    query, _ = warehouse.answers_query(CATALOG, survey)
    for column in warehouse.JSON_COLUMNS:
        assert f"to_json(r.{column}) AS {column}" in query


def test_answers_query_takes_cell_id_from_panels_only(survey):
    """両方から取ると SQL があいまいになる。"""
    query, _ = warehouse.answers_query(CATALOG, survey)
    assert "p.cell_id" in query
    assert "b.cell_id" not in query


def test_answers_query_selects_the_segment_attributes(survey):
    query, _ = warehouse.answers_query(CATALOG, survey)
    assert "b.sex" in query
    assert "b.age_band_10" in query


def test_survey_id_is_bound_not_interpolated(survey):
    """文字列連結で SQL を組み立てない。"""
    query, _ = warehouse.answers_query(CATALOG, survey)
    assert ":survey_id" in query
    assert survey.survey_id not in query


def test_survey_definition_query_takes_the_latest_run():
    """同じ survey_id で複数回実行されていることがある。"""
    query, _ = warehouse.survey_definition_query(CATALOG)
    assert "ORDER BY finished_at DESC LIMIT 1" in query


# --------------------------------------------------------------------------- #
# 行の後処理
# --------------------------------------------------------------------------- #


def test_json_columns_are_parsed_back_into_lists():
    row = {
        "persona_uuid": "u1",
        "answer_codes": "[2]",
        "options_order": "[1,2,3,4,5]",
        "flags": '["parse_error"]',
    }
    parsed = warehouse.parse_answer_row(row)
    assert parsed["answer_codes"] == [2]
    assert parsed["options_order"] == [1, 2, 3, 4, 5]
    assert parsed["flags"] == ["parse_error"]


def test_null_arrays_become_empty_lists():
    parsed = warehouse.parse_answer_row(
        {"answer_codes": None, "options_order": None, "flags": None}
    )
    assert parsed["answer_codes"] == []
    assert parsed["flags"] == []


def test_arrays_already_decoded_by_the_connector_are_accepted():
    """接続設定によっては配列のまま返る。どちらでも扱えること。"""
    parsed = warehouse.parse_answer_row(
        {"answer_codes": [1], "options_order": (1, 2), "flags": []}
    )
    assert parsed["answer_codes"] == [1]
    assert parsed["options_order"] == [1, 2]


def test_unparsable_json_does_not_crash_the_page():
    parsed = warehouse.parse_answer_row({"answer_codes": "not json", "options_order": "[]", "flags": "[]"})
    assert parsed["answer_codes"] == []


def test_parsed_rows_feed_the_shared_answer_conversion(survey):
    """SQL 経由でも Spark 経由と同じ純関数を通ること。"""
    from persona_sim.aggregate.frame import answers_from_rows

    row = {
        "persona_uuid": "u1",
        "stimulus_id": "c1",
        "question_id": "q_intent_1",
        "answer_codes": "[1]",
        "options_order": "[3,1,2,4,5]",
        "weight": None,
        "flags": "[]",
        "answer_text": None,
        "sex": "女",
        "age_band_10": "30代",
        "cell_id": "F_20s",
    }
    answers = answers_from_rows([warehouse.parse_answer_row(row)], survey)
    assert len(answers) == 1
    # 提示順 → 定義順の読み替えが効いていること
    assert answers[0].codes == (3,)
    assert answers[0].weight == 1.0


# --------------------------------------------------------------------------- #
# ローデータのクエリ
# --------------------------------------------------------------------------- #


def test_raw_query_serialises_arrays_as_json():
    """`r.*` だとコネクタが配列を何で返すかに挙動を預けることになる。"""
    query, parameters = warehouse.responses_query(CATALOG)

    assert "r.*" not in query
    for name in warehouse.JSON_COLUMNS:
        assert f"to_json(r.{name}) AS {name}" in query
    assert parameters == {"role": "main"}


def test_raw_query_keeps_the_columns_needed_to_label_answers():
    """コンセプト名と選択肢ラベルを引くのに要る列が落ちていないこと。"""
    query, _ = warehouse.responses_query(CATALOG)
    for name in ("stimulus_id", "question_id", "answer_codes", "options_order"):
        assert name in query


def test_raw_query_includes_the_reasoning_column():
    """理由を書かせた調査（§6.3）で、その理由がダウンロードに出ること。

    `answer_raw` にも同じ内容が JSON として入っているが、読むために毎回 JSON を
    割らせるのはローデータの用途に合わない。
    """
    query, _ = warehouse.responses_query(CATALOG)

    assert "answer_reasoning" in warehouse.RAW_RESPONSE_COLUMNS
    assert "r.answer_reasoning" in query


def test_raw_query_does_not_select_the_timestamp():
    """`ts` を引かないこと（`docs/issues/20260805004.md`）。

    `responses.ts` は `timestamp` 列で、コネクタはタイムゾーン付きの `datetime` で
    返す。openpyxl は tz 付き datetime をセルに書けないので、1列あるだけで
    ダウンロードが丸ごと `TypeError` で失敗する。
    """
    query, _ = warehouse.responses_query(CATALOG)

    assert "ts" not in warehouse.RAW_RESPONSE_COLUMNS
    assert "r.ts" not in query


def test_raw_query_keeps_the_diagnostic_columns():
    """時刻を落としても、どの試行がどれだけかかったかは追えること。"""
    query, _ = warehouse.responses_query(CATALOG)
    assert "r.latency_ms" in query
    assert "r.attempt" in query


# --------------------------------------------------------------------------- #
# 調査定義の復元
# --------------------------------------------------------------------------- #


class _StubConnection:
    """`metadata_json` を1行返すだけの接続。実接続は張らない。"""

    description = [("metadata_json",)]

    def __init__(self, rows: list[str]):
        self._rows = rows

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, parameters=None):
        self.query = query
        self.parameters = parameters

    def fetchall(self):
        return [(row,) for row in self._rows]


def test_fetch_survey_reads_a_record_written_before_the_rules_changed():
    """聞き方の書き方が変わっても、過去の調査の結果が読めること。

    `prompt.rules.reasoning` を設問タイプごとの入れ子にした時点（§6.3）より前の記録。
    集計は `prompt` を読まないので、ここで止めると結果だけが読めなくなる。
    """
    definition = base_survey_dict()
    definition["main_survey"]["prompt"] = {"rules": {"reasoning": "理由を{max_length}字で。"}}
    connection = _StubConnection([json.dumps({"survey_definition": definition})])

    survey = warehouse.fetch_survey(connection, CATALOG, "survey_test")

    assert survey.survey_id == "survey_test"
    assert len(survey.questions) == 6
    # 集計専用の復元であることが読み取れること（実行には使えない）。
    assert survey.from_record is True


def test_fetch_survey_reports_a_missing_run():
    connection = _StubConnection([])
    with pytest.raises(PersonaSimError, match="survey_unknown"):
        warehouse.fetch_survey(connection, CATALOG, "survey_unknown")


# --------------------------------------------------------------------------- #
# 依存関係
# --------------------------------------------------------------------------- #


def test_http_path_is_built_from_the_warehouse_id():
    """Databricks Apps が注入するのは ID なので、パスはこちらで組み立てる。"""
    assert warehouse.http_path("abc123") == "/sql/1.0/warehouses/abc123"


def test_module_does_not_pull_in_pyspark():
    """Databricks Apps の軽量コンテナで動く必要がある。"""
    assert "pyspark" not in sys.modules
