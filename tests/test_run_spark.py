"""回答生成の結合テスト（Spark が必要）。

重点は M3・M6 の完了条件そのもの。

- 1コンセプト × 100人が完走すること（M3）
- 中断→再開で結果が一致すること（M6）
"""

from __future__ import annotations

import json

import pytest

from persona_sim.config import StorageConfig
from persona_sim.errors import EndpointFailureError
from persona_sim.llm.client import RetryStats
from persona_sim.llm.fake import FakeClient
from persona_sim.panel.build import build_panel
from persona_sim.panel.loader import survey_from_dict
from persona_sim.run.executor import ContinuousFailureDetector, raise_if_unrecoverable
from persona_sim.run.run import RESPONSES_SCHEMA_COLUMNS, run_survey
from persona_sim.storage import delta
from persona_sim.storage.locator import PANELS, PERSONAS_BASE, RESPONSES, RUNS, locator
from tests.conftest import base_survey_dict, with_remember

pytestmark = pytest.mark.spark

PANEL_SIZE = 100
PERSONA_COUNT = 400

PERSONA_SCHEMA = (
    "uuid string, sex string, age int, prefecture string, region string, area string, "
    "marital_status string, education_level string, occupation_raw string, persona string, "
    "cultural_background string, professional_persona string, hobbies_and_interests string, "
    "culinary_persona string, source_version string"
)


def _survey_dict(*, memory="none", **design_overrides) -> dict:
    """1コンセプト × 100人 × 2設問（閉じた設問1・自由回答1）。"""
    # コンセプトを1件に絞るので設問も slot 1 だけにする。1ペルソナが評価する
    # コンセプト数を超える slot は E6（`_check_slots`）。
    data = base_survey_dict(slots=1)
    data["panel"]["size"] = PANEL_SIZE
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20_40s", "sex": "男", "age_min": 20, "age_max": 49, "n": PANEL_SIZE}
    ]
    data["stimuli"] = [{"id": "c1", "name": "コンセプトA", "text": "内容A"}]
    data.update(design_overrides)
    data["main_survey"]["model"]["concurrency"] = 8
    return with_remember(data, memory)


def _survey(**overrides):
    return survey_from_dict(_survey_dict(**overrides))


