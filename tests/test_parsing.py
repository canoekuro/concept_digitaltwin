"""出力の強制とパース（`persona_sim.run.parsing`）。"""

from __future__ import annotations

import pytest

from persona_sim.panel.schema import Question, QuestionType
from persona_sim.run.parsing import answer_schema, parse_answer

_SINGLE_QUESTION = Question(
    id="q_intent",
    text="この商品を購入したいと思いますか。",
    type=QuestionType.SINGLE,
    options=("ぜひ", "やや", "どちらとも", "あまり", "まったく"),
)

_MULTI_QUESTION = Question(
    id="q_multi",
    text="当てはまるものを選んでください。",
    type=QuestionType.MULTI,
    options=("A", "B", "C"),
)

_OPEN_QUESTION = Question(id="q_reason", text="理由は。", type=QuestionType.OPEN, max_length=100)

_NUMERIC_QUESTION = Question(id="q_num", text="何回ですか。", type=QuestionType.NUMERIC)


@pytest.mark.parametrize(
    "raw",
    ["3", "３", "3.", "「3」", "答え: 3", '{"answer": 3}', "3番"],
)
def test_parse_single_code_variants(raw):
    result = parse_answer(raw, _SINGLE_QUESTION, option_count=5)
    assert result.code == 3
    assert result.codes == (3,)
    assert result.out_of_range is False


def test_parse_single_out_of_range():
    result = parse_answer("9", _SINGLE_QUESTION, option_count=5)
    assert result.code == 9
    assert result.out_of_range is True


def test_parse_single_refusal():
    result = parse_answer("わかりません", _SINGLE_QUESTION, option_count=5)
    assert result.refusal is True
    assert result.code is None
    assert result.codes == ()


@pytest.mark.parametrize("raw", ["1,3", "1、3", "1 と 3"])
def test_parse_multi_from_text(raw):
    result = parse_answer(raw, _MULTI_QUESTION, option_count=3)
    assert result.codes == (1, 3)
    assert result.out_of_range is False


def test_parse_multi_from_json():
    result = parse_answer('{"answers":[3,1]}', _MULTI_QUESTION, option_count=3)
    assert result.codes == (1, 3)


def test_parse_multi_out_of_range():
    result = parse_answer("1,9", _MULTI_QUESTION, option_count=3)
    assert result.out_of_range is True


def test_parse_open_strips_text():
    result = parse_answer("  美味しそうだから  ", _OPEN_QUESTION, option_count=0)
    assert result.text == "美味しそうだから"
    assert result.code is None


def test_parse_numeric_extracts_value():
    result = parse_answer("だいたい月に3回くらいです", _NUMERIC_QUESTION, option_count=0)
    assert result.text == "3"


def test_parse_numeric_with_comma_and_decimal():
    result = parse_answer("1,234.5円くらい", _NUMERIC_QUESTION, option_count=0)
    assert result.text == "1,234.5"


def test_parse_numeric_no_value_found():
    result = parse_answer("特にありません", _NUMERIC_QUESTION, option_count=0)
    assert result.text is None


def test_refusal_flag_set_even_with_parsable_code():
    # 番号が取れていても拒否パターンに一致すれば refusal を立てる
    result = parse_answer("3ですが、AIとしてお答えしかねます", _SINGLE_QUESTION, option_count=5)
    assert result.refusal is True
    assert result.code == 3


def test_answer_schema_single():
    schema = answer_schema(_SINGLE_QUESTION, option_count=5)
    assert schema["type"] == "json_schema"
    assert schema["json_schema"]["schema"]["properties"]["answer"]["maximum"] == 5


def test_answer_schema_multi():
    schema = answer_schema(_MULTI_QUESTION, option_count=3)
    assert schema["json_schema"]["schema"]["properties"]["answers"]["items"]["maximum"] == 3


def test_answer_schema_open_is_none():
    assert answer_schema(_OPEN_QUESTION, option_count=0) is None


def test_json_is_read_from_the_first_object_when_the_reply_has_more_than_one():
    """JSON が2つ並んでも先頭の1つを読む。

    貪欲な `\\{.*\\}` で切り出していたときは、先頭の `{` から**最後の** `}` までを
    1つの塊として渡してしまい、JSON として壊れて後段の正規表現に落ちていた。
    そこで拾われるのは「文字列中に最初に現れた数字」なので、
    例示や言い訳の中の数字を答えとして採る事故になる。
    """
    raw = '設問 2 について。{"answer": 3}（参考: {"note": "x"}）'
    result = parse_answer(raw, _SINGLE_QUESTION, option_count=5)
    assert result.code == 3


