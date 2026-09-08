"""スクリーニングの結合テスト（Spark が必要）。

重点は M4 の完了条件（インシデンスが記録される）と、2方式の振る舞いの違い。
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from persona_sim.config import StorageConfig
from persona_sim.errors import ScreenerShortfallError
from persona_sim.llm.fake import FakeClient
from persona_sim.panel.build import build_panel
from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.sampling import (
    ROLE_CANDIDATE,
    ROLE_MAIN,
    ROLE_RESERVE,
    ROLE_SCREENED_OUT,
)
from persona_sim.panel.screening import screen_survey
from persona_sim.run.run import run_survey
from persona_sim.storage import delta
from persona_sim.storage.locator import (
    PANELS,
    PERSONAS_BASE,
    RESPONSES,
    SCREENER_RESPONSES,
    locator,
)
from tests.conftest import base_survey_dict
from tests.test_run_spark import PERSONA_SCHEMA

pytestmark = pytest.mark.spark

CELL_SIZE = 30
PERSONA_COUNT = 1200

#: 対象者条件。選択肢を提示しないので自然言語で書く（§4.2）。
SCREENER_CONDITIONS = ["ビールを月1回以上飲む"]


@pytest.fixture(scope="session")
def personas_frame(spark):
    rows = [
        (
            f"s{index:04d}",
            "男" if index % 2 == 0 else "女",
            20 + (index // 2) % 40,
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


def _survey_dict(*, oversample_factor=4, concepts=3, screening=True):
    # 全ペルソナが全コンセプトを評価するので、設問はコンセプト数だけ slot 展開する。
    data = base_survey_dict(slots=concepts)
    data["survey"]["id"] = f"screen_{'on' if screening else 'off'}_{concepts}"
    data["panel"]["size"] = CELL_SIZE
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20_40s", "sex": "男", "age_min": 20, "age_max": 49, "n": CELL_SIZE}
    ]
    if screening:
        data["screening"] = {
            "conditions": list(SCREENER_CONDITIONS),
            "oversample_factor": oversample_factor,
        }
    data["main_survey"]["model"]["concurrency"] = 8
    return data


def _prepare(spark, personas_frame, tmp_path, survey) -> StorageConfig:
    storage = StorageConfig(warehouse=str(tmp_path))
    delta.write_table(personas_frame, locator(PERSONAS_BASE, storage))
    build_panel(spark, survey, storage)
    return storage


def _panel_rows(spark, storage, survey):
    from pyspark.sql import functions as F

    return (
        delta.read_table(spark, locator(PANELS, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .collect()
    )


def _roles(rows) -> Counter:
    return Counter(row["role"] for row in rows)


class _JudgeClient:
    """判定用の決定論的クライアント。

    人物一覧の奇数番号だけを通す。FakeClient は番号を1つしか返さないので、
    通過率を制御したいこのテストでは使えない。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.batch_sizes: list[int] = []

    def describe(self) -> str:
        return "judge-fake"

    def complete(self, messages, **kwargs):
        from persona_sim.llm.client import Completion

        self.calls += 1
        listed = [
            line
            for line in messages[1].content.splitlines()
            if line[:1].isdigit() and ". " in line
        ]
        self.batch_sizes.append(len(listed))
        picked = [str(i) for i in range(1, len(listed) + 1) if i % 2 == 1]
        return Completion(
            text=",".join(picked), latency_ms=1, input_tokens=1, output_tokens=1
        )


class _RejectAllJudgeClient:
    """誰も通過させない判定クライアント（通過者ゼロの再現）。"""

    def describe(self) -> str:
        return "judge-reject-all"

    def complete(self, messages, **kwargs):
        from persona_sim.llm.client import Completion

        return Completion(text="", latency_ms=1, input_tokens=1, output_tokens=1)


class _FailingJudgeClient:
    """判定の呼び出しが必ず失敗するクライアント（エンドポイント障害の再現）。"""

    def __init__(self) -> None:
        self.calls = 0

    def describe(self) -> str:
        return "judge-failing"

    def complete(self, messages, **kwargs):
        from persona_sim.llm.client import LLMError

        self.calls += 1
        raise LLMError("判定エンドポイントに届かない")


# --------------------------------------------------------------------------- #
# スクリーニングの基本挙動（§4.2）
# --------------------------------------------------------------------------- #


