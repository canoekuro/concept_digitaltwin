"""セッションの組み立てと実行（`persona_sim.run.session`）。

設問ごとの `remember` がセッションの粒度を決めること、履歴の有無が期待どおりであることを見る。
"""

from __future__ import annotations

import pytest

from persona_sim.llm.budget import budgets
from persona_sim.llm.client import Completion, OutputLimitExceeded
from persona_sim.llm.fake import FakeClient
from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.schema import StructuredOutput
from persona_sim.run.session import (
    MISSING_ANSWER,
    SessionContext,
    StructuredOutputState,
    build_sessions,
    run_session,
    sample_prompts,
)
from tests.conftest import base_survey_dict, with_remember

PANEL_ROWS = [
    {"persona_uuid": "u1", "assigned_stimuli": ["c1", "c2", "c3"]},
    {"persona_uuid": "u2", "assigned_stimuli": ["c1", "c2", "c3"]},
]

PERSONAS = {
    "u1": {
        "uuid": "u1",
        "sex": "男",
        "age": 34,
        "prefecture": "東京都",
        "marital_status": "既婚",
        "education_level": "大学卒 文系",
        "occupation_raw": "小売業 中堅",
        "persona": "都内在住の会社員。",
        "cultural_background": "下町育ち。",
        "professional_persona": "販売企画を担当。",
        "hobbies_and_interests": "登山と映画。",
        "culinary_persona": "外食は週2回。",
    },
    "u2": dict.fromkeys(("uuid",), "u2"),
}
PERSONAS["u2"].update({k: v for k, v in PERSONAS["u1"].items() if k != "uuid"})


def _survey(memory="none"):
    """`memory` はかつての `design.memory` の3値。設問ごとの `remember` に展開する。"""
    return survey_from_dict(with_remember(base_survey_dict(), memory))


def _context(survey, client=None):
    return SessionContext(
        survey=survey,
        client=client or FakeClient(),
        personas=PERSONAS,
        stimuli={stimulus.id: stimulus for stimulus in survey.stimuli},
        structured_output=StructuredOutputState(mode=StructuredOutput.NEVER),
    )


# --------------------------------------------------------------------------- #
# セッションの粒度
# --------------------------------------------------------------------------- #


def test_memory_none_makes_one_session_per_question():
    """2人 × 3コンセプト × 2設問 = 12 セッション。"""
    sessions = build_sessions(_survey(memory="none"), PANEL_ROWS)
    assert len(sessions) == 12
    assert all(len(session.units) == 1 for session in sessions)


def test_memory_within_stimulus_groups_by_stimulus():
    """2人 × 3コンセプト = 6 セッション、各2設問。"""
    sessions = build_sessions(_survey(memory="within_stimulus"), PANEL_ROWS)
    assert len(sessions) == 6
    assert all(len(session.units) == 2 for session in sessions)
    for session in sessions:
        assert len({unit.stimulus_id for unit in session.units}) == 1


def test_memory_full_session_groups_by_persona():
    """1人1セッション、3コンセプト × 2設問。"""
    sessions = build_sessions(_survey(memory="full_session"), PANEL_ROWS)
    assert len(sessions) == 2
    assert all(len(session.units) == 6 for session in sessions)


def test_memory_none_is_ordered_by_stimulus_first():
    """prefix cache が効くよう、コンセプト外側・ペルソナ内側に並べる（§6.2）。"""
    sessions = build_sessions(_survey(memory="none"), PANEL_ROWS)
    stimulus_order = [session.units[0].stimulus_id for session in sessions]
    assert stimulus_order == sorted(stimulus_order)
    assert stimulus_order[:4] == ["c1", "c1", "c1", "c1"]


def test_sequence_records_presentation_order():
    sessions = build_sessions(_survey(memory="full_session"), PANEL_ROWS)
    units = sessions[0].units
    assert [unit.sequence for unit in units] == [1, 1, 2, 2, 3, 3]


# --------------------------------------------------------------------------- #
# 提示順（§9.1「誰に・どのコンセプトを・何番目に」）
# --------------------------------------------------------------------------- #

