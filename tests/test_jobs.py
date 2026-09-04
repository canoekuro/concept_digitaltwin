"""調査の投入（`persona_sim.uiconfig.jobs`）。

実際の Databricks には繋がない。**パスの組み立て・YAML の中身・SDK へ渡す引数**を、
偽のクライアントで確認する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml

from persona_sim.uiconfig import jobs
from persona_sim.uiconfig.schema import UIConfigError

VOLUME = "/Volumes/main/persona_lab/surveys"

SURVEY = {
    "survey": {"id": "beer_20260728", "name": "新ビールコンセプト評価調査", "type": "concept"},
    "panel": {"size": 500},
    "stimuli": [{"id": "c1", "name": "コンセプトA", "text": "【商品名】ゆずハイボール"}],
}

#: `ui_config.yaml` の `main_survey.prompt.systems` と同じ形の複数行プロンプト。
#: 箇条書きが字下げされているのが肝で、これがあると PyYAML は plain も
#: single-quoted も使えず double-quoted に落ちる。
MULTILINE_PROMPT = (
    "あなたは、提示された人物の立場で、実際の消費行動を想像しながら調査に回答します。\n"
    "回答時のルール:\n"
    "  - 商品への好感と購入意向は区別してください。\n"
    "  - 指定された形式のみで回答し、説明や前置きは書かないでください。\n"
)


@dataclass
class _Files:
    uploaded: list[tuple[str, bytes, bool]] = field(default_factory=list)

    def upload(self, path: str, contents: Any, overwrite: bool = False) -> None:
        self.uploaded.append((path, contents.read(), overwrite))


@dataclass
class _Response:
    run_id: int = 4242


@dataclass
class _Wait:
    response: _Response = field(default_factory=_Response)


@dataclass
class _Jobs:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run_now(self, **kwargs: Any) -> _Wait:
        self.calls.append(kwargs)
        return _Wait()


@dataclass
class _Client:
    files: _Files = field(default_factory=_Files)
    jobs: _Jobs = field(default_factory=_Jobs)


# --------------------------------------------------------------------------- #
# 純関数
# --------------------------------------------------------------------------- #


def test_survey_yaml_keeps_japanese_readable():
    """あとから人が「何を聞いたのか」を確認する対象になる。"""
    text = jobs.survey_yaml(SURVEY).decode("utf-8")
    assert "新ビールコンセプト評価調査" in text
    assert "\\u" not in text


def test_survey_yaml_round_trips():
    assert yaml.safe_load(jobs.survey_yaml(SURVEY)) == SURVEY


def test_multiline_prompts_are_written_as_literal_blocks():
    """プロンプトが `\\n` エスケープの1行に潰れない（`|` で出る）。

    PyYAML は既定でブロックスタイルを選ばないので、指定を外すとここが崩れる。
    """
    survey = {**SURVEY, "main_survey": {"prompt": {"system": MULTILINE_PROMPT}}}
    text = jobs.survey_yaml(survey).decode("utf-8")
    assert "system: |" in text
    assert "\\n" not in text
    # 字下げされた箇条書きが、行として読める形で残っている
    assert "      - 商品への好感と購入意向は区別してください。" in text


def test_multiline_prompts_round_trip():
    """見た目を変えても、ジョブが受け取る文字列は元のまま。"""
    survey = {**SURVEY, "main_survey": {"prompt": {"system": MULTILINE_PROMPT}}}
    loaded = yaml.safe_load(jobs.survey_yaml(survey))
    assert loaded["main_survey"]["prompt"]["system"] == MULTILINE_PROMPT


def test_a_string_that_cannot_be_a_literal_block_still_round_trips():
    """`|` にできない文字列（行末の空白・`\\r`）は PyYAML が引用形式に戻す。

    強制指定で壊れないことを確かめる。ここが崩れると、投入した調査定義と
    ジョブが読む内容が食い違う。
    """
    for text in ("行末に空白がある  \n次の行\n", "CR を含む\r\n次の行\n"):
        survey = {**SURVEY, "main_survey": {"prompt": {"system": text}}}
        loaded = yaml.safe_load(jobs.survey_yaml(survey))
        assert loaded["main_survey"]["prompt"]["system"] == text


def test_long_single_line_text_is_not_wrapped():
    """1行に収まる長文（コンセプト文）を折り返さない。既定の80桁では割れる。"""
    concept = "ゆず果汁を効かせた、すっきり飲めるハイボール。" * 12
    survey = {**SURVEY, "stimuli": [{"id": "c1", "name": "コンセプトA", "text": concept}]}
    text = jobs.survey_yaml(survey).decode("utf-8")
    assert concept in text


def test_survey_yaml_keeps_the_assembled_key_order():
    text = jobs.survey_yaml(SURVEY).decode("utf-8")
    assert text.index("survey:") < text.index("panel:") < text.index("stimuli:")


def test_volume_path_is_named_after_the_survey():
    assert jobs.volume_path(VOLUME, "beer_20260728") == f"{VOLUME}/beer_20260728.yaml"


def test_trailing_slash_does_not_double_up():
    assert jobs.volume_path(VOLUME + "/", "s1") == f"{VOLUME}/s1.yaml"


def test_a_path_outside_volumes_is_rejected():
    with pytest.raises(UIConfigError, match="/Volumes/"):
        jobs.volume_path("dbfs:/tmp/surveys", "s1")


def test_idempotency_token_is_derived_from_the_survey():
    """Streamlit の再実行で二重投入しないための鍵。"""
    assert jobs.idempotency_token("s1") == jobs.idempotency_token("s1")
    assert jobs.idempotency_token("s1") != jobs.idempotency_token("s2")


def test_survey_id_comes_from_the_definition_itself():
    assert jobs.survey_id_of(SURVEY) == "beer_20260728"


@pytest.mark.parametrize(
    "broken",
    [
        {"panel": {"size": 500}},
        {"survey": {"name": "名前だけ"}},
        {"survey": {"id": ""}},
        {"survey": "文字列"},
    ],
)
def test_a_definition_without_an_id_is_rejected(broken: dict):
    """ID が引けないまま投入すると、ファイル名と中身がずれた YAML が置かれる。"""
    with pytest.raises(UIConfigError, match="survey.id"):
        jobs.survey_id_of(broken)


# --------------------------------------------------------------------------- #
# クライアントへの受け渡し
# --------------------------------------------------------------------------- #


def test_upload_writes_the_yaml_to_the_volume():
    client = _Client()
    path = jobs.upload_survey(client, VOLUME, SURVEY)

    assert path == f"{VOLUME}/beer_20260728.yaml"
    uploaded_path, contents, overwrite = client.files.uploaded[0]
    assert uploaded_path == path
    assert yaml.safe_load(contents) == SURVEY
    # 同じ調査を組み直したときに上書きできること
    assert overwrite is True


def test_run_passes_the_path_as_a_job_parameter():
    client = _Client()
    run_id = jobs.run(client, "123", f"{VOLUME}/s1.yaml", "s1")

    assert run_id == 4242
    call = client.jobs.calls[0]
    assert call["job_id"] == 123  # 文字列で渡ってきても int にする
    assert call["job_parameters"] == {jobs.SURVEY_PARAMETER: f"{VOLUME}/s1.yaml"}
    assert call["idempotency_token"] == jobs.idempotency_token("s1")


def test_submit_uploads_then_runs():
    client = _Client()
    path, run_id = jobs.submit_survey(client, 7, VOLUME, SURVEY)

    assert path == f"{VOLUME}/beer_20260728.yaml"
    assert run_id == 4242
    assert client.files.uploaded, "調査定義を置いてから起動すること"
    assert client.jobs.calls[0]["job_parameters"][jobs.SURVEY_PARAMETER] == path


def test_the_uploaded_path_matches_the_id_inside_the_yaml():
    """ファイル名の ID と中身の ID がずれると、ジョブが書く survey_id と
    画面が表示する survey_id が食い違い、結果閲覧で自分の調査を特定できなくなる。"""
    client = _Client()
    path, _ = jobs.submit_survey(client, 7, VOLUME, SURVEY)

    uploaded_path, contents, _ = client.files.uploaded[0]
    inner_id = yaml.safe_load(contents)["survey"]["id"]
    assert path == uploaded_path == jobs.volume_path(VOLUME, inner_id)
    assert client.jobs.calls[0]["idempotency_token"] == jobs.idempotency_token(inner_id)


def test_run_does_not_wait_for_completion():
    """待つと Streamlit のスクリプト実行がブロックされる。"""
    client = _Client()
    jobs.run(client, 1, f"{VOLUME}/s1.yaml", "s1")
    # `.result()` を呼んでいたら偽クライアントの Wait に無いので落ちる
    assert client.jobs.calls