def test_panel_writes_candidates_when_screening(spark, personas_frame, tmp_path):
    """判定前は candidate。誰が通過するかは screen まで分からない。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    rows = _panel_rows(spark, storage, survey)
    assert _roles(rows) == {ROLE_CANDIDATE: CELL_SIZE * 4}
    assert all(row["assigned_stimuli"] == [] for row in rows)


def test_incidence_is_recorded(spark, personas_frame, tmp_path):
    """M4 の完了条件。"""
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = screen_survey(spark, survey, storage, client=_JudgeClient())

    assert result.method == "infer"
    assert result.achieved["M_20_40s"] == CELL_SIZE
    assert 0.0 < result.incidence["total"] < 1.0
    assert 0.0 < result.incidence["M_20_40s"] < 1.0


def test_roles_after_screening(spark, personas_frame, tmp_path):
    """確定・予備・非通過に分かれ、非通過の行が消えていないこと（§4.2）。"""
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    rows = _panel_rows(spark, storage, survey)
    roles = _roles(rows)

    assert roles[ROLE_MAIN] == CELL_SIZE
    assert roles[ROLE_SCREENED_OUT] > 0, "非通過が1件も無い（判定が効いていない）"
    assert roles[ROLE_CANDIDATE] == 0
    assert sum(roles.values()) == CELL_SIZE * 4
    # 通過者の総数 = main + reserve
    assert roles[ROLE_MAIN] + roles[ROLE_RESERVE] + roles[ROLE_SCREENED_OUT] == CELL_SIZE * 4


def test_screened_out_rows_have_no_assignment(spark, personas_frame, tmp_path):
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    for row in _panel_rows(spark, storage, survey):
        if row["role"] != ROLE_MAIN:
            assert row["assigned_stimuli"] == []
            assert row["cell_rank"] is None


def test_cell_rank_is_renumbered_after_screening(spark, personas_frame, tmp_path):
    """確定後に順位を振り直す（§4.2 手順6）。

    候補の順位のまま残すと、非通過で空いた穴がそのまま `cell_rank` の飛びになり、
    「セル内の何番目に選ばれた人か」がパネルの記録から読めなくなる。
    """
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    main_rows = [row for row in _panel_rows(spark, storage, survey) if row["role"] == ROLE_MAIN]
    assert sorted(row["cell_rank"] for row in main_rows) == list(range(CELL_SIZE))
    # 全員が全コンセプトを評価する（§5）。
    assert all(list(row["assigned_stimuli"]) == ["c1", "c2", "c3"] for row in main_rows)


def test_rerun_does_not_ask_again(spark, personas_frame, tmp_path):
    """判定済みの候補に聞き直さない（`screener_responses` が冪等）。"""
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    first = screen_survey(spark, survey, storage, client=_JudgeClient())
    before = delta.read_table(spark, locator(SCREENER_RESPONSES, storage)).count()

    client = _JudgeClient()
    second = screen_survey(spark, survey, storage, client=client)

    assert client.calls == 0, "判定済みなのに聞き直している"
    assert second.sessions_total == 0
    assert delta.read_table(spark, locator(SCREENER_RESPONSES, storage)).count() == before
    assert second.achieved == first.achieved


def test_tokens_count_only_this_run(spark, personas_frame, tmp_path):
    """スクリーナーのトークンは**この実行で呼び出したぶんだけ**（docs/issues/20260805003.md）。

    再実行では1件も聞き直さないので 0 になる。ここで過去の実行ぶんまで足すと、
    ノートブックの表示が「今回いくら使ったか」を答えなくなる。
    """
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    first = screen_survey(spark, survey, storage, client=_JudgeClient())
    assert first.input_tokens > 0
    assert first.output_tokens > 0

    second = screen_survey(spark, survey, storage, client=_JudgeClient())
    assert second.input_tokens == 0
    assert second.output_tokens == 0


def test_no_screening_spends_no_tokens(spark, personas_frame, tmp_path):
    """判定しなければ 0。表示上も費用が発生していないことが分かるように。"""
    survey = survey_from_dict(_survey_dict(screening=False))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = screen_survey(spark, survey, storage, client=_JudgeClient())

    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_screener_answers_do_not_leak_into_responses(spark, personas_frame, tmp_path):
    """スクリーナーの回答が本調査の生データに混ざらないこと（§2.6）。"""
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())
    run_survey(spark, survey, storage, client=FakeClient())

    from pyspark.sql import functions as F

    responses = delta.read_table(spark, locator(RESPONSES, storage)).filter(
        F.col("survey_id") == F.lit(survey.survey_id)
    )
    question_ids = {row["question_id"] for row in responses.select("question_id").collect()}
    assert "sc1" not in question_ids
    # 設問は slot ごとに展開されるので、本調査の設問IDは 3 slot × 2問。
    assert question_ids == {q.id for q in survey.questions}


def test_run_surveys_only_confirmed_members(spark, personas_frame, tmp_path):
    """非通過者にまで本調査を実行しないこと。"""
    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())
    run_survey(spark, survey, storage, client=FakeClient())

    from pyspark.sql import functions as F

    main_uuids = {
        row["persona_uuid"] for row in _panel_rows(spark, storage, survey) if row["role"] == ROLE_MAIN
    }
    surveyed = {
        row["persona_uuid"]
        for row in delta.read_table(spark, locator(RESPONSES, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .select("persona_uuid")
        .collect()
    }
    assert surveyed == main_uuids


def test_premise_reaches_the_survey_prompt(spark, personas_frame, tmp_path):
    """判定で付与した条件がペルソナカードに載って本調査まで届くこと（§6.1）。"""
    from persona_sim.panel.schema import PromptHeadings
    from persona_sim.panel.screening import load_premises

    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    main_uuids = [
        row["persona_uuid"] for row in _panel_rows(spark, storage, survey) if row["role"] == ROLE_MAIN
    ]
    premises = load_premises(spark, survey, storage, main_uuids)

    assert len(premises) == len(main_uuids)
    for premise in premises.values():
        assert premise.heading == PromptHeadings().infer_premise
        # 条件文は言い換えずそのまま載る。
        assert premise.lines == tuple(SCREENER_CONDITIONS)


def test_shortfall_raises_e2_with_incidence(spark, personas_frame, tmp_path):
    """誰も通過しなければ、倍化を繰り返したうえで実インシデンスつきで止まる。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=2))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    with pytest.raises(ScreenerShortfallError) as excinfo:
        screen_survey(spark, survey, storage, client=_RejectAllJudgeClient())

    message = str(excinfo.value)
    assert "M_20_40s" in message
    assert "実インシデンス" in message
    # 母集団を使い切った場合も、E1（候補不足）ではなく E2 として実インシデンスを示す
    assert "通過者が必要数に満たない" in message