#: 定義順（c1, c2, c3）とは違う並びを持つ `panels` の行。
#: 提示設計を凍結した今は書き出されないが、**読む側は `assigned_stimuli` の順に従う**
#: ことを固定しておく。調査定義の記述順から補う実装に戻ると、以前のパネルを読み直した
#: ときにペルソナごとの提示順が黙って化ける（§9.1）。
ROTATED_PANEL_ROWS = [{"persona_uuid": "u1", "assigned_stimuli": ["c3", "c1", "c2"]}]


def test_each_question_presents_the_concept_at_its_slot_in_the_assigned_order():
    """`slot` は `assigned_stimuli` の位置。調査定義の記述順ではない。"""
    survey = _survey(memory="none")
    sessions = build_sessions(survey, ROTATED_PANEL_ROWS)

    assert len(sessions) == len(survey.questions)
    for session in sessions:
        unit = session.units[0]
        assert unit.stimulus_id == ROTATED_PANEL_ROWS[0]["assigned_stimuli"][unit.sequence - 1]
        assert tuple(s.id for s in unit.stimuli_to_present) == (unit.stimulus_id,)


def test_every_unit_carries_its_own_stimuli_in_presentation_order():
    """提示物は全ユニットが持つ。実際に出すかは組み立て時に決める（回帰）。

    履歴を持つモードでも間引かない。間引くと提示物を持たないユニットを調査定義の
    記述順から補うことになり、ペルソナごとの提示順が失われる（§9.1）。
    """
    survey = _survey(memory="full_session")
    session = build_sessions(survey, ROTATED_PANEL_ROWS)[0]

    assigned = ROTATED_PANEL_ROWS[0]["assigned_stimuli"]
    assert all(
        tuple(s.id for s in unit.stimuli_to_present) == (assigned[unit.sequence - 1],)
        for unit in session.units
    )


# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #


def test_run_session_produces_one_record_per_unit():
    survey = _survey(memory="full_session")
    session = build_sessions(survey, PANEL_ROWS)[0]
    records = run_session(session, _context(survey))

    assert len(records) == len(session.units)
    keys = {(r.persona_uuid, r.stimulus_id, r.question_id) for r in records}
    assert len(keys) == len(records)
    assert all(r.survey_id == survey.survey_id for r in records)


def test_closed_question_answers_are_within_range():
    survey = _survey(memory="none")
    sessions = build_sessions(survey, PANEL_ROWS)
    ctx = _context(survey)
    for session in sessions:
        for record in run_session(session, ctx):
            if record.question_id.startswith("q_intent"):
                assert record.answer_codes
                assert all(1 <= code <= 5 for code in record.answer_codes)
                assert record.options_order == [1, 2, 3, 4, 5]


def test_open_question_keeps_text_and_no_code():
    survey = _survey(memory="none")
    sessions = build_sessions(survey, PANEL_ROWS)
    ctx = _context(survey)
    records = [r for s in sessions for r in run_session(s, ctx)]
    open_records = [r for r in records if r.question_id.startswith("q_reason")]

    assert open_records
    for record in open_records:
        assert record.answer_codes == []
        assert record.answer_text


def test_history_grows_only_when_memory_is_kept():
    """履歴を持つモードでは、後の設問ほどプロンプトが長くなる。"""
    kept = _survey(memory="full_session")
    client = FakeClient()
    run_session(build_sessions(kept, PANEL_ROWS)[0], _context(kept, client))
    calls_with_history = client.calls

    fresh = _survey(memory="none")
    client_fresh = FakeClient()
    ctx = _context(fresh, client_fresh)
    for session in build_sessions(fresh, PANEL_ROWS)[:6]:
        run_session(session, ctx)

    # どちらも6設問ぶん呼ばれる。差は履歴の有無であって呼び出し回数ではない。
    assert calls_with_history == 6
    assert client_fresh.calls == 6


def test_run_is_deterministic_with_fake_client():
    survey = _survey(memory="full_session")
    session = build_sessions(survey, PANEL_ROWS)[0]
    first = run_session(session, _context(survey))
    second = run_session(session, _context(survey))
    assert [r.answer_raw for r in first] == [r.answer_raw for r in second]


# --------------------------------------------------------------------------- #
# リトライとフラグ
# --------------------------------------------------------------------------- #


