"""プロンプト組み立て（`persona_sim.run.prompt`）。"""

from __future__ import annotations

from persona_sim.panel.schema import (
    ImageMode,
    PersonaAttribute,
    PersonaCardConfig,
    PersonaField,
    PromptConfig,
    PromptHeadings,
    PromptRules,
    Question,
    QuestionType,
    Stimulus,
)
from persona_sim.run.prompt import (
    SYSTEM_PROMPT,
    ReplayTurn,
    attribute_row,
    build_user_message,
    follow_up_message,
    initial_messages,
    persona_card,
    presented_options,
    question_block,
    replayed_messages,
    stimulus_block,
)

_PERSONA = {
    "uuid": "u1",
    "sex": "女",
    "age": 34,
    "prefecture": "東京都",
    "marital_status": "未婚",
    "education_level": "大卒",
    "occupation_raw": "会社員",
    "persona": "都内で働く会社員。",
    "cultural_background": "アニメ・サブカル好き",
    "professional_persona": "IT企業で営業をしている",
    "hobbies_and_interests": "旅行と読書",
    "culinary_persona": "自炊派で健康志向",
}

_PROMPT_CONFIG = PromptConfig()
_CARD_CONFIG = PersonaCardConfig()


def _stimulus() -> Stimulus:
    return Stimulus(id="c1", name="コンセプトA", text="内容A")


def test_persona_card_default_order():
    card = persona_card(_PERSONA, _CARD_CONFIG)
    lines = card.split("\n")
    assert lines[0] == "女・34歳・東京都在住・未婚・大卒・会社員"
    assert lines[1] == ""
    assert lines[2] == "都内で働く会社員。"
    assert lines[3] == ""
    assert lines[4] == "【生活背景】アニメ・サブカル好き"
    assert lines[5] == "【仕事】IT企業で営業をしている"
    assert lines[6] == "【関心事】旅行と読書"
    assert lines[7] == "【食まわり】自炊派で健康志向"
    assert len(lines) == 8


def test_persona_card_missing_columns_are_skipped():
    persona = dict(_PERSONA)
    persona["prefecture"] = None
    persona["hobbies_and_interests"] = ""
    card = persona_card(persona, _CARD_CONFIG)
    lines = card.split("\n")
    # 中黒が余らずに詰めて連結される
    assert lines[0] == "女・34歳・未婚・大卒・会社員"
    # 【関心事】の行自体が省略される
    assert "【関心事】" not in card
    assert "【生活背景】アニメ・サブカル好き" in lines
    assert "【仕事】IT企業で営業をしている" in lines
    assert "【食まわり】自炊派で健康志向" in lines


def test_persona_card_uses_custom_persona_fields():
    custom_config = PersonaCardConfig(
        persona_fields=(
            PersonaField(field="sports_persona", label="運動習慣"),
            PersonaField(field="travel_persona", label="旅行"),
        )
    )
    persona = dict(_PERSONA)
    persona["sports_persona"] = "週末にジョギングをする"
    persona["travel_persona"] = "海外旅行が好き"
    card = persona_card(persona, custom_config)
    assert "【運動習慣】週末にジョギングをする" in card
    assert "【旅行】海外旅行が好き" in card
    assert "【生活背景】" not in card


def test_persona_card_selects_attributes_individually():
    """属性行は属性ごとに選べる（issue 202607301208 項目4）。"""
    config = PersonaCardConfig(
        attributes=(
            PersonaAttribute(field="sex"),
            PersonaAttribute(field="age", suffix="歳"),
        )
    )
    card = persona_card(_PERSONA, config)
    # 選ばなかった属性は属性行に出ない（総括文には残るので1行目だけを見る）。
    assert card.split("\n")[0] == "女・34歳"