# --------------------------------------------------------------------------- #
# スクリーニング無し
# --------------------------------------------------------------------------- #


def test_panel_is_final_without_screening(spark, personas_frame, tmp_path):
    """判定しないので候補も非通過も現れない。"""
    survey = survey_from_dict(_survey_dict(screening=False))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    assert _roles(_panel_rows(spark, storage, survey)) == {ROLE_MAIN: CELL_SIZE}


def test_no_screening_calls_no_llm(spark, personas_frame, tmp_path):
    """`screening:` を書かなければ判定の呼び出しは0。1回でも呼んでいたら費用が乗る。"""
    survey = survey_from_dict(_survey_dict(screening=False))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    client = FakeClient()
    result = screen_survey(spark, survey, storage, client=client)

    assert client.calls == 0
    assert result.executed is False
    assert result.sessions_total == 0
    assert not delta.table_exists(spark, locator(SCREENER_RESPONSES, storage))


def test_no_screening_gives_no_premise(spark, personas_frame, tmp_path):
    """条件を書いていないので、ペルソナカードに前提を付けない。"""
    from persona_sim.panel.screening import load_premises

    survey = survey_from_dict(_survey_dict(screening=False))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    uuids = [row["persona_uuid"] for row in _panel_rows(spark, storage, survey)]
    assert load_premises(spark, survey, storage, uuids) == {}


