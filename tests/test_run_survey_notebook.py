"""`notebooks/run_survey.ipynb` の健全性（Spark 不要）。

本番ジョブ用ノートブックなので nbclient での実行は CI では行わない。
そのかわり、**壊れたまま気づかれない**状態を防ぐ最低限の検査だけを常に回す。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "run_survey.ipynb"


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
    """実行結果を含めない。差分が読めなくなり、古い結果が残るため。"""
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert not cell.get("outputs"), "出力を消してからコミットすること"
            assert not cell.get("execution_count"), "実行回数を消してからコミットすること"


def test_survey_widget_does_not_require_the_app(notebook):
    """`survey` パラメータがアプリ経由に限定されていないこと。

    アプリを経由しない単体実行（ジョブの「今すぐ実行」やクラスタアタッチ）でも
    `job_parameters`/ウィジェットで同じ値を渡せるので、その旨がエラー文言や
    説明に残っていることを確認する（`アプリ側が渡すはずの` のような、
    アプリ限定を前提にした文言が復活していないか）。
    """
    source = "".join(
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    )
    assert 'dbutils.widgets.text("survey"' in source
    assert "アプリ側が渡すはずの" not in source


def test_screening_abort_fails_the_job(notebook):
    """スクリーニングの中断を握りつぶさないこと（docs/issues/20260805001.md）。

    `screen_survey` は判定が全滅しても例外ではなく `aborted_reason` で返す
    （CLI は集計表を出してから終了コードで伝える設計）。ノートブックがこれを
    見ないと、判定できていないだけなのに次のセルで「先に screen を実行しろ」と
    いう無関係な文言で落ちる。
    """
    source = next(
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code" and "screen_survey(" in "".join(cell["source"])
    )
    assert "aborted_reason" in source
    assert "raise" in source


def test_token_usage_is_reported(notebook):
    """トークン使用量を最後に出すこと（docs/issues/20260805003.md）。

    単価はワークスペースごとに違うのでシステム側は金額を持たない。費用を把握する
    唯一の手掛かりがトークン数なので、ジョブ実行の出力から消えていないか見張る。
    スクリーナーと回答生成は別のモデル・別の呼び出しなので、分けて出す。
    """
    last_code = [
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    ][-1]

    assert "screening.input_tokens" in last_code
    assert "screening.output_tokens" in last_code
    assert "run_result.input_tokens" in last_code
    assert "run_result.output_tokens" in last_code
    # 合計も出す（要望どおり、内訳だけでは足し算を読み手にさせることになる）。
    assert "total_input" in last_code
    assert "total_output" in last_code


def _cell_with(notebook: dict, needle: str) -> str:
    return next(
        source
        for cell in notebook["cells"]
        if cell["cell_type"] == "code" and needle in (source := "".join(cell["source"]))
    )


def test_run_reports_progress(notebook):
    """回答生成の進捗を出すこと。

    `run_survey()` は完走まで `responses` を書かないので、進捗を渡さないと実行中の
    出力が一切無い。1セッションに数十秒かかる状態では、遅いのか止まったのかを
    区別できず、待てずに中断すると**その実行ぶんが丸ごと消える**。
    """
    source = _cell_with(notebook, "run_survey(")
    assert "progress=" in source
    assert "console_reporter" in source


def test_screening_reports_progress(notebook):
    """スクリーニングも同じ理由で進捗を出すこと。"""
    source = _cell_with(notebook, "screen_survey(")
    assert "progress=" in source
    assert "console_reporter" in source


def test_warnings_are_printed(notebook):
    """警告を握りつぶさないこと。

    E3/E4（パース失敗・拒否）も W_ENDPOINT_RETRY（エンドポイントの詰まり）も
    `warnings` にしか出ない。印字しないと、結果だけ見て正常だと思い込むことになる。
    """
    for needle in ("run_survey(", "screen_survey("):
        assert "warnings" in _cell_with(notebook, needle)


def test_run_shares_one_client_with_the_reporter(notebook):
    """進捗行に再試行の状況を出すには、run と表示が同じクライアントを見る必要がある。

    モデルは調査定義から引くこと（`build_client(survey.model)`）。ここで別の
    エンドポイントを書くと、調査定義と実際に叩いた先が食い違う。
    """
    source = _cell_with(notebook, "run_survey(")
    assert "build_client(survey.model)" in source
    assert "client=client" in source
    assert "console_reporter(client=client)" in source