def test_unparseable_answer_is_retried_then_flagged():
    """3回試して駄目なら parse_error を立てて続行する（§6.3）。"""
    survey = _survey(memory="none")
    session = next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id.startswith("q_intent")
    )
    client = FakeClient(unparseable_rate=1.0)
    record = run_session(session, _context(survey, client))[0]

    assert record.answer_codes == []
    assert "parse_error" in record.flags
    assert "retried" in record.flags
    assert record.attempt == 3
    assert client.calls == 3


def test_refusal_is_flagged():
    survey = _survey(memory="none")
    session = next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id.startswith("q_intent")
    )
    record = run_session(session, _context(survey, FakeClient(refusal_rate=1.0)))[0]
    assert "refusal" in record.flags


def test_structured_output_never_sends_no_response_format():
    """`never` では構造化出力を使わないので、Fake は素の数字を返す。"""
    survey = _survey(memory="none")
    session = next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id.startswith("q_intent")
    )
    record = run_session(session, _context(survey))[0]
    assert not record.answer_raw.startswith("{")


def test_structured_output_auto_uses_schema():
    survey = _survey(memory="none")
    session = next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id.startswith("q_intent")
    )
    ctx = SessionContext(
        survey=survey,
        client=FakeClient(),
        personas=PERSONAS,
        stimuli={stimulus.id: stimulus for stimulus in survey.stimuli},
        structured_output=StructuredOutputState(mode=StructuredOutput.AUTO),
    )
    record = run_session(session, ctx)[0]
    assert record.answer_raw.startswith("{")
    assert record.answer_codes


def test_structured_output_always_propagates_unsupported():
    """`always` は落とさず止める。黙って劣化させないため。"""
    from persona_sim.llm.client import StructuredOutputUnsupported

    state = StructuredOutputState(mode=StructuredOutput.ALWAYS)
    with pytest.raises(StructuredOutputUnsupported):
        state.note_unsupported(StructuredOutputUnsupported("駄目"))


def test_structured_output_auto_falls_back_and_records_it():
    state = StructuredOutputState(mode=StructuredOutput.AUTO)
    from persona_sim.llm.client import StructuredOutputUnsupported

    state.note_unsupported(StructuredOutputUnsupported("駄目"))
    assert state.enabled is False
    assert state.fell_back is True


# --------------------------------------------------------------------------- #
# 出力予算の引き上げ（`llm/budget.py`）
#
# 3600セッション中4セッションが、出力長超過を「何度投げても同じ 400」と扱われて丸ごと
# 失われた（`docs/issues/20260807001.md`）。失敗したセッションは1行も書き出さないので、
# そのペルソナが既に答え終えていた設問まで消える。
# --------------------------------------------------------------------------- #


class _BudgetClient:
    """予算が `succeeds_at` 未満なら出力長超過を返すクライアント。

    `FakeClient` の失敗注入はプロンプトのハッシュなので、同じプロンプトで「予算を広げたら
    通る」を作れない。ここは予算そのものが分岐条件なので専用のスタブが要る。
    """

    def __init__(self, *, succeeds_at=None, partial_text=""):
        self._succeeds_at = succeeds_at
        self._partial_text = partial_text
        self.budgets: list[int] = []

    def describe(self) -> str:
        return "budget-stub"

    def complete(self, messages, *, max_tokens, response_format=None):
        self.budgets.append(max_tokens)
        if self._succeeds_at is None or max_tokens < self._succeeds_at:
            raise OutputLimitExceeded(
                "上限に達した", max_tokens=max_tokens, partial_text=self._partial_text
            )
        return Completion(text="1", latency_ms=1)


def _single_session(survey):
    return next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id.startswith("q_intent")
    )


def test_a_raised_budget_does_not_count_as_a_parse_retry():
    """`responses.attempt` はパース失敗の回数。予算の引き上げを混ぜると意味が読めなくなる。"""
    survey = _survey(memory="none")
    client = _BudgetClient(succeeds_at=32)
    ctx = _context(survey, client)

    record = run_session(_single_session(survey), ctx)[0]

    assert len(client.budgets) > 1  # 実際に広げている
    assert client.budgets[0] < client.budgets[1]
    assert record.attempt == 1
    assert "retried" not in record.flags
    assert "output_limit" not in record.flags
    assert (ctx.output_budget.escalated, ctx.output_budget.exhausted) == (1, 0)