def test_no_screening_records_no_incidence(spark, personas_frame, tmp_path):
    """測っていないことと、全員該当は違う。1.0 と書いてはいけない（§9）。"""
    from persona_sim.metadata import write_metadata

    survey = survey_from_dict(_survey_dict(screening=False))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())
    result = run_survey(spark, survey, storage, client=FakeClient())

    path = write_metadata(spark, survey, result, storage, str(tmp_path / "outputs"))
    panel = json.loads(path.read_text(encoding="utf-8"))["panel"]

    assert panel["screener_method"] is None
    assert panel["screener_incidence_estimated"] is None
    assert panel["oversample_actual"] is None


# --------------------------------------------------------------------------- #
# 判定の再利用と再判定（issue 202607281420 項目7）
# --------------------------------------------------------------------------- #


def test_infer_finalizes_panel_without_asking(spark, personas_frame, tmp_path):
    """判定はするが本人には聞かない。パネルは確定する。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    client = _JudgeClient()
    result = screen_survey(spark, survey, storage, client=client)

    assert result.method == "infer"
    assert result.executed
    assert client.calls > 0
    roles = _roles(_panel_rows(spark, storage, survey))
    assert roles[ROLE_MAIN] == CELL_SIZE
    assert ROLE_CANDIDATE not in roles


def test_infer_keeps_screened_out_rows(spark, personas_frame, tmp_path):
    """非通過も残す。インシデンス検証に使う（AGENTS.md 不変条件）。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    assert _roles(_panel_rows(spark, storage, survey))[ROLE_SCREENED_OUT] > 0


def test_infer_batches_candidates(spark, personas_frame, tmp_path):
    """1呼び出しに複数ペルソナをまとめる。これが ask より安い理由。"""
    data = _survey_dict(oversample_factor=4)
    data["screening"]["batch_size"] = 10
    survey = survey_from_dict(data)
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    client = _JudgeClient()
    screen_survey(spark, survey, storage, client=client)

    assert client.calls == CELL_SIZE * 4 / 10
    assert set(client.batch_sizes) == {10}


def test_infer_records_are_flagged_as_inferred(spark, personas_frame, tmp_path):
    """`ask` の実回答と区別できること。"""
    from persona_sim.run import flags as flag_names

    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    rows = delta.read_table(spark, locator(SCREENER_RESPONSES, storage)).collect()
    assert rows
    assert all(flag_names.INFERRED in row["flags"] for row in rows)


def test_infer_premise_reaches_the_survey_prompt(spark, personas_frame, tmp_path):
    """付与した属性が本調査のプロンプトに載り、由来が見出しで分かること。"""
    from persona_sim.panel.schema import PromptHeadings
    from persona_sim.panel.screening import load_premises

    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    main = [
        row["persona_uuid"]
        for row in _panel_rows(spark, storage, survey)
        if row["role"] == ROLE_MAIN
    ]
    premises = load_premises(spark, survey, storage, main)

    assert premises
    for premise in premises.values():
        # ask（本人の回答）でも assume（一律付与）でもない由来を示す。
        assert premise.heading == PromptHeadings().infer_premise
        assert premise.heading != PromptHeadings().ask_premise
        assert premise.heading != PromptHeadings().assume_premise
        assert "ビールを月1回以上飲む" in premise.render()


def test_infer_incidence_is_recorded_as_estimated(spark, personas_frame, tmp_path):
    """聞いていないので実測値の欄には入れない（AGENTS.md 不変条件と同じ趣旨）。"""
    from persona_sim.metadata import build_metadata
    from persona_sim.run.run import RunResult

    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    metadata = build_metadata(
        spark,
        survey,
        RunResult(survey_id=survey.survey_id, started_at=now, finished_at=now),
        storage,
    )
    panel = metadata["panel"]

    assert panel["screener_method"] == "infer"
    assert panel["screener_incidence_estimated"]
    assert panel["screener_incidence_estimated"]["total"] > 0


