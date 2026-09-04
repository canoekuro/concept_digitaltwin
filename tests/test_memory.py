"""設問ごとの記憶（`persona_sim.run.memory` と `questions[].remember`）。

見るのは3つ。

- 記憶の持たせ方でセッションの粒度が変わり、**従来の3つの形が再現できる**こと
- `remember` に列挙した設問だけが、ask order 順でプロンプトに再生されること
- 設問が黙って消える書き方は、読み込みか `validate` で止まること
"""

from __future__ import annotations

import pytest

from persona_sim.errors import SurveyDefinitionError
from persona_sim.llm.client import Completion
from persona_sim.llm.fake import FakeClient
from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.schema import StructuredOutput
from persona_sim.panel.validate import validate_static
from persona_sim.run.memory import plan_remembers, retains_memory, session_groups
from persona_sim.run.session import (
    SessionContext,
    StructuredOutputState,
    build_sessions,
    run_session,
)
from tests.conftest import base_survey_dict, with_remember
from tests.test_session import PERSONAS

PANEL_ROWS = [{"persona_uuid": "u1", "assigned_stimuli": ["c1", "c2", "c3"]}]

#: `base_survey_dict()` の設問。slot 1〜3 × (購入意向, 理由)。
INTENT = ("q_intent_1", "q_intent_2", "q_intent_3")
REASON = ("q_reason_1", "q_reason_2", "q_reason_3")


def _survey(remembers=None, *, shape="none", **design_overrides):
    """`shape` はかつて `design.memory` の3値で表していた形（`with_remember`）。

    `remembers` を渡すと、その設問だけ上書きする。
    """
    data = base_survey_dict()
    data["design"].update(design_overrides)
    with_remember(data, shape)
    for question in data["questions"]:
        if remembers and question["id"] in remembers:
            question["remember"] = remembers[question["id"]]
    return survey_from_dict(data)


def _codes(issues):
    return {issue.code for issue in issues}


# --------------------------------------------------------------------------- #
# design.memory の3値（回帰）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("shape", "expected_groups", "expected_sizes"),
    [
        ("none", 6, {1}),
        ("within_stimulus", 3, {2}),
        ("full_session", 1, {6}),
    ],
)
def test_the_three_classic_shapes_keep_their_session_split(shape, expected_groups, expected_sizes):
    """記憶の持たせ方でセッションの粒度が決まる（3つの形は `with_remember` が作る）。

    記憶なしは設問ごと、コンセプト内だけならコンセプトごと、全部覚えるなら丸ごと1つ。
    ここが変わると、同じ調査定義で呼び出し回数（＝費用）と順序効果の有無が変わる。
    """
    survey = _survey(shape=shape)
    groups = session_groups(survey)

    assert len(groups) == expected_groups
    assert {len(group) for group in groups} == expected_sizes
    assert len(build_sessions(survey, PANEL_ROWS)) == expected_groups


def test_listing_ids_keeps_the_memory_inside_one_concept():
    """コンセプト内だけに絞るのは、設問IDを並べて書く（専用の値は要らない）。"""
    plans = plan_remembers(_survey(shape="within_stimulus"))

    assert plans["q_reason_1"] == ("q_intent_1",)
    assert plans["q_intent_2"] == ()  # コンセプトが変わるのでリセット


def test_all_reaches_across_concepts():
    """`all` は「それまで全部」——コンセプトをまたぐ。意味は1つしかない。"""
    plans = plan_remembers(_survey(shape="full_session"))

    assert plans["q_intent_2"] == ("q_intent_1", "q_reason_1")


def test_all_does_not_depend_on_anything_outside_the_question():
    """`all` の意味は調査定義の他の設定に左右されない（回帰）。

    かつては調査全体の設定でコンセプト内／またぎが切り替わり、既定の組み合わせでは
    未定義のまま「またぐ」側に倒れていた。設定を消して意味を1つに固定した。
    """
    plain = plan_remembers(_survey({"q_intent_3": "all"}))
    rotated = plan_remembers(_survey({"q_intent_3": "all"}, rotation="random"))

    assert plain["q_intent_3"] == rotated["q_intent_3"]
    assert plain["q_intent_3"] == (
        "q_intent_1",
        "q_reason_1",
        "q_intent_2",
        "q_reason_2",
    )


# --------------------------------------------------------------------------- #
# remember の指定
# --------------------------------------------------------------------------- #