def test_persona_card_can_use_columns_outside_the_default_attributes():
    """既定の6項目に無い列も属性行に入れられる。"""
    config = PersonaCardConfig(
        attributes=(PersonaAttribute(field="region", suffix="地方"),)
    )
    persona = dict(_PERSONA)
    persona["region"] = "関東"
    assert persona_card(persona, config).split("\n")[0] == "関東地方"


def test_persona_card_without_attributes_or_summary_has_no_blank_lead():
    """属性行も総括も出さないなら空行も出さない。

    空行だけが残ると、載せ忘れたようにプロンプトが読める。
    """
    config = PersonaCardConfig(attributes=(), include_summary=False)
    card = persona_card(_PERSONA, config)
    assert card.startswith("【生活背景】")
    assert "都内で働く会社員。" not in card


def test_persona_card_drops_the_summary_line_when_it_is_missing():
    """総括が空なら行ごと落とす（空行だけを残さない）。"""
    persona = dict(_PERSONA)
    persona["persona"] = ""
    card = persona_card(persona, _CARD_CONFIG)
    lines = card.split("\n")
    assert lines[0] == "女・34歳・東京都在住・未婚・大卒・会社員"
    assert lines[1] == ""
    assert lines[2] == "【生活背景】アニメ・サブカル好き"


def test_attribute_row_is_shared_with_the_judge_card():
    """本調査と判定で同じ属性の並びを使う（2箇所に書かない）。"""
    from persona_sim.panel.infer import persona_card_for_judge

    attributes = (
        PersonaAttribute(field="sex"),
        PersonaAttribute(field="prefecture", suffix="在住"),
    )
    row = attribute_row(_PERSONA, attributes)
    assert row == "女・東京都在住"

    judge = persona_card_for_judge(
        _PERSONA,
        PersonaCardConfig(attributes=attributes, include_summary=False, persona_fields=()),
    )
    assert judge == row


def test_stimulus_block():
    stimulus = Stimulus(id="c1", name="コンセプトA", text="【商品名】…\n【特徴】…")
    block = stimulus_block(stimulus, _PROMPT_CONFIG)
    assert block == "■提示物\n【商品名】…\n【特徴】…"


def _single_question(**overrides) -> Question:
    base = dict(
        id="q_intent",
        text="この商品を購入したいと思いますか。",
        type=QuestionType.SINGLE,
        options=("ぜひ購入したい", "やや購入したい", "どちらともいえない"),
    )
    base.update(overrides)
    return Question(**base)


def test_question_block_single_lists_options_in_given_order():
    question = _single_question()
    options = ["どちらともいえない", "ぜひ購入したい", "やや購入したい"]
    block = question_block(question, options, _PROMPT_CONFIG)
    lines = block.split("\n")
    assert lines[0] == "■設問"
    assert lines[1] == question.text
    assert lines[2] == "1. どちらともいえない"
    assert lines[3] == "2. ぜひ購入したい"
    assert lines[4] == "3. やや購入したい"
    assert lines[5] == ""
    assert lines[6] == "番号のみで答えてください。"


def test_question_block_multi_instruction():
    question = Question(
        id="q_multi", text="当てはまるものを選んでください。", type=QuestionType.MULTI,
        options=("A", "B", "C"),
    )
    block = question_block(question, list(question.options), _PROMPT_CONFIG)
    assert block.endswith("当てはまる番号をすべて、カンマ区切りで答えてください。")


def test_question_block_reasoning_replaces_the_type_instruction():
    """`reasoning: true` の設問はタイプの指示行を置き換える（§6.3）。

    併記すると「番号のみで答えてください」と矛盾する。
    """
    question = Question(
        id="q_novelty",
        text="この商品は新しいと思いますか。",
        type=QuestionType.SINGLE,
        options=("とても新しい", "やや新しい"),
        reasoning=True,
        reasoning_max_length=60,
    )
    block = question_block(question, list(question.options), _PROMPT_CONFIG)
    assert "番号のみで答えてください。" not in block
    assert block.endswith(
        "まず、そう考えた理由を60文字以内で書き、そのうえで当てはまる番号を1つ選んでください。"
    )
    # 選択肢は変わらず並ぶ
    assert "1. とても新しい" in block