def test_json_with_a_nested_object_is_read_whole():
    """入れ子の `{}` を途中で切らない（非貪欲 `.*?` に替えると落ちる）。"""
    raw = '前置き {"meta": {"note": "参考"}, "answer": 2} 以上。'
    result = parse_answer(raw, _SINGLE_QUESTION, option_count=5)
    assert result.code == 2


_REASONING_QUESTION = Question(
    id="q_novelty",
    text="この商品は新しいと思いますか。",
    type=QuestionType.SINGLE,
    options=("とても新しい", "やや新しい", "どちらともいえない", "あまり新しくない", "まったく新しくない"),
    reasoning=True,
    reasoning_max_length=80,
)

_REASONING_MULTI_QUESTION = Question(
    id="q_scene",
    text="飲みたい場面をすべて選んでください。",
    type=QuestionType.MULTI,
    options=("A", "B", "C"),
    reasoning=True,
)


def test_answer_schema_puts_reasoning_before_the_answer():
    """理由が先、番号が後（§6.3）。

    生成は前から進むので、この順序が「考えてから答える」順を作る。逆に並べると
    番号を決めた後の後付けの説明になり、回答そのものは変わらない。
    """
    schema = answer_schema(_REASONING_QUESTION, option_count=5)
    body = schema["json_schema"]["schema"]
    assert list(body["properties"]) == ["reasoning", "answer"]
    assert body["required"] == ["reasoning", "answer"]
    assert body["properties"]["reasoning"]["type"] == "string"
    # 字数はスキーマに入れない（strict 対応がエンドポイント依存のため）
    assert "maxLength" not in body["properties"]["reasoning"]
    assert schema["json_schema"]["name"] != answer_schema(_SINGLE_QUESTION, 5)["json_schema"]["name"]


def test_answer_schema_reasoning_for_multi():
    body = answer_schema(_REASONING_MULTI_QUESTION, option_count=3)["json_schema"]["schema"]
    assert list(body["properties"]) == ["reasoning", "answers"]


def test_answer_schema_without_reasoning_is_unchanged():
    body = answer_schema(_SINGLE_QUESTION, option_count=5)["json_schema"]["schema"]
    assert list(body["properties"]) == ["answer"]


def test_parse_reasoning_keeps_both_the_reason_and_the_code():
    raw = '{"reasoning": "ゆず果汁の缶ハイボールは売り場で見ないので目新しい。", "answer": 2}'
    result = parse_answer(raw, _REASONING_QUESTION, option_count=5)
    assert result.code == 2
    assert result.reasoning == "ゆず果汁の缶ハイボールは売り場で見ないので目新しい。"


def test_parse_without_reasoning_leaves_the_field_empty():
    result = parse_answer('{"answer": 2}', _SINGLE_QUESTION, option_count=5)
    assert result.reasoning is None


def test_reasoning_questions_do_not_scan_for_bare_digits():
    """理由の中の数字を回答にしない（§6.3）。

    `structured_output: always` を必須にしてあるので JSON は必ず返る。それでも
    数字の走査を残すと、フォールバックが起きた回に「205円」の 205 や「3人家族」の 3 を
    回答番号として拾い、パース失敗が**もっともらしい回答**に化ける。
    """
    result = parse_answer("205円は高いと思う。3人家族なので。", _REASONING_QUESTION, option_count=5)
    assert result.code is None
    assert result.codes == ()


def test_reasoning_multi_questions_do_not_scan_for_bare_digits():
    result = parse_answer("1本目は良いが2本目は要らない", _REASONING_MULTI_QUESTION, option_count=3)
    assert result.codes == ()


def test_refusal_is_not_raised_by_the_reasoning_body():
    """理由に「わかりません」と書かれても拒否ではない（§8）。

    こちらが書かせた散文なので拒否の目印にならない。誤検出すると `refusal` が
    `QUALITY_FLAGS` に含まれるぶん「フラグ除外後の n」が実態より小さくなる。
    """
    raw = '{"reasoning": "似た商品があるか判断できません。わかりませんが目新しくはない。", "answer": 4}'
    result = parse_answer(raw, _REASONING_QUESTION, option_count=5)
    assert result.refusal is False
    assert result.code == 4


def test_refusal_outside_the_reasoning_body_is_still_flagged():
    """理由の外に書かれた拒否は拾う。JSON の外へ本文を垂れ流す応答がありうる。"""
    raw = 'AIとしてお答えできません。{"reasoning": "特になし。", "answer": 3}'
    result = parse_answer(raw, _REASONING_QUESTION, option_count=5)
    assert result.refusal is True