def test_remember_none_makes_the_question_its_own_session():
    """記憶を持たない設問は依存を持たないので単独で流せる（並列度が落ちない）。"""
    survey = _survey({"q_reason_1": "none"}, shape="within_stimulus")
    groups = session_groups(survey)

    assert ("q_reason_1",) in groups


def test_remember_is_not_transitive():
    """Q4→Q3、Q3→Q1 でも Q4 は Q1 を見ない。列挙どおり。"""
    survey = _survey(
        {
            "q_intent_1": "none",
            "q_reason_1": ["q_intent_1"],
            "q_intent_2": ["q_reason_1"],
        }
    )
    plans = plan_remembers(survey)

    assert plans["q_reason_1"] == ("q_intent_1",)
    assert plans["q_intent_2"] == ("q_reason_1",)  # q_intent_1 は入らない


def test_remember_skips_over_intervening_questions():
    """非連続な参照。間の設問を飛ばして前の設問だけを覚える。"""
    survey = _survey({"q_intent_2": ["q_intent_1"]})
    plans = plan_remembers(survey)

    assert plans["q_intent_2"] == ("q_intent_1",)


def test_remember_is_replayed_in_ask_order_not_written_order():
    """書いた順ではなく聞いた順に再生する。会話履歴として時系列が狂わないように。"""
    survey = _survey({"q_reason_2": ["q_intent_2", "q_intent_1"]})

    assert plan_remembers(survey)["q_reason_2"] == ("q_intent_1", "q_intent_2")


def test_questions_joined_by_remember_share_one_session():
    """記憶で繋がった設問は同じセッションに入る。先に答えが要るため。"""
    survey = _survey({"q_reason_2": ["q_intent_1", "q_intent_2"]})
    groups = session_groups(survey)

    joined = next(group for group in groups if "q_reason_2" in group)
    assert joined == ("q_intent_1", "q_intent_2", "q_reason_2")


def test_retains_memory_sees_question_level_settings():
    """記憶の有無は設問側の指定だけで決まる（順序効果の判定に効く）。"""
    assert not retains_memory(_survey())
    assert retains_memory(_survey({"q_reason_1": ["q_intent_1"]}))


# --------------------------------------------------------------------------- #
# プロンプトの組み立て
# --------------------------------------------------------------------------- #


class _Spy(FakeClient):
    """送ったメッセージ列を全部控えるクライアント。"""

    def __init__(self):
        super().__init__()
        self.sent: list[list] = []

    def complete(self, messages, **kwargs) -> Completion:
        self.sent.append(list(messages))
        return super().complete(messages, **kwargs)


def _run(survey, panel_rows=PANEL_ROWS):
    client = _Spy()
    ctx = SessionContext(
        survey=survey,
        client=client,
        personas=PERSONAS,
        stimuli={stimulus.id: stimulus for stimulus in survey.stimuli},
        structured_output=StructuredOutputState(mode=StructuredOutput.NEVER),
    )
    for session in build_sessions(survey, panel_rows):
        run_session(session, ctx)
    return client.sent


def _texts(messages) -> list[str]:
    return [
        message.content if isinstance(message.content, str) else str(message.content)
        for message in messages
    ]


def test_the_profile_appears_exactly_once_in_a_replayed_conversation():
    """回答済みのターンを組み直しても、ペルソナカードは先頭の1回だけ。"""
    survey = _survey({"q_reason_2": ["q_intent_1", "q_intent_2"]})
    longest = max(_run(survey), key=len)

    assert sum(text.count("■あなたのプロフィール") for text in _texts(longest)) == 1


def test_a_concept_is_presented_once_per_replayed_conversation():
    """同じ案の提示文は繰り返さず、案が変わるターンでだけ挟む。"""
    survey = _survey({"q_reason_2": ["q_intent_1", "q_intent_2"]})
    longest = max(_run(survey), key=len)
    joined = "\n".join(_texts(longest))

    assert joined.count("内容A") == 1
    assert joined.count("内容B") == 1


def test_an_unremembered_question_is_absent_from_the_prompt():
    """覚えていない設問は、間に聞いていてもプロンプトに入らない。"""
    survey = _survey(
        {"q_intent_2": "none", "q_reason_2": ["q_intent_2"]}, shape="within_stimulus"
    )
    for messages in _run(survey):
        texts = _texts(messages)
        if "理由は。" in texts[-1]:
            # 直前に聞いた q_reason_1 は覚えていないので出てこない。
            assert sum(text.count("理由は。") for text in texts) == 1