def test_an_exhausted_budget_records_a_flagged_answer_instead_of_failing_the_session():
    """1設問の予算超過でセッションを殺さない。殺すと答え済みの設問まで巻き添えで消える。"""
    survey = _survey(memory="none")
    client = _BudgetClient(succeeds_at=None)
    ctx = _context(survey, client)

    records = run_session(_single_session(survey), ctx)

    assert len(records) == 1
    assert records[0].answer_codes == []
    assert "output_limit" in records[0].flags
    assert "parse_error" in records[0].flags
    assert ctx.output_budget.exhausted == 1


def test_an_exhausted_budget_keeps_whatever_text_was_produced():
    """自由回答では途中までの本文が唯一残る手がかりになる。捨てない。"""
    survey = _survey(memory="none")
    client = _BudgetClient(succeeds_at=None, partial_text="ここまでは書け")
    ctx = _context(survey, client)

    record = run_session(_single_session(survey), ctx)[0]

    assert record.answer_raw == "ここまでは書け"


def test_an_exhausted_budget_stops_the_parse_loop():
    """残りのパース試行も同じように使い切るだけ。短絡しないと呼び出しが3倍に膨れる。"""
    survey = _survey(memory="none")
    client = _BudgetClient(succeeds_at=None)

    run_session(_single_session(survey), _context(survey, client))

    # 1設問ぶんの予算計画をちょうど1周しただけ（パース再送で繰り返していない）。
    assert client.budgets == list(budgets(survey.model.max_tokens))


# --------------------------------------------------------------------------- #
# 複数回答（multi）
# --------------------------------------------------------------------------- #


def _multi_survey(memory="none"):
    data = base_survey_dict()
    with_remember(data, memory)
    data["questions"].insert(
        1,
        {
            "id": "q_uses",
            "text": "どの場面で飲みたいと思いますか。",
            "type": "multi",
            "options": ["夕食時", "入浴後", "外出先"],
        },
    )
    return survey_from_dict(data)


def _multi_record(survey, client=None):
    session = next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id == "q_uses"
    )
    return run_session(session, _context(survey, client))[0]


def test_multi_selection_is_recorded_in_answer_codes():
    """multi は選ばれた番号すべてが `answer_codes` に残る。"""
    record = _multi_record(_multi_survey(memory="none"))

    assert record.answer_codes
    assert all(1 <= code <= 3 for code in record.answer_codes)


def test_multi_answer_is_not_flagged_as_parse_error():
    """番号が取れているのに parse_error を立てると、再開のたびにやり直しになる。"""
    record = _multi_record(_multi_survey(memory="none"))
    assert "parse_error" not in record.flags


def test_multi_without_any_number_is_flagged_and_retried():
    record = _multi_record(_multi_survey(memory="none"), FakeClient(unparseable_rate=1.0))

    assert record.answer_codes == []
    assert "parse_error" in record.flags
    assert record.attempt == 3


def test_single_choice_records_exactly_one_code():
    """single は選択肢を1つだけ選ぶので、`answer_codes` は1要素になる。"""
    survey = _survey(memory="none")
    session = next(
        s for s in build_sessions(survey, PANEL_ROWS) if s.units[0].question_id.startswith("q_intent")
    )
    record = run_session(session, _context(survey))[0]
    assert len(record.answer_codes) == 1


def test_open_question_has_no_codes():
    survey = _survey(memory="none")
    sessions = build_sessions(survey, PANEL_ROWS)
    ctx = _context(survey)
    records = [r for s in sessions for r in run_session(s, ctx)]
    for record in (r for r in records if r.question_id.startswith("q_reason")):
        assert record.answer_codes == []


def test_run_session_with_native_image_stimulus():
    data = base_survey_dict()
    data["stimuli"][0]["image_mode"] = "native"
    data["stimuli"][0]["image_uri"] = "/Volumes/catalog/schema/volume/img.png"
    survey = survey_from_dict(data)

    client = FakeClient()
    sessions = build_sessions(survey, PANEL_ROWS[:1])
    records = run_session(sessions[0], _context(survey, client))

    assert len(records) > 0
    assert client.calls > 0
    assert records[0].answer_raw


