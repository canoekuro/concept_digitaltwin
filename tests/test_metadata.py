"""実行メタデータの人が読む側（`persona_sim.metadata`）。

`prompt_sample.md` は、実行後に「結局どう聞いたのか」を確かめる唯一の読み物
（`SPEC.md` §9）。Spark を起動せずに組み立てだけを見る。
"""

from __future__ import annotations

from persona_sim.metadata import render_prompt_sample
from persona_sim.panel.loader import survey_from_dict
from persona_sim.run.session import MISSING_ANSWER
from tests.conftest import base_survey_dict


def _survey():
    """`survey_id` を表に出すためだけの最小の定義。コンセプトと slot は1件に揃える。"""
    data = base_survey_dict(slots=1)
    data["stimuli"] = [{"id": "c1", "name": "コンセプトA", "text": "内容A"}]
    return survey_from_dict(data)


def _sample(**overrides):
    entry = {
        "stimulus_id": "c1",
        "question_id": "q_intent_1",
        "sequence": 1,
        "messages": [
            {"role": "system", "content": "あなたは次の人物です。"},
            {"role": "user", "content": "■あなたのプロフィール\n34歳・男性"},
        ],
        "missing_answers": [],
    }
    entry.update(overrides)
    return {"persona_uuid": "p0001", "questions": [entry]}


def test_every_question_is_rendered_with_its_messages():
    """1問だけ書き出して終わりにしない（§9）。"""
    sample = {
        "persona_uuid": "p0001",
        "questions": [
            {
                "stimulus_id": "c1",
                "question_id": "q_intent_1",
                "sequence": 1,
                "messages": [
                    {"role": "system", "content": "SYSTEM本文"},
                    {"role": "user", "content": "USER1本文"},
                ],
                "missing_answers": [],
            },
            {
                "stimulus_id": "c1",
                "question_id": "q_reason_1",
                "sequence": 1,
                "messages": [
                    {"role": "system", "content": "SYSTEM本文"},
                    {"role": "user", "content": "USER1本文"},
                    {"role": "assistant", "content": "ASSISTANT本文"},
                    {"role": "user", "content": "USER2本文"},
                ],
                "missing_answers": [],
            },
        ],
    }

    markdown = render_prompt_sample(_survey(), sample)

    assert markdown.startswith("# 実際に送ったプロンプト")
    assert "| 設問数 | 2 |" in markdown
    for body in ("USER1本文", "ASSISTANT本文", "USER2本文"):
        assert body in markdown
    for heading in ("## 1. q_intent_1", "## 2. q_reason_1", "### [assistant]"):
        assert heading in markdown
    # プロンプト本文は `■` や `【】` を含むので、コードブロックに入れる。
    assert "```text" in markdown


def test_a_restored_answer_that_is_missing_is_called_out():
    """目印で埋めたことを黙っていると、実物と一致しない記録が一致して見える。"""
    sample = _sample(
        question_id="q_reason_1",
        messages=[
            {"role": "system", "content": "SYSTEM本文"},
            {"role": "user", "content": "USER1本文"},
            {"role": "assistant", "content": MISSING_ANSWER},
            {"role": "user", "content": "USER2本文"},
        ],
        missing_answers=["q_intent_1"],
    )

    markdown = render_prompt_sample(_survey(), sample)

    assert "`q_intent_1`" in markdown
    assert "実際に送った文面とは一致しない" in markdown


def test_no_note_when_every_answer_was_restored():
    markdown = render_prompt_sample(_survey(), _sample())
    assert "一致しない" not in markdown


def test_native_image_parts_are_shown_as_a_placeholder():
    """画像は URI だけ残す。本文に生の dict を落とすと読めない。"""
    sample = _sample(
        messages=[
            {"role": "system", "content": "SYSTEM本文"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "USER本文"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "/Volumes/catalog/schema/volume/img.png"},
                    },
                ],
            },
        ]
    )

    markdown = render_prompt_sample(_survey(), sample)

    assert "USER本文" in markdown
    assert "[Image: /Volumes/catalog/schema/volume/img.png]" in markdown
