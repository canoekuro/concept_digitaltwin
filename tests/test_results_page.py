"""結果閲覧ページが組み立てる表とファイル（`app/views/results.py`）。

Streamlit は起動しない。ページが呼ぶ純関数を**同じ順序でつなぎ**、画面に出る表と
ダウンロードされる xlsx の中身だけを見る。`docs/issues/20260805002.md` で決めた
構成（クロス集計表だけ・ダウンロードは1ファイル）が崩れたらここが落ちる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from persona_sim.aggregate import segments as segment_axes
from persona_sim.aggregate.aggregate import build_result
from persona_sim.aggregate.crosstab import tabulated_measures
from persona_sim.aggregate.export import UI_RAW_SHEET, UI_SUMMARY_SHEET, write_ui_workbook
from persona_sim.aggregate.frame import answers_from_rows
from persona_sim.aggregate.rawdata import labelled_csv_rows
from persona_sim.aggregate.tables import concept_axis_table, stacked_crosstab_table
from persona_sim.panel.loader import survey_from_dict
from persona_sim.storage import warehouse
from tests.conftest import base_survey_dict

openpyxl = pytest.importorskip("openpyxl")

#: 結果画面が属性別クロス集計の選択肢に出す表（`app/views/results.py`）。
CROSSTAB_PREFIX = "crosstab_"


def _survey_dict() -> dict:
    data = base_survey_dict()
    data["output"]["segments"] = ["total", "sex"]
    # 追加の設問も slot ごとに展開する。measure で1つの表に畳まれる。
    data["questions"].extend(
        {
            "id": f"q_novelty_{slot}",
            "slot": slot,
            "measure": "q_novelty",
            "text": "目新しいと思いますか。",
            "type": "single",
            "options": ["とても", "やや", "ふつう", "あまり", "まったく"],
            "top_box": [1, 2],
        }
        for slot in (1, 2, 3)
    )
    return data


@pytest.fixture
def survey():
    return survey_from_dict(_survey_dict())


def _rows() -> list[dict]:
    rows = []
    for index, (stimulus, code, sex) in enumerate(
        [("c1", 1, "男"), ("c1", 2, "女"), ("c2", 5, "男"), ("c2", 1, "女")]
    ):
        # そのコンセプトを1番目に見た人として記録する（slot 1 の設問に答える）。
        for question in ("q_intent_1", "q_novelty_1"):
            rows.append(
                {
                    "persona_uuid": f"u{index}",
                    "stimulus_id": stimulus,
                    "question_id": question,
                    "answer_codes": [code],
                    "options_order": [1, 2, 3, 4, 5],
                    "weight": 1.0,
                    "flags": [],
                    "answer_text": None,
                    "sex": sex,
                }
            )
    return rows


@pytest.fixture
def result(survey):
    return build_result(survey, answers_from_rows(_rows(), survey), [])


# --------------------------------------------------------------------------- #
# 画面に出る表
# --------------------------------------------------------------------------- #


def test_the_page_shows_one_concept_table_per_tabulated_question(survey, result):
    """一番上が購入意向、2番目が新規性。並びは調査定義のまま。

    **slot ごとに1表ではなく measure ごとに1表。** 設問は提示スロットぶん展開されるので、
    設問IDで回すと同じ問いの表がコンセプトの数だけ並ぶ（`app/views/results.py`）。
    """
    tables = [
        concept_axis_table(
            survey,
            result.crosstabs,
            question.measure_key,
            key=f"concept_axis_{question.measure_key}",
            title=question.text,
            segments=(segment_axes.TOTAL,),
        )
        for question in tabulated_measures(survey)
    ]

    assert [table.key for table in tables] == [
        "concept_axis_q_intent",
        "concept_axis_q_novelty",
    ]
    for table in tables:
        # 軸＝コンセプト、セグメントは全体のみ。1コンセプト1行。
        assert table.columns[0] == "コンセプト"
        assert "セグメント" not in table.columns
        assert len(table.rows) == len(survey.stimuli)
        assert table.columns[-2:] == ("T2B", "平均")


def test_the_attribute_table_picker_offers_only_concept_by_question(result):
    """コンセプト比較表とパネル構成表は選ばせない。"""
    offered = [table.key for table in result.tables if table.key.startswith(CROSSTAB_PREFIX)]

    assert offered
    assert "concept_summary" not in offered
    assert "concept_summary_by_segment" not in offered
    assert "panel_composition" not in offered


# --------------------------------------------------------------------------- #
# ダウンロード（1ファイル）
# --------------------------------------------------------------------------- #


def test_the_download_is_one_workbook_with_the_table_and_the_raw_data(
    tmp_path, survey, result
):
    raw_columns, raw_rows = labelled_csv_rows(survey, _rows())
    table = stacked_crosstab_table(
        survey, result.crosstabs, key="crosstab", title="クロス集計表"
    )
    path = write_ui_workbook(
        tmp_path / "r.xlsx",
        survey,
        table,
        notes=result.notes,
        raw_columns=raw_columns,
        raw_rows=raw_rows,
    )

    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames == [UI_SUMMARY_SHEET, UI_RAW_SHEET]

    body = list(workbook[UI_SUMMARY_SHEET].iter_rows(values_only=True))
    header = next(row for row in body if row[0] == "設問")
    assert header[:6] == ("設問", "コンセプト", "軸", "セグメント", "n", "n(フラグ除外後)")
    # 2問 × 3コンセプト × セグメント（全体・男・女）ぶんの行がある。
    assert {row[0] for row in body if row[0] in ("q_intent", "q_novelty")} == {
        "q_intent",
        "q_novelty",
    }

    raw = list(workbook[UI_RAW_SHEET].iter_rows(values_only=True))
    assert "stimulus_name" in raw[0]
    assert "answer_labels" in raw[0]
    assert raw[1][raw[0].index("stimulus_name")] == "コンセプトA"
    assert raw[1][raw[0].index("answer_labels")] == "1. ぜひ"


def test_the_downloaded_workbook_carries_the_attribution_and_the_disclaimer(
    tmp_path, survey, result
):
    """ダウンロードされて独り歩きするので、帰属表示（§15.3）と免責（§15.1）を載せる。"""
    table = stacked_crosstab_table(
        survey, result.crosstabs, key="crosstab", title="クロス集計表"
    )
    path = write_ui_workbook(tmp_path / "r.xlsx", survey, table, notes=result.notes)

    workbook = openpyxl.load_workbook(path)
    body = "\n".join(
        str(row[0]) for row in workbook[UI_SUMMARY_SHEET].iter_rows(values_only=True) if row[0]
    )

    assert "Nemotron-Personas-Japan" in body
    assert "AIによるシミュレーション" in body


# --------------------------------------------------------------------------- #
# タイムゾーン付き datetime（`docs/issues/20260805004.md`）
# --------------------------------------------------------------------------- #


def test_a_timezone_aware_datetime_breaks_the_download(tmp_path, survey):
    """1セルあれば xlsx の書き出しが丸ごと落ちる、という前提を固定する。

    これが `TypeError: Excel does not support timezones in datetimes` の正体で、
    ローデータから `ts` を落とした理由。openpyxl 側が将来これを許すようになったら
    ここが落ちるので、そのとき列を戻すか判断すればよい。
    """
    table = stacked_crosstab_table(
        survey, {}, key="crosstab", title="クロス集計表"
    )

    with pytest.raises(TypeError, match="timezone"):
        write_ui_workbook(
            tmp_path / "r.xlsx",
            survey,
            table,
            raw_columns=("persona_uuid", "ts"),
            raw_rows=[["u0", datetime(2026, 8, 5, 7, 21, 45, tzinfo=UTC)]],
        )


# --------------------------------------------------------------------------- #
# 共有接続（`docs/issues/20260807002.md` H4）
# --------------------------------------------------------------------------- #

#: 結果閲覧ページの本体。ここでは import しない（streamlit を要求してしまうため）。
_RESULTS_PAGE = Path(__file__).resolve().parents[1] / "app/views/results.py"
_CONTEXT_MODULE = Path(__file__).resolve().parents[1] / "app/lib/context.py"


def test_the_page_never_passes_the_shared_connection_to_a_fetcher():
    """取得は必ず `context.query()` を通すこと。

    接続は `@st.cache_resource` なので**全利用者で1本**を共有する。
    databricks-sql-connector の Connection は複数スレッドから同時に使えず、
    Streamlit は利用者ごとに別スレッドでスクリプトを再実行するので、
    素で `warehouse.fetch_*(connection, ...)` と書くと2人が同時に取得したときに
    リクエストが混ざる。

    Streamlit を起動せずに守るため、ソースの形で固定する。
    """
    source = _RESULTS_PAGE.read_text(encoding="utf-8")

    assert "context.query(" in source, "取得が context.query() を経由していない"
    assert "warehouse.fetch_survey(connection" not in source
    assert "warehouse.build_result(connection" not in source
    assert "warehouse.fetch_responses_raw(connection" not in source


def test_the_query_helper_holds_a_lock():
    """`query()` が錠を取ってから取得すること。"""
    source = _CONTEXT_MODULE.read_text(encoding="utf-8")

    assert "_QUERY_LOCK = threading.Lock()" in source
    assert "with _QUERY_LOCK:" in source


def test_the_raw_data_does_not_carry_a_timestamp_column(survey):
    """ローデータのクエリに時刻を戻さないこと。

    上のとおり、tz 付き datetime が1列あるだけでダウンロードが失敗する。
    `responses.ts` はコネクタが tz 付きで返す唯一の列なので、ここを守れば
    `write_ui_workbook()` に datetime は渡らない。
    """
    columns, _ = labelled_csv_rows(
        survey, [{name: None for name in warehouse.RAW_RESPONSE_COLUMNS}]
    )

    assert "ts" not in warehouse.RAW_RESPONSE_COLUMNS
    assert "ts" not in columns


def test_the_page_does_not_build_the_workbook_on_every_rerun():
    """xlsx の組み立ては `context.workbook_bytes()`（`@st.cache_data`）を通すこと。

    `st.download_button` は押される前からデータの実体を要求するので、ページが
    `write_ui_workbook()` を直に呼ぶと、表を切り替えるたび・チェックを触るたびに
    回答数千行の Excel を作り直す。Streamlit を起動せずに守るため、ソースの形で固定する。

    キャッシュが返すのは**バイト列**であってパスではない。一時ディレクトリは
    関数を抜けた時点で消えるので、パスを覚えると2回目以降に読めなくなる。
    """
    page = _RESULTS_PAGE.read_text(encoding="utf-8")
    context_source = _CONTEXT_MODULE.read_text(encoding="utf-8")

    assert "write_ui_workbook" not in page, "ページが xlsx を直接組み立てている"
    assert "context.workbook_bytes(" in page

    assert "@st.cache_data" in context_source
    assert "def workbook_bytes(" in context_source
    assert "return path.read_bytes()" in context_source