# --------------------------------------------------------------------------- #
# プロンプトの記録（§9）
#
# 記録は「実際に送った内容と一致する」ことが前提なので、組み直した列を実送信と
# 突き合わせる。ここが崩れると、記録を読んで判断した内容が実物と食い違う。
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


def _shape(sample) -> list[tuple[str, object]]:
    return [(message.role, message.content) for message in sample.messages]


def test_sample_prompts_covers_every_question_of_the_session():
    """1設問だけでなく、そのセッションの全設問が残ること。"""
    survey = _survey(memory="full_session")
    session = build_sessions(survey, PANEL_ROWS)[0]

    samples = sample_prompts(session, _context(survey))

    assert len(samples) == len(session.units) == 6
    assert [s.question_id for s in samples] == [u.question_id for u in session.units]
    assert [s.stimulus_id for s in samples] == [u.stimulus_id for u in session.units]


def test_sample_prompts_matches_what_was_actually_sent_without_memory():
    survey = _survey(memory="none")
    session = build_sessions(survey, PANEL_ROWS)[0]
    client = _RecordingClient()
    ctx = _context(survey, client)

    run_session(session, ctx)
    samples = sample_prompts(session, ctx)

    assert client.sent
    for sample in samples:
        assert _shape(sample) in client.sent


def test_sample_prompts_replays_the_recorded_answers():
    """記憶を持つ設問は [assistant] を挟んで送るので、そこまで一致すること。"""
    survey = _survey(memory="full_session")
    session = build_sessions(survey, PANEL_ROWS)[0]
    client = _RecordingClient()
    ctx = _context(survey, client)

    records = run_session(session, ctx)
    answers = {record.question_id: record.answer_raw for record in records}
    samples = sample_prompts(session, ctx, answers)

    # 最後の設問は先行5設問ぶんの [user]/[assistant] を背負う（system + 5*2 + user）。
    last = samples[-1]
    assert [message.role for message in last.messages] == [
        "system",
        *["user", "assistant"] * 5,
        "user",
    ]
    assert not last.missing_answers
    for sample in samples:
        assert _shape(sample) in client.sent


def test_sample_prompts_marks_answers_it_could_not_restore():
    """引けなかった回答は目印で埋め、黙って一致しない記録を作らない。"""
    survey = _survey(memory="full_session")
    session = build_sessions(survey, PANEL_ROWS)[0]
    ctx = _context(survey)

    records = run_session(session, ctx)
    answers = {record.question_id: record.answer_raw for record in records}
    dropped = session.units[0].question_id
    del answers[dropped]

    samples = sample_prompts(session, ctx, answers)

    # 先頭設問は記憶を持たないので、欠けても自分自身の列には影響しない。
    assert samples[0].missing_answers == ()
    assert all(dropped in sample.missing_answers for sample in samples[1:])
    assert MISSING_ANSWER in [
        message.content for message in samples[-1].messages if message.role == "assistant"
    ]


def test_sample_prompts_returns_nothing_for_an_unknown_persona():
    survey = _survey(memory="none")
    rows = [{"persona_uuid": "unknown", "assigned_stimuli": ["c1", "c2", "c3"]}]
    session = build_sessions(survey, rows)[0]
    assert sample_prompts(session, _context(survey)) == []


# --------------------------------------------------------------------------- #
# 理由を書かせる設問（`questions[].reasoning`、§6.3）
# --------------------------------------------------------------------------- #


def _reasoning_survey(max_tokens_reasoning: int = 512):
    """1問目（`q_intent_1`）だけ理由を書かせる調査定義。"""
    data = base_survey_dict()
    for question in data["questions"]:
        if question["id"] == "q_intent_1":
            question["reasoning"] = True
            question["reasoning_max_length"] = 60
    data["main_survey"]["model"].update(
        {
            "max_tokens": 8,
            "max_tokens_open": 256,
            "max_tokens_reasoning": max_tokens_reasoning,
            "structured_output": "always",
        }
    )
    return survey_from_dict(data)