def test_question_block_reasoning_keeps_the_multi_instruction():
    """`multi` の置き換え先は「すべて」を保つ（§6.3）。

    共通の1文で全タイプを賄っていた頃、複数回答なのに「番号を選んでください」だけが
    残り、1つしか選ばせない問いになっていた。
    """
    question = Question(
        id="q_uses",
        text="どの場面で飲みますか。",
        type=QuestionType.MULTI,
        options=("食事中", "入浴後", "就寝前"),
        reasoning=True,
        reasoning_max_length=60,
    )
    block = question_block(question, list(question.options), _PROMPT_CONFIG)
    assert block.endswith(
        "まず、そう考えた理由を60文字以内で書き、そのうえで当てはまる番号をすべて挙げてください。"
    )


def test_question_block_open_with_max_length():
    question = Question(id="q_reason", text="理由は。", type=QuestionType.OPEN, max_length=150)
    block = question_block(question, [], _PROMPT_CONFIG)
    assert block == "■設問\n理由は。\n\n150文字以内で答えてください。"


def test_question_block_open_without_max_length():
    question = Question(id="q_reason", text="理由は。", type=QuestionType.OPEN, max_length=None)
    block = question_block(question, [], _PROMPT_CONFIG)
    assert block == "■設問\n理由は。"


def test_question_block_numeric():
    question = Question(id="q_num", text="何回ですか。", type=QuestionType.NUMERIC)
    block = question_block(question, [], _PROMPT_CONFIG)
    assert block == "■設問\n何回ですか。\n\n数値のみで答えてください。"


def test_build_user_message_order_and_blank_line_separator():
    question = _single_question()
    stimulus = Stimulus(id="c1", name="コンセプトA", text="内容A")
    message = build_user_message(_PERSONA, _PROMPT_CONFIG, _CARD_CONFIG, [stimulus], question, list(question.options))
    assert message.startswith("■あなたのプロフィール\n")
    assert "\n\n■提示物\n内容A\n\n■設問\n" in message


def test_build_user_message_multiple_stimuli_in_order():
    question = _single_question()
    stimuli = [
        Stimulus(id="c1", name="コンセプトA", text="内容A"),
        Stimulus(id="c2", name="コンセプトB", text="内容B"),
    ]
    message = build_user_message(_PERSONA, _PROMPT_CONFIG, _CARD_CONFIG, stimuli, question, list(question.options))
    assert message.index("内容A") < message.index("内容B")


def test_initial_messages():
    question = _single_question()
    stimulus = Stimulus(id="c1", name="コンセプトA", text="内容A")
    messages = initial_messages(_PERSONA, _PROMPT_CONFIG, _CARD_CONFIG, [stimulus], question, list(question.options))
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[0].content == SYSTEM_PROMPT
    assert messages[1].role == "user"
    assert "■あなたのプロフィール" in messages[1].content
    assert "■提示物" in messages[1].content


def test_a_single_turn_replay_equals_initial_messages():
    """記憶を持たない設問の入力は、従来の組み立てと1バイトも変わらない（回帰）。

    `replayed_messages()` の docstring がこの同値を約束しているので、網を張っておく。
    """
    question = _single_question()
    stimulus = Stimulus(id="c1", name="コンセプトA", text="内容A")
    options = list(question.options)

    assert replayed_messages(
        _PERSONA,
        _PROMPT_CONFIG,
        _CARD_CONFIG,
        [ReplayTurn(stimuli=(stimulus,), question=question, options=tuple(options))],
    ) == initial_messages(
        _PERSONA, _PROMPT_CONFIG, _CARD_CONFIG, [stimulus], question, options
    )