def test_infer_records_judging_conditions(spark, personas_frame, tmp_path):
    """誰をどう判定したかまで残さないと入力を再現できない（§9.1）。"""
    from datetime import UTC, datetime

    from persona_sim.metadata import build_metadata
    from persona_sim.run.run import RunResult

    data = _survey_dict(oversample_factor=4)
    data["screening"]["batch_size"] = 10
    data["screening"]["model"] = {"deployment": "judge-endpoint", "max_tokens": 256}
    data["screening"]["persona_card"] = {
        "persona_fields": [{"field": "culinary_persona", "label": "食まわり"}],
        "include_summary": False,
    }
    survey = survey_from_dict(data)
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    now = datetime.now(UTC)
    summary = build_metadata(
        spark,
        survey,
        RunResult(survey_id=survey.survey_id, started_at=now, finished_at=now),
        storage,
    )["panel"]["screener_infer"]

    assert summary["batch_size"] == 10
    assert summary["model"]["deployment"] == "judge-endpoint"
    assert summary["model"]["max_tokens"] == 256
    assert summary["persona_card"]["persona_fields"] == [
        {"field": "culinary_persona", "label": "食まわり"}
    ]
    assert summary["persona_card"]["include_summary"] is False


def test_infer_rerun_does_not_judge_again(spark, personas_frame, tmp_path):
    """判定済みなら聞き直さない（同じコマンドを二度打っても壊れない）。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    second = _JudgeClient()
    screen_survey(spark, survey, storage, client=second)
    assert second.calls == 0


def test_infer_endpoint_failure_does_not_finalize_the_panel(spark, personas_frame, tmp_path):
    """判定が全滅したらパネルを確定させない。中断理由にエラー文を残す。

    確定させてしまうと、聞けていないだけの候補が `screened_out` として残り、
    実インシデンス0%（＝条件に合う人がいない）と見分けがつかなくなる。
    """
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    client = _FailingJudgeClient()
    result = screen_survey(spark, survey, storage, client=client)

    assert client.calls > 0
    assert result.aborted_reason is not None
    # 何を直せばよいのかが分かるように、エンドポイントのエラー文をそのまま載せる。
    assert "判定エンドポイントに届かない" in result.aborted_reason
    roles = _roles(_panel_rows(spark, storage, survey))
    assert roles[ROLE_CANDIDATE] > 0
    assert ROLE_SCREENED_OUT not in roles


def test_infer_rejudges_candidates_whose_judgement_failed(spark, personas_frame, tmp_path):
    """判定できなかった候補は再実行で聞き直す（docs/issues/20260805001.md）。

    失敗を「判定済み・非通過」として残すと、再実行時に `todo` が空になり、
    LLM を1回も呼ばないまま「実インシデンスが低い」と誤診して打ち切っていた。
    """
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_FailingJudgeClient())

    second = _JudgeClient()
    result = screen_survey(spark, survey, storage, client=second)

    assert second.calls > 0
    assert result.aborted_reason is None
    assert _roles(_panel_rows(spark, storage, survey))[ROLE_MAIN] == CELL_SIZE


def test_infer_rejudges_when_the_prompt_changes(spark, personas_frame, tmp_path):
    """判定プロンプトを変えたら聞き直す。

    ここが効かないと、system prompt や rule をどう書き換えても古い判定が
    再利用され、結果が1件も変わらない（docs/issues/screeningの問題.md）。
    """
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    data = _survey_dict(oversample_factor=4)
    data["screening"]["prompt"] = {"rule": "条件に明確に反する人物のみを除いてください。"}
    changed = survey_from_dict(data)

    second = _JudgeClient()
    result = screen_survey(spark, changed, storage, client=second)
    assert second.calls > 0
    assert result.refreshed
    assert result.config_hash != ""


def test_infer_rejudges_when_the_judge_model_changes(spark, personas_frame, tmp_path):
    """判定モデルを差し替えたときも同じ。別のモデルの判定は別物。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    data = _survey_dict(oversample_factor=4)
    data["screening"]["model"] = {"deployment": "別の判定エンドポイント"}
    second = _JudgeClient()
    screen_survey(spark, survey_from_dict(data), storage, client=second)
    assert second.calls > 0


def test_infer_reuses_when_only_oversample_factor_changes(spark, personas_frame, tmp_path):
    """倍率は判定の中身を変えない。指紋に含めると再試行のたびに全件聞き直しになる。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    wider = survey_from_dict(_survey_dict(oversample_factor=8))
    second = _JudgeClient()
    screen_survey(spark, wider, storage, client=second)
    assert second.calls == 0


def test_infer_force_rejudges_everyone(spark, personas_frame, tmp_path):
    """設定を変えていなくても `--force` なら聞き直す（手動の逃げ道）。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    second = _JudgeClient()
    result = screen_survey(spark, survey, storage, client=second, force=True)
    assert second.calls > 0
    assert result.refreshed
    roles = _roles(_panel_rows(spark, storage, survey))
    assert roles[ROLE_MAIN] == CELL_SIZE
    assert ROLE_CANDIDATE not in roles


