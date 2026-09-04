"""品質フラグの純関数（`persona_sim.run.flags`）。

判定そのものは Spark 側（`run/run.py::_apply_straightline`）にあるが、
**どの設問を対象にするか**はここで決まる。Spark 抜きで固定できるようにしてある。
"""

from __future__ import annotations

from persona_sim.panel.schema import Question, QuestionType
from persona_sim.run.flags import straightline_question_ids


def _question(question_id: str, question_type: QuestionType) -> Question:
    return Question(
        id=question_id,
        text="",
        type=question_type,
        options=("A", "B", "C") if question_type is not QuestionType.OPEN else (),
    )


def test_straightline_targets_single_and_scale():
    questions = [
        _question("q_single", QuestionType.SINGLE),
        _question("q_scale", QuestionType.SCALE),
    ]
    assert straightline_question_ids(questions) == ["q_single", "q_scale"]


def test_straightline_excludes_multi():
    """複数回答は「同一位置を選んだ」に潰せない。

    判定は `answer_codes` の先頭要素で行うので、`multi` を混ぜると
    `[1, 2, 3]` と `[1, 5]` が「どちらも 1」として同一視され、
    違う回答をした人に straightline が立つ。
    """
    questions = [
        _question("q_single", QuestionType.SINGLE),
        _question("q_multi", QuestionType.MULTI),
    ]
    assert straightline_question_ids(questions) == ["q_single"]


def test_straightline_excludes_open_and_numeric():
    """選択肢が無いので位置という概念が無い。"""
    questions = [
        _question("q_open", QuestionType.OPEN),
        _question("q_numeric", QuestionType.NUMERIC),
    ]
    assert straightline_question_ids(questions) == []


def test_straightline_keeps_the_definition_order():
    questions = [
        _question("q_b", QuestionType.SCALE),
        _question("q_multi", QuestionType.MULTI),
        _question("q_a", QuestionType.SINGLE),
    ]
    assert straightline_question_ids(questions) == ["q_b", "q_a"]