def test_a_replayed_turn_carries_the_answer_and_skips_the_repeated_concept():
    """2ターン目は同じコンセプトを繰り返さず、回答が [assistant] として挟まる。"""
    question = _single_question()
    stimulus = Stimulus(id="c1", name="コンセプトA", text="内容A")
    options = tuple(question.options)
    messages = replayed_messages(
        _PERSONA,
        _PROMPT_CONFIG,
        _CARD_CONFIG,
        [
            ReplayTurn(stimuli=(stimulus,), question=question, options=options, answer="2"),
            ReplayTurn(stimuli=(stimulus,), question=question, options=options),
        ],
    )

    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[2].content == "2"
    assert "■提示物" not in messages[3].content
    assert "■あなたのプロフィール" not in messages[3].content


def test_follow_up_message_only_contains_question_block():
    question = _single_question()
    message = follow_up_message(question, list(question.options), _PROMPT_CONFIG)
    assert message.role == "user"
    assert message.content == question_block(question, list(question.options), _PROMPT_CONFIG)
    assert "■あなたのプロフィール" not in message.content
    assert "■提示物" not in message.content


# --------------------------------------------------------------------------- #
# presented_options
# --------------------------------------------------------------------------- #


def test_presented_options_no_randomize_keeps_definition_order():
    question = _single_question(randomize_options=False)
    options, order = presented_options(question, seed=42, persona_uuid="uuid-1")
    assert options == list(question.options)
    assert order == [1, 2, 3]


def test_presented_options_randomize_is_deterministic_for_same_key():
    question = _single_question(randomize_options=True)
    options_a, order_a = presented_options(question, seed=42, persona_uuid="uuid-1")
    options_b, order_b = presented_options(question, seed=42, persona_uuid="uuid-1")
    assert options_a == options_b
    assert order_a == order_b


def test_presented_options_randomize_varies_across_personas():
    question = _single_question(randomize_options=True)
    results = {
        tuple(presented_options(question, seed=42, persona_uuid=f"uuid-{i}")[1])
        for i in range(20)
    }
    assert len(results) > 1


def test_presented_options_order_reconstructs_presented_list():
    question = _single_question(randomize_options=True)
    options, order = presented_options(question, seed=7, persona_uuid="uuid-xyz")
    defined = list(question.options)
    assert [defined[i - 1] for i in order] == options


# --------------------------------------------------------------------------- #
# プロンプトの config 化（§6.1）
# --------------------------------------------------------------------------- #


def test_system_prompt_can_be_overridden():
    """[system] を差し替えられる。省略時は既定のまま。"""
    config = PromptConfig(system="あなたは架空の人物です。")
    messages = initial_messages(
        _PERSONA, config, _CARD_CONFIG, (_stimulus(),), _single_question(), ["A", "B", "C"]
    )
    assert messages[0].content == "あなたは架空の人物です。"
    default = initial_messages(
        _PERSONA, _PROMPT_CONFIG, _CARD_CONFIG, (_stimulus(),), _single_question(), ["A", "B", "C"]
    )
    assert default[0].content == SYSTEM_PROMPT


def test_headings_can_be_overridden():
    config = PromptConfig(
        headings=PromptHeadings(profile="●プロフィール", stimulus="●対象", question="●質問")
    )
    message = build_user_message(
        _PERSONA, config, _CARD_CONFIG, (_stimulus(),), _single_question(), ["A", "B", "C"]
    )
    assert message.startswith("●プロフィール\n")
    assert "●対象" in message
    assert "●質問" in message
    assert "■あなたのプロフィール" not in message


def test_rules_can_be_overridden_per_question_type():
    config = PromptConfig(rules=PromptRules(single="1つだけ番号で答えよ。"))
    block = question_block(_single_question(), ["A", "B", "C"], config)
    assert block.endswith("1つだけ番号で答えよ。")
    assert "番号のみで答えてください。" not in block