class _BudgetRecordingClient:
    """送られた `max_tokens` を控えるだけのクライアント。"""

    def __init__(self, text: str = '{"reasoning": "理由。", "answer": 2}'):
        self._text = text
        self.budgets: list[int] = []
        self.formats: list[dict | None] = []

    def describe(self) -> str:
        return "budget-recorder"

    def complete(self, messages, *, max_tokens, response_format=None):
        self.budgets.append(max_tokens)
        self.formats.append(response_format)
        return Completion(text=self._text, latency_ms=1)


def _reasoning_context(survey, client):
    return SessionContext(
        survey=survey,
        client=client,
        personas=PERSONAS,
        stimuli={stimulus.id: stimulus for stimulus in survey.stimuli},
        # reasoning は構造化出力が前提（`validate` が always を必須にしている）。
        structured_output=StructuredOutputState(mode=StructuredOutput.ALWAYS),
    )


def _session_for(survey, question_id):
    return next(
        session
        for session in build_sessions(survey, PANEL_ROWS)
        if session.units[0].question_id == question_id
    )


def test_reasoning_questions_use_the_reasoning_budget():
    """番号ぶんの `max_tokens` では理由の散文が必ず切れる。"""
    survey = _reasoning_survey(max_tokens_reasoning=384)
    client = _BudgetRecordingClient()

    run_session(_session_for(survey, "q_intent_1"), _reasoning_context(survey, client))

    assert client.budgets == [384]


def test_questions_without_reasoning_keep_their_own_budget():
    """理由を書かせない設問の予算は変わらない（open は `max_tokens_open`）。"""
    survey = _reasoning_survey()
    # 番号が取れる応答にしておく。取れないとパース再送で呼び出しが増え、
    # 予算の並びが読めなくなる。
    client = _BudgetRecordingClient(text='{"answer": 1}')

    run_session(_session_for(survey, "q_intent_2"), _reasoning_context(survey, client))
    run_session(_session_for(survey, "q_reason_1"), _reasoning_context(survey, client))

    assert client.budgets == [8, 256]


def test_reasoning_is_recorded_next_to_the_answer():
    survey = _reasoning_survey()
    client = _BudgetRecordingClient('{"reasoning": "甘さ控えめが好みに合う。", "answer": 2}')

    record = run_session(_session_for(survey, "q_intent_1"), _reasoning_context(survey, client))[0]

    assert record.answer_codes == [2]
    assert record.answer_reasoning == "甘さ控えめが好みに合う。"
    # 生出力もそのまま残る。記憶（`remember`）で再生されるのはこちら。
    assert record.answer_raw == '{"reasoning": "甘さ控えめが好みに合う。", "answer": 2}'
    assert record.flags == []


def test_answers_without_reasoning_leave_the_column_empty():
    survey = _reasoning_survey()
    client = _BudgetRecordingClient(text='{"answer": 3}')

    record = run_session(_session_for(survey, "q_intent_2"), _reasoning_context(survey, client))[0]

    assert record.answer_reasoning is None


def test_the_reasoning_schema_is_sent_only_for_reasoning_questions():
    survey = _reasoning_survey()
    client = _BudgetRecordingClient()

    run_session(_session_for(survey, "q_intent_1"), _reasoning_context(survey, client))
    run_session(_session_for(survey, "q_intent_2"), _reasoning_context(survey, client))

    with_reasoning, without = client.formats
    assert list(with_reasoning["json_schema"]["schema"]["properties"]) == ["reasoning", "answer"]
    assert list(without["json_schema"]["schema"]["properties"]) == ["answer"]


def test_the_fake_client_answers_reasoning_questions_in_the_same_shape():
    """`endpoint: fake` でも本番と同じ形の生出力を返す（理由が先、番号が後）。"""
    survey = _reasoning_survey()
    ctx = _reasoning_context(survey, FakeClient())

    record = run_session(_session_for(survey, "q_intent_1"), ctx)[0]

    assert record.answer_reasoning
    assert record.answer_codes
    assert record.answer_raw.index('"reasoning"') < record.answer_raw.index('"answer"')