@pytest.fixture(scope="session")
def personas_frame(spark):
    rows = [
        (
            f"p{index:04d}",
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


def _prepare(spark, personas_frame, tmp_path, survey) -> StorageConfig:
    """personas_base を書き、パネルを構築した状態を作る。"""
    storage = StorageConfig(warehouse=str(tmp_path))
    delta.write_table(personas_frame, locator(PERSONAS_BASE, storage))
    build_panel(spark, survey, storage)
    return storage


def _responses(spark, storage, survey):
    from pyspark.sql import functions as F

    return delta.read_table(spark, locator(RESPONSES, storage)).filter(
        F.col("survey_id") == F.lit(survey.survey_id)
    )


def _answer_key(spark, storage, survey):
    rows = _responses(spark, storage, survey).select(
        "persona_uuid", "stimulus_id", "question_id", "answer_codes", "answer_raw"
    ).collect()
    return sorted(
        (r["persona_uuid"], r["stimulus_id"], r["question_id"], tuple(r["answer_codes"] or ()), r["answer_raw"])
        for r in rows
    )


# --------------------------------------------------------------------------- #
# M3: 完走
# --------------------------------------------------------------------------- #


def test_one_stimulus_by_100_personas_completes(spark, personas_frame, tmp_path):
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = run_survey(spark, survey, storage, client=FakeClient())

    expected = PANEL_SIZE * len(survey.questions)
    assert result.sessions_total == expected
    assert result.sessions_ok == expected
    assert result.sessions_failed == 0
    assert result.records_written == expected
    assert not result.aborted
    assert _responses(spark, storage, survey).count() == expected


def test_responses_has_spec_columns(spark, personas_frame, tmp_path):
    """`responses` が §2.3 の列を、その順序で持つ。"""
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    run_survey(spark, survey, storage, client=FakeClient())

    responses = _responses(spark, storage, survey)
    assert responses.columns == list(RESPONSES_SCHEMA_COLUMNS)

    row = responses.filter("question_id = 'q_intent_1'").first()
    assert len(row["answer_codes"]) == 1
    assert 1 <= row["answer_codes"][0] <= 5
    assert row["options_order"] == [1, 2, 3, 4, 5]
    assert row["sequence"] == 1
    assert row["attempt"] == 1

    open_row = responses.filter("question_id = 'q_reason_1'").first()
    assert open_row["answer_codes"] == []
    assert open_row["answer_text"]

    # 理由を書かせていない調査なので、列はあっても中身は入らない。
    assert row["answer_reasoning"] is None


def test_reasoning_answers_are_written_to_their_own_column(spark, personas_frame, tmp_path):
    """`reasoning: true` の設問で、理由が `answer_reasoning` に残る（§6.3）。"""
    data = _survey_dict()
    data["questions"][0]["reasoning"] = True
    data["main_survey"]["model"]["structured_output"] = "always"
    survey = survey_from_dict(data)
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    run_survey(spark, survey, storage, client=FakeClient())

    rows = (
        _responses(spark, storage, survey)
        .filter("question_id = 'q_intent_1'")
        .select("answer_codes", "answer_reasoning")
        .collect()
    )
    assert len(rows) == PANEL_SIZE
    assert all(row["answer_reasoning"] for row in rows)
    assert all(len(row["answer_codes"]) == 1 for row in rows)


def test_full_session_memory_completes(spark, personas_frame, tmp_path):
    """履歴を持つモードでも完走する（1人1セッション）。"""
    survey = _survey(memory="full_session")
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = run_survey(spark, survey, storage, client=FakeClient())
    assert result.sessions_total == PANEL_SIZE
    assert result.sessions_ok == PANEL_SIZE
    assert _responses(spark, storage, survey).count() == PANEL_SIZE * len(survey.questions)


# --------------------------------------------------------------------------- #
# M6: 冪等性と再開
# --------------------------------------------------------------------------- #


def test_rerun_is_idempotent(spark, personas_frame, tmp_path):
    """完走後にもう一度流しても行が二重にならず、全セッションがスキップされる。"""
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    run_survey(spark, survey, storage, client=FakeClient())
    before = _answer_key(spark, storage, survey)

    second = run_survey(spark, survey, storage, client=FakeClient())

    assert second.sessions_skipped == second.sessions_total
    assert second.records_written == 0
    assert _answer_key(spark, storage, survey) == before


def test_resume_after_failures_matches_uninterrupted_run(spark, personas_frame, tmp_path):
    """中断→再開の結果が、一度で完走した結果と一致すること（M6 の完了条件）。"""
    survey = _survey()

    interrupted = _prepare(spark, personas_frame, tmp_path / "interrupted", survey)
    first = run_survey(spark, survey, interrupted, client=FakeClient(failure_rate=0.3))
    assert first.sessions_failed > 0, "失敗が注入されていない"
    assert first.sessions_ok > 0

    resumed = run_survey(spark, survey, interrupted, client=FakeClient())
    assert resumed.sessions_skipped == first.sessions_ok
    assert resumed.sessions_failed == 0

    reference = _prepare(spark, personas_frame, tmp_path / "reference", survey)
    run_survey(spark, survey, reference, client=FakeClient())

    assert _answer_key(spark, interrupted, survey) == _answer_key(spark, reference, survey)
    assert _responses(spark, interrupted, survey).count() == PANEL_SIZE * len(survey.questions)


def test_parse_error_rows_are_retried_on_resume(spark, personas_frame, tmp_path):
    """パース失敗のまま残った設問は、再実行でやり直す。"""
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    run_survey(spark, survey, storage, client=FakeClient(unparseable_rate=1.0))
    closed = _responses(spark, storage, survey).filter("question_id = 'q_intent_1'")
    assert closed.filter("size(answer_codes) = 0").count() == PANEL_SIZE

    second = run_survey(spark, survey, storage, client=FakeClient())
    assert second.sessions_skipped == PANEL_SIZE  # 自由回答ぶんはスキップされる
    assert _responses(spark, storage, survey).filter("size(answer_codes) = 0").count() == PANEL_SIZE


# --------------------------------------------------------------------------- #
# 品質フラグ・E5・メタデータ
# --------------------------------------------------------------------------- #


def test_refusal_and_parse_error_are_flagged_and_warned(spark, personas_frame, tmp_path):
    """フラグは立てるが除外しない。閾値超えは警告として表面化させる（§8・§11）。"""
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = run_survey(spark, survey, storage, client=FakeClient(refusal_rate=1.0))

    assert result.flag_counts["refusal"] == PANEL_SIZE * len(survey.questions)
    assert result.flag_counts["parse_error"] == PANEL_SIZE
    assert any("E3" in warning for warning in result.warnings)
    assert any("E4" in warning for warning in result.warnings)
    # 除外していないこと
    assert _responses(spark, storage, survey).count() == PANEL_SIZE * len(survey.questions)


def test_straightline_is_flagged_across_all_questions(spark, personas_frame, tmp_path):
    """閉じた設問が3問あり全て同じ位置なら straightline（§8）。"""
    data = _survey_dict()
    options = ["ぜひ", "やや", "どちらとも", "あまり", "まったく"]
    data["questions"] = [
        {"id": f"q{i}", "text": f"設問{i}", "type": "single", "options": options, "top_box": [1, 2]}
        for i in range(1, 4)
    ]
    survey = survey_from_dict(data)
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    run_survey(spark, survey, storage, client=FakeClient())

    from collections import defaultdict

    from pyspark.sql import functions as F

    responses = _responses(spark, storage, survey)
    rows = responses.select("persona_uuid", "answer_codes", "flags").collect()

    codes: dict[str, set[int]] = defaultdict(set)
    flagged: set[str] = set()
    for row in rows:
        # 3問とも single なので、番号が取れていれば必ず1要素。
        if row["answer_codes"]:
            codes[row["persona_uuid"]].add(row["answer_codes"][0])
        if "straightline" in row["flags"]:
            flagged.add(row["persona_uuid"])

    expected = {uuid for uuid, values in codes.items() if len(values) == 1}
    # 3問すべて同じ位置のペルソナがフラグ付き、それ以外は付いていない（両方向で一致）。
    assert flagged == expected
    assert expected, "同一位置のペルソナが1人も出ていない（判定を検証できていない）"

    # フラグを立てるだけで除外していないこと（§8）。
    assert responses.count() == PANEL_SIZE * 3
    assert responses.filter(F.array_contains(F.col("flags"), F.lit("straightline"))).count() == len(
        expected
    ) * 3


def test_continuous_failure_aborts_and_keeps_completed_work(spark, personas_frame, tmp_path):
    """E5: 中断しても完了分は保存し、同じコマンドで再開できる（§11）。"""
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = run_survey(
        spark,
        survey,
        storage,
        client=FakeClient(failure_rate=1.0),
        detector=ContinuousFailureDetector(consecutive_limit=5, window=10, rate_limit=0.5),
    )

    assert result.aborted
    assert result.sessions_ok == 0
    with pytest.raises(EndpointFailureError, match="再開"):
        raise_if_unrecoverable(result)

    recovered = run_survey(spark, survey, storage, client=FakeClient())
    assert not recovered.aborted
    assert _responses(spark, storage, survey).count() == PANEL_SIZE * len(survey.questions)


def test_run_metadata_is_written(spark, personas_frame, tmp_path):
    """§9 の項目が揃い、再現性の範囲が明記されていること。"""
    from persona_sim.metadata import write_metadata

    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    result = run_survey(spark, survey, storage, client=FakeClient())

    path = write_metadata(spark, survey, result, storage, str(tmp_path / "outputs"))
    metadata = json.loads(path.read_text(encoding="utf-8"))

    assert metadata["survey_id"] == survey.survey_id
    assert metadata["survey_definition"]["survey"]["id"] == survey.survey_id
    assert metadata["data_versions"]["personas_base"] == 0
    assert metadata["data_versions"]["source_dataset"] == "test@fixture"
    assert metadata["panel"]["requested"] == {"M_20_40s": PANEL_SIZE}
    assert metadata["panel"]["achieved"] == {"M_20_40s": PANEL_SIZE}
    assert set(metadata["design"]["questions_memory"]) == {q.id for q in survey.questions}
    assert metadata["execution"]["sessions_ok"] == PANEL_SIZE * len(survey.questions)
    assert metadata["reproducibility"]["guaranteed"] == "inputs_only"

    assert metadata["survey_name"] == survey.name
    assert metadata["survey_type"] == "concept"
    # 理由を書かせたかどうかは条件の違いそのものなので、使わなかった調査でも残す（§6.3）。
    assert metadata["model"]["reasoning_questions"] == []
    assert metadata["model"]["max_tokens_reasoning"] == survey.model.max_tokens_reasoning

    runs = delta.read_table(spark, locator(RUNS, storage))
    row = runs.filter(f"survey_id = '{survey.survey_id}'").collect()
    assert len(row) == 1
    # 調査名と種別は列としても引ける（一覧のたびに metadata_json を掘らないため。§2.4）
    assert row[0]["survey_name"] == survey.name
    assert row[0]["survey_type"] == "concept"


# --------------------------------------------------------------------------- #
# プロンプトの記録（§9）
# --------------------------------------------------------------------------- #


class _RecordingClient:
    """送ったメッセージ列をそのまま控えるクライアント。"""

    def __init__(self) -> None:
        self._inner = FakeClient()
        self.sent: list[list[tuple[str, object]]] = []

    def describe(self) -> str:
        return self._inner.describe()

    def complete(self, messages, **kwargs):
        self.sent.append([(message.role, message.content) for message in messages])
        return self._inner.complete(messages, **kwargs)


def _shape(entry: dict) -> list[tuple[str, object]]:
    return [(message["role"], message["content"]) for message in entry["messages"]]


def _multi_slot_survey(*, memory="none"):
    """3コンセプト × 6設問を小さいパネルで。設問をまたぐ記録を見るための定義。"""
    data = base_survey_dict(slots=3)
    data["panel"]["size"] = 4
    data["panel"]["quotas"]["cells"] = [
        {"cell_id": "M_20_40s", "sex": "男", "age_min": 20, "age_max": 49, "n": 4}
    ]
    data["main_survey"]["model"]["concurrency"] = 4
    return survey_from_dict(with_remember(data, memory))


def test_prompt_sample_covers_every_question_of_one_persona(spark, personas_frame, tmp_path):
    """1名ぶんだが、その1名については全設問が聞いた順に残ること（§9）。

    1問しか残らないと、2問目以降に何を送ったのかを事後に確かめる手段が無い
    （提示順が定義順に化けていた不具合を検知できなかった原因でもある）。
    """
    from persona_sim.metadata import build_metadata

    survey = _multi_slot_survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    result = run_survey(spark, survey, storage, client=FakeClient())

    sample = build_metadata(spark, survey, result, storage)["prompt_sample"]
    questions = sample["questions"]

    # ask order（slot 昇順、同 slot 内は定義順）で全設問。
    assert [entry["question_id"] for entry in questions] == [
        "q_intent_1", "q_reason_1", "q_intent_2", "q_reason_2", "q_intent_3", "q_reason_3"
    ]
    assert all(entry["missing_answers"] == [] for entry in questions)

    # コンセプトは、そのペルソナの提示順（`panels.assigned_stimuli`）の slot 番目。
    assigned = (
        delta.read_table(spark, locator(PANELS, storage))
        .filter(f"survey_id = '{survey.survey_id}'")
        .filter(f"persona_uuid = '{sample['persona_uuid']}'")
        .first()["assigned_stimuli"]
    )
    by_id = {question.id: question for question in survey.questions}
    for entry in questions:
        assert entry["stimulus_id"] == assigned[by_id[entry["question_id"]].slot - 1]


def test_prompt_sample_is_written_as_markdown(spark, personas_frame, tmp_path):
    """実行後に「何を聞いたのか」を確認できること。"""
    from persona_sim.metadata import write_metadata

    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    result = run_survey(spark, survey, storage, client=FakeClient())

    path = write_metadata(spark, survey, result, storage, str(tmp_path / "outputs"))
    metadata = json.loads(path.read_text(encoding="utf-8"))
    sample = metadata["prompt_sample"]

    assert sample is not None
    assert sample["persona_uuid"]
    assert len(sample["questions"]) == len(survey.questions)

    first = sample["questions"][0]
    assert [message["role"] for message in first["messages"]] == ["system", "user"]
    assert first["messages"][0]["content"] == survey.prompt.system
    # 実際に組み立てた [user] であること（見出しと設問文が入っている）。
    assert survey.prompt.headings.profile in first["messages"][1]["content"]
    assert survey.prompt.headings.question in first["messages"][1]["content"]

    markdown = (path.parent / "prompt_sample.md").read_text(encoding="utf-8")
    assert markdown.startswith("# 実際に送ったプロンプト")
    assert sample["persona_uuid"] in markdown
    # 本文はコードブロックに入れて Markdown として解釈させない。
    assert "```text" in markdown
    # 全設問ぶんが本文に出ること。1問だけ書き出して終わりにしない。
    for entry in sample["questions"]:
        assert entry["question_id"] in markdown
        assert entry["messages"][-1]["content"] in markdown


def test_prompt_sample_is_stable_across_runs(spark, personas_frame, tmp_path):
    """実行のたびに別のペルソナが出ると、比較の役に立たない。"""
    from persona_sim.metadata import build_metadata

    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    first = build_metadata(
        spark, survey, run_survey(spark, survey, storage, client=FakeClient()), storage
    )["prompt_sample"]
    second = build_metadata(
        spark, survey, run_survey(spark, survey, storage, client=FakeClient()), storage
    )["prompt_sample"]

    # 2回目は全セッションがスキップされるが、それでも記録は残る。
    assert second is not None
    assert first["persona_uuid"] == second["persona_uuid"]
    assert first["questions"] == second["questions"]


def test_prompt_sample_reflects_prompt_overrides(spark, personas_frame, tmp_path):
    """config の上書きが記録にも反映されること。"""
    from persona_sim.metadata import build_metadata

    data = _survey_dict()
    data["main_survey"]["prompt"] = {"system": "あなたは架空の人物です。"}
    survey = survey_from_dict(data)
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    result = run_survey(spark, survey, storage, client=FakeClient())

    sample = build_metadata(spark, survey, result, storage)["prompt_sample"]
    for entry in sample["questions"]:
        assert entry["messages"][0]["content"] == "あなたは架空の人物です。"


def test_prompt_sample_matches_what_was_actually_sent(spark, personas_frame, tmp_path):
    """組み直した記録が、実際にエンドポイントへ送った内容と一致すること。

    ここがずれると、記録を見て「こう聞いている」と判断した内容が実物と食い違う。
    """
    from persona_sim.metadata import build_metadata

    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    client = _RecordingClient()
    result = run_survey(spark, survey, storage, client=client)

    sample = build_metadata(spark, survey, result, storage)["prompt_sample"]
    assert client.sent
    for entry in sample["questions"]:
        assert _shape(entry) in client.sent


def test_prompt_sample_replays_remembered_answers(spark, personas_frame, tmp_path):
    """記憶を持つ設問は、実際に返ってきた回答を [assistant] として再生すること。

    ここを目印や省略で済ませると、記録のメッセージ列が実物と別物になる。
    """
    from persona_sim.metadata import build_metadata

    survey = _multi_slot_survey(memory="full_session")
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    client = _RecordingClient()
    result = run_survey(spark, survey, storage, client=client)

    sample = build_metadata(spark, survey, result, storage)["prompt_sample"]
    questions = sample["questions"]
    assert len(questions) == 6

    # 最後の設問は先行5設問ぶんの [user]/[assistant] を背負う。
    assert [message["role"] for message in questions[-1]["messages"]] == [
        "system", *["user", "assistant"] * 5, "user"
    ]
    for entry in questions:
        assert entry["missing_answers"] == []
        assert _shape(entry) in client.sent


# --------------------------------------------------------------------------- #
# 伝送層の再試行の記録（§6.5）
#
# `latency_ms` にはバックオフの待ち時間も含まれるので、これを分けて残していないと
# 「モデルが遅かった」のか「429 で眠っていた」のかを実行後に切り分けられない。
# --------------------------------------------------------------------------- #


class _RetryingClient:
    """`retry_stats()` を持つクライアント。回答そのものは fake に任せる。"""

    def __init__(self, stats: RetryStats) -> None:
        self._inner = FakeClient()
        self._stats = stats

    def describe(self) -> str:
        return self._inner.describe()

    def complete(self, messages, **kwargs):
        return self._inner.complete(messages, **kwargs)

    def retry_stats(self) -> RetryStats:
        return self._stats


def test_retry_stats_are_carried_into_the_result(spark, personas_frame, tmp_path):
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    stats = RetryStats(calls=200, retried_calls=2, retries=3, backoff_seconds=7.5)

    result = run_survey(spark, survey, storage, client=_RetryingClient(stats))

    assert result.retry_stats == stats


def test_a_retry_storm_is_surfaced_as_a_warning(spark, personas_frame, tmp_path):
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    stats = RetryStats(
        calls=200,
        retried_calls=80,
        retries=140,
        backoff_seconds=920.0,
        reasons={"429": 130, "503": 10},
    )

    result = run_survey(spark, survey, storage, client=_RetryingClient(stats))

    warning = next(w for w in result.warnings if "W_ENDPOINT_RETRY" in w)
    assert "40.0%" in warning
    assert "429 130回" in warning
    # 次の一手まで書く。数字だけ出しても何をすればよいか分からない。
    assert "concurrency" in warning


def test_a_healthy_endpoint_produces_no_retry_warning(spark, personas_frame, tmp_path):
    """平常時に警告が出ると、本当に詰まったときに見分けられなくなる。"""
    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = run_survey(spark, survey, storage, client=FakeClient())

    assert not [w for w in result.warnings if "W_ENDPOINT_RETRY" in w]
    # fake も統計は出せる（本番と同じ経路が通ることの確認）。
    assert result.retry_stats is not None
    assert result.retry_stats.retries == 0


def test_a_client_without_retry_stats_leaves_the_field_empty(spark, personas_frame, tmp_path):
    """`complete` しか持たないクライアントでも実行は通る。"""

    class _Bare:
        def __init__(self) -> None:
            self._inner = FakeClient()

        def describe(self) -> str:
            return "bare"

        def complete(self, messages, **kwargs):
            return self._inner.complete(messages, **kwargs)

    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)

    result = run_survey(spark, survey, storage, client=_Bare())

    assert result.retry_stats is None


def test_metadata_records_the_retry_breakdown(spark, personas_frame, tmp_path):
    from persona_sim.metadata import build_metadata

    survey = _survey()
    storage = _prepare(spark, personas_frame, tmp_path, survey)
    stats = RetryStats(
        calls=200, retried_calls=12, retries=19, backoff_seconds=64.25, reasons={"429": 19}
    )

    result = run_survey(spark, survey, storage, client=_RetryingClient(stats))
    recorded = build_metadata(spark, survey, result, storage)["model"]["endpoint_retries"]

    assert recorded == {
        "calls": 200,
        "retried_calls": 12,
        "retries": 19,
        "backoff_seconds": 64.2,
        "reasons": {"429": 19},
    }