def test_open_rule_receives_max_length():
    config = PromptConfig(rules=PromptRules(open="{max_length}字まで。"))
    question = _single_question(type=QuestionType.OPEN, options=(), max_length=80)
    assert question_block(question, [], config).endswith("80字まで。")


def test_open_rule_is_omitted_without_max_length():
    """`max_length` が無ければ指示行そのものを出さない（差し込み先が無いため）。"""
    config = PromptConfig(rules=PromptRules(open="{max_length}字まで。"))
    question = _single_question(type=QuestionType.OPEN, options=(), max_length=None)
    block = question_block(question, [], config)
    assert "字まで。" not in block
    assert block.splitlines()[-1] == question.text


# --------------------------------------------------------------------------- #
# 設問ごとの [system]（§6.1）
# --------------------------------------------------------------------------- #

#: 購入意向・新規性を書き分けた設定。名前で引く（文面は設問側に書かない）。
_TWO_SYSTEMS = PromptConfig(
    systems={"purchase_intent": "買うかどうかで判断せよ。", "novelty": "違いの大きさで判断せよ。"}
)


def test_the_question_chooses_the_system_prompt_by_name():
    question = _single_question(system="novelty")
    messages = initial_messages(
        _PERSONA, _TWO_SYSTEMS, _CARD_CONFIG, (_stimulus(),), question, ["A", "B", "C"]
    )
    assert messages[0].content == "違いの大きさで判断せよ。"


def test_a_question_without_a_system_falls_back_to_the_default():
    """`systems` を定義しても、指していない設問は既定（`prompt.system`）のまま。"""
    messages = initial_messages(
        _PERSONA, _TWO_SYSTEMS, _CARD_CONFIG, (_stimulus(),), _single_question(), ["A", "B", "C"]
    )
    assert messages[0].content == SYSTEM_PROMPT


def test_defining_systems_does_not_change_the_input_of_other_questions():
    """`systems` を足しただけでは、既存の設問の入力が1バイトも変わらない（回帰）。"""
    question = _single_question()
    args = (_PERSONA, _CARD_CONFIG, (_stimulus(),), question, ["A", "B", "C"])
    assert initial_messages(args[0], _TWO_SYSTEMS, *args[1:]) == initial_messages(
        args[0], _PROMPT_CONFIG, *args[1:]
    )


def test_the_replayed_system_prompt_is_the_one_of_the_question_being_asked():
    """記憶を跨いでも [system] はいま聞いている設問のもの（`replayed_messages()` の不変条件）。

    先行設問のものを使うと、これから答えさせる設問が別の指示で聞かれることになる。
    """
    stimulus = _stimulus()
    intent = _single_question(id="q_intent", system="purchase_intent")
    novelty = _single_question(id="q_novelty", system="novelty")
    options = tuple(intent.options)

    messages = replayed_messages(
        _PERSONA,
        _TWO_SYSTEMS,
        _CARD_CONFIG,
        [
            ReplayTurn(stimuli=(stimulus,), question=intent, options=options, answer="2"),
            ReplayTurn(stimuli=(stimulus,), question=novelty, options=options),
        ],
    )

    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0].content == "違いの大きさで判断せよ。"
    # 先行設問の回答は履歴として残る（[system] だけが差し替わる）。
    assert messages[2].content == "2"


def test_build_user_message_native_image():
    stimulus = Stimulus(
        id="c1",
        name="コンセプトA",
        text="内容A",
        image_mode=ImageMode.NATIVE,
        image_uri="/Volumes/main/schema/vol/c1.png",
    )
    msg = build_user_message(_PERSONA, _PROMPT_CONFIG, _CARD_CONFIG, (stimulus,), _single_question(), ["A", "B"])
    assert isinstance(msg, list)
    assert len(msg) == 2
    assert msg[0]["type"] == "text"
    assert "内容A" in msg[0]["text"]
    assert msg[1] == {"type": "image_url", "image_url": {"url": "/Volumes/main/schema/vol/c1.png"}}