def test_a_question_without_memory_sends_the_same_prompt_as_before():
    """記憶を持たない設問の入力は [system] + [user] の2通のまま（回帰）。"""
    survey = _survey()

    assert all(len(messages) == 2 for messages in _run(survey))


# --------------------------------------------------------------------------- #
# 検証（E6）
# --------------------------------------------------------------------------- #


def test_unknown_question_id_in_remember_is_rejected():
    report = validate_static(_survey({"q_reason_1": ["q_nope"]}))
    assert "E6" in _codes(report.errors)


def test_self_reference_is_rejected():
    report = validate_static(_survey({"q_reason_1": ["q_reason_1"]}))
    assert "E6" in _codes(report.errors)


def test_forward_reference_is_rejected():
    """まだ答えていない設問の記憶は持てない。"""
    report = validate_static(_survey({"q_intent_1": ["q_reason_3"]}))
    assert "E6" in _codes(report.errors)


def test_a_missing_slot_is_rejected_at_load_time():
    """提示されるのに1問も聞かれないコンセプトを作らせない。

    **読み込みで止める。** `run` は `validate` を通らずに実行できるので、`validate`
    だけに置くと素通りして、そのコンセプトが評価枠を消費したまま無言で消える。
    """
    data = base_survey_dict()
    data["questions"] = [q for q in data["questions"] if q["slot"] != 2]

    with pytest.raises(SurveyDefinitionError, match="slot"):
        survey_from_dict(data)


def test_a_slot_beyond_the_presented_concepts_is_rejected_at_load_time():
    data = base_survey_dict()
    data["questions"][0]["slot"] = 9

    with pytest.raises(SurveyDefinitionError, match="範囲外"):
        survey_from_dict(data)


def test_a_duplicate_question_id_is_rejected_at_load_time():
    """重複したIDは実行計画で畳まれ、1問まるごと聞かれずに消える。"""
    data = base_survey_dict()
    data["questions"][2]["id"] = data["questions"][0]["id"]

    with pytest.raises(SurveyDefinitionError, match="重複"):
        survey_from_dict(data)


def test_mismatched_top_box_under_one_measure_is_rejected():
    """T2B は代表1つの定義で全コンセプトぶん計算するので、揃っていないと数字が狂う。"""
    data = base_survey_dict()
    data["questions"][2]["top_box"] = [1]  # q_intent_2 だけ T2B の定義が違う
    report = validate_static(survey_from_dict(data))

    assert "E6" in _codes(report.errors)


def test_an_out_of_range_slot_fails_legibly_at_run_time():
    """`run` は `validate` を通らずに実行できるので、範囲外の slot をここでも止める。

    素の IndexError だと、パネル全員ぶんのセッションが「list index out of range」で
    落ちるだけで、調査定義のどこが悪いのか読み取れない。
    """
    from persona_sim.run.memory import stimulus_for

    survey = _survey()
    question = next(q for q in survey.questions if q.slot == 3)
    stimuli = {stimulus.id: stimulus for stimulus in survey.stimuli}

    with pytest.raises(SurveyDefinitionError, match="範囲外"):
        stimulus_for(question, ["c1"], stimuli, simultaneous=False)


def test_mismatched_options_under_one_measure_are_rejected():
    """同じ measure は1つの表に束ねるので、選択肢が揃っていないと表頭が作れない。"""
    data = base_survey_dict()
    data["questions"][2]["options"] = ["はい", "いいえ"]  # q_intent_2 だけ選択肢が違う
    report = validate_static(survey_from_dict(data))

    assert "E6" in _codes(report.errors)


def test_legacy_questions_without_slot_are_rejected():
    """旧形式（全コンセプトに繰り返すテンプレート）は読まずに止める。

    slot の既定 1 で黙って読むと、2つ目以降のコンセプトが聞かれずに消える。
    """
    data = base_survey_dict()
    data["questions"] = [
        {"id": "q_only", "text": "買いたいですか。", "type": "single", "options": ["はい", "いいえ"]}
    ]
    with pytest.raises(SurveyDefinitionError, match="slot"):
        survey_from_dict(data)


def test_legacy_questions_are_fine_when_only_one_concept_is_shown():
    """m == 1 なら旧形式と新形式の展開が一致するので、そのまま読んでよい。"""
    data = base_survey_dict()
    data["design"]["sample_overlap"] = "disjoint"
    data["questions"] = [
        {"id": "q_only", "text": "買いたいですか。", "type": "single", "options": ["はい", "いいえ"]}
    ]

    assert validate_static(survey_from_dict(data)).ok
