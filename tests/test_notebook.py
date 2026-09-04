"""`notebooks/quickstart.ipynb` の健全性（Spark 不要）。

ノートブックは CI で実行するには重すぎる（HF からのダウンロードと Spark 起動を伴う）。
そのかわり、**壊れたまま気づかれない**状態を防ぐ最低限の検査だけを常に回す。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "quickstart.ipynb"


@pytest.fixture(scope="module")
def notebook() -> dict:
    return json.loads(NOTEBOOK.read_text(encoding="utf-8"))


def test_notebook_exists():
    assert NOTEBOOK.is_file()


def test_code_cells_compile(notebook):
    """構文エラーのまま入らないようにする。"""
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if source.lstrip().startswith("%") or "dbutils." in source:
            continue  # Databricks マジックはここでは評価できない
        compile(source, f"{NOTEBOOK.name}:cell{index}", "exec")


def test_outputs_are_not_committed(notebook):
    """実行結果を含めない。差分が読めなくなり、古い結果が残るため。

    `execution_count` は Databricks で保存すると 0 が入る（実行回数ではなく既定値）。
    実行回数そのものが残っている状態だけを弾く。
    """
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert not cell.get("outputs"), "出力を消してからコミットすること"
            assert not cell.get("execution_count"), "実行回数を消してからコミットすること"


def test_attribution_is_present(notebook):
    """CC BY 4.0 の帰属表示は義務（SPEC.md §15.3）。"""
    text = "".join(
        "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "markdown"
    )
    assert "Nemotron-Personas-Japan" in text
    assert "CC BY 4.0" in text
    assert "実在する生活者の回答ではありません" in text


def test_survey_definition_in_notebook_is_valid():
    """ノートブックの調査定義が、実装の検証を通ること。

    仕様やスキーマを変えたときに、ノートブックだけ古いまま残るのを防ぐ。
    """
    from persona_sim.panel.loader import survey_from_dict
    from persona_sim.panel.validate import validate_static

    survey_dict = _extract_survey_dict()
    report = validate_static(survey_from_dict(survey_dict))
    assert report.ok, [str(issue) for issue in report.errors]


def _extract_survey_dict() -> dict:
    """調査定義を組み立てているセルだけを実行して `survey_dict` を取り出す。"""
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if "survey_dict = {" not in source:
            continue
        namespace: dict = {}
        # 末尾の loader 呼び出しは切り落とし、辞書の組み立てだけを評価する。
        body = source.split("from persona_sim.panel.loader")[0]
        exec(compile(body, "quickstart.ipynb:survey_dict", "exec"), namespace)
        return namespace["survey_dict"]
    raise AssertionError("survey_dict を組み立てるセルが見つからない")