def test_screener_responses_carry_the_config_hash(spark, personas_frame, tmp_path):
    """どの設定で得た判定かを行に残す（§2.6）。"""
    from pyspark.sql import functions as F

    from persona_sim.panel.screening import CONFIG_HASH_COLUMN, screening_fingerprint

    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    rows = (
        delta.read_table(spark, locator(SCREENER_RESPONSES, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .collect()
    )
    assert rows
    assert {row[CONFIG_HASH_COLUMN] for row in rows} == {screening_fingerprint(survey)}


def test_rows_without_a_config_hash_are_rejudged(spark, personas_frame, tmp_path):
    """`config_hash` 列を持たない古いテーブルからは、全件を聞き直して自己修復する。"""
    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    screen_survey(spark, survey, storage, client=_JudgeClient())

    # 列ごと落として、この変更より前に作られたテーブルを再現する。
    screener_locator = locator(SCREENER_RESPONSES, storage)
    legacy = delta.read_table(spark, screener_locator).drop("config_hash")
    delta.write_table(legacy, screener_locator, mode="overwrite")
    build_panel(spark, survey, storage, oversample_factor=4)

    second = _JudgeClient()
    screen_survey(spark, survey, storage, client=second)
    assert second.calls > 0


def test_low_incidence_stops_before_exhausting_the_retries(spark, personas_frame, tmp_path):
    """通過率が構造的に低いときは、上限まで倍率を上げずに打ち切る。

    候補を倍にしても通過率は変わらないので必要数には届かず、判定の呼び出し費用
    だけが増える。届かないと分かった時点で止め、必要な候補数の見積もりを添える。
    """

    class _RejectAll:
        def __init__(self) -> None:
            self.calls = 0

        def describe(self) -> str:
            return "reject-all"

        def complete(self, messages, **kwargs):
            from persona_sim.llm.client import Completion

            self.calls += 1
            return Completion(text="なし", latency_ms=1, input_tokens=1, output_tokens=1)

    survey = survey_from_dict(_survey_dict(oversample_factor=4))
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    client = _RejectAll()
    with pytest.raises(ScreenerShortfallError) as excinfo:
        screen_survey(spark, survey, storage, client=client)

    message = str(excinfo.value)
    assert "0名" in message  # 通過0なので候補を増やしても届かない、と言うこと
    assert "打ち切った" in message
    # 4→8→16→32 と上げずに、初回で止まっていること。
    assert client.calls == CELL_SIZE * 4 / survey.screening.batch_size


# --------------------------------------------------------------------------- #
# 充足判定（`docs/issues/20260807002.md` M5）
# --------------------------------------------------------------------------- #


def test_a_cell_without_candidates_is_reported_as_short(spark, personas_frame, tmp_path):
    """割り付けにあるセルの候補が0件でも「充足」と言わないこと。

    候補のあるセルだけを回すと、候補ゼロのセルが `shortfalls` に載らないまま
    `complete` が真になり、**そのセル0人のまま本調査に進む**。
    現状は `select_members()` が全セルで必要数を確保してから書くのでこの状態は起きないが、
    充足判定がその前提に黙って依存する形にはしない。
    """
    from persona_sim.panel.build import finalize_panel

    survey = survey_from_dict(_survey_dict())
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    # 同じ survey_id のまま、候補を1件も持たないセルを割り付けに足す。
    widened = _survey_dict()
    widened["panel"]["size"] = CELL_SIZE * 2
    widened["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20_40s", "sex": "男", "age_min": 20, "age_max": 49, "n": CELL_SIZE},
        {"cell_id": "F_20_40s", "sex": "女", "age_min": 20, "age_max": 49, "n": CELL_SIZE},
    ]

    passed = {row["persona_uuid"] for row in _panel_rows(spark, storage, survey)}
    result = finalize_panel(spark, survey_from_dict(widened), storage, passed)

    assert not result.complete, "候補ゼロのセルがあるのに充足と判定してはいけない"
    assert result.shortfalls.get("F_20_40s") == CELL_SIZE
