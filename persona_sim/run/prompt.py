"""プロンプト組み立て（`SPEC.md` §6.1）。

このモジュールは **pyspark に依存しない**。純関数のみで構成し、Spark を
起動せずに単体テストできるようにする。
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from persona_sim.llm.client import ChatMessage
from persona_sim.panel.schema import (
    DEFAULT_SYSTEM_PROMPT,
    ImageMode,
    PersonaAttribute,
    PersonaCardConfig,
    PromptConfig,
    Question,
    QuestionType,
    Stimulus,
)

#: [system] ブロックの既定。`SPEC.md` §6.1 と一字一句同じにすること。
#: 調査定義の `prompt.system` で差し替えられる。
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


@dataclass(frozen=True)
class PremiseBlock:
    """ペルソナカード末尾に足す前提（§4.2, §6.1）。

    見出しで由来を分けているのは、プロンプトを読んだときに
    「本人が答えたこと」と「こちらが与えた条件」を取り違えないようにするため。
    """

    heading: str
    lines: tuple[str, ...]

    def render(self) -> str:
        body = "\n".join(f"- {line}" for line in self.lines)
        return f"【{self.heading}】\n{body}"


def attribute_row(
    persona: Mapping[str, Any],
    attributes: Sequence[PersonaAttribute],
) -> str:
    """属性行。`{値}{suffix}` を中黒で連ねる。

    本調査のカードと判定用のカードで共用する。**整形は共用しない**（あちらは1行に
    詰める。`persona_sim.panel.infer.persona_card_for_judge` の説明を参照）が、
    どの属性をどの接尾辞で出すかはここに集める。2箇所に同じ並びを書いていると、
    片方だけ直したときに本調査と判定で見せている属性が食い違う。

    値が None または空文字の項目は詰める（区切りの中黒が余らないこと）。
    """
    parts: list[str] = []
    for attribute in attributes:
        value = persona.get(attribute.field)
        if value in (None, ""):
            continue
        parts.append(f"{value}{attribute.suffix}")
    return "・".join(parts)


def persona_card(
    persona: Mapping[str, Any],
    card_config: PersonaCardConfig,
    premise: PremiseBlock | None = None,
) -> str:
    """§6.1 のペルソナカードを組み立てる。順序は固定。

    属性行（`card_config.attributes`）
    空行
    "{persona}"（`card_config.include_summary` のとき）
    空行
    `card_config.persona_fields` の各要素について "【{label}】{値}" を1行ずつ

    値が None または空文字の列はその行ごと省略する。属性行・総括を出さない設定なら
    **空行も出さない**。空行だけが残ると、何かを載せ忘れたようにプロンプトが読める。
    """
    lines: list[str] = []

    row = attribute_row(persona, card_config.attributes)
    if row:
        lines.extend([row, ""])

    if card_config.include_summary:
        summary = persona.get("persona")
        if summary not in (None, ""):
            lines.extend([str(summary), ""])

    for persona_field in card_config.persona_fields:
        value = persona.get(persona_field.field)
        if value in (None, ""):
            continue
        lines.append(f"【{persona_field.label}】{value}")

    if premise is not None and premise.lines:
        lines.append("")
        lines.append(premise.render())

    return "\n".join(lines)


def stimulus_block(stimulus: Stimulus, prompt_config: PromptConfig) -> str:
    """コンセプト提示文。見出しは `prompt.headings.stimulus`。

    image_mode が text で image_uri がある場合も、text 本文をそのまま使う
    （画像の文章化は事前に text に含める運用。§5.5）。native の場合の画像
    パーツ構築は build_user_message 等で行う。
    """
    return f"{prompt_config.headings.stimulus}\n{stimulus.text}"


def question_block(
    question: Question, options: Sequence[str], prompt_config: PromptConfig
) -> str:
    """設問文。見出しは `prompt.headings.question`、指示行は `prompt.rules`。

    選択式（single / multi / scale）は設問文の下に "1. 選択肢" 形式で options を
    渡された順で並べ、最後に指示行を置く。open は選択肢を出さず、`max_length` が
    None なら指示行そのものを省く。

    `question.reasoning` が真なら、指示行は `prompt.rules.{設問タイプ}` ではなく
    `prompt.rules.reasoning.{設問タイプ}`（`{max_length}` に `reasoning_max_length` が
    入る）になる。併記しないのは「番号のみで答えてください」と矛盾するため（§6.3）。
    置き換え先も設問タイプごとに持つので、`multi` の「すべて」のようなタイプ固有の
    指示は落ちない。
    """
    lines = [prompt_config.headings.question, question.text]
    rule = prompt_config.rules.for_question(question)

    if question.type in (QuestionType.SINGLE, QuestionType.SCALE, QuestionType.MULTI):
        for index, option in enumerate(options, start=1):
            lines.append(f"{index}. {option}")
        lines.append("")
        lines.append(
            rule.format(max_length=question.reasoning_max_length) if question.reasoning else rule
        )
    elif question.type is QuestionType.OPEN:
        if question.max_length is not None:
            lines.append("")
            lines.append(rule.format(max_length=question.max_length))
    elif question.type is QuestionType.NUMERIC:
        lines.append("")
        lines.append(rule)

    return "\n".join(lines)


def build_user_message(
    persona: Mapping[str, Any],
    prompt_config: PromptConfig,
    card_config: PersonaCardConfig,
    stimuli: Sequence[Stimulus],
    question: Question,
    options: Sequence[str],
    premise: PremiseBlock | None = None,
) -> str | list[dict[str, Any]]:
    """"■あなたのプロフィール" → 提示物 → 設問 の順に連結した [user] メッセージ。

    stimuli が複数のときは提示物ブロックを順に並べる。ブロック間は空行1つで区切る。

    stimulus.image_mode is ImageMode.NATIVE かつ image_uri が指定されている場合は
    list[dict[str, Any]]（テキストパート＋画像パート）として組み立てる。
    """
    profile = persona_card(persona, card_config, premise)
    blocks = [f"{prompt_config.headings.profile}\n{profile}"]
    blocks.extend(stimulus_block(stimulus, prompt_config) for stimulus in stimuli)
    blocks.append(question_block(question, options, prompt_config))
    return _with_images("\n\n".join(blocks), stimuli)


def initial_messages(
    persona: Mapping[str, Any],
    prompt_config: PromptConfig,
    card_config: PersonaCardConfig,
    stimuli: Sequence[Stimulus],
    question: Question,
    options: Sequence[str],
    premise: PremiseBlock | None = None,
) -> list[ChatMessage]:
    """[system] と最初の [user] を組んだメッセージ列。

    `[system]` は設問が指す `prompt.systems` の文面（`question.system`）。
    指していなければ `prompt.system`（§6.1）。
    """
    return [
        ChatMessage(role="system", content=prompt_config.system_for(question.system)),
        ChatMessage(
            role="user",
            content=build_user_message(
                persona, prompt_config, card_config, stimuli, question, options, premise
            ),
        ),
    ]


def follow_up_message(
    question: Question,
    options: Sequence[str],
    prompt_config: PromptConfig,
    stimuli: Sequence[Stimulus] = (),
) -> ChatMessage:
    """会話履歴を保持する場合の2問目以降。

    設問ブロックだけを user メッセージにする（プロフィールは履歴に既にあるので
    繰り返さない）。`stimuli` を渡すと設問の前に提示物ブロックを挟む——**その会話で
    まだ見せていないコンセプトに切り替わるターンだけ**渡すこと。判定は
    `replayed_messages()` が持つ。
    """
    blocks = [stimulus_block(stimulus, prompt_config) for stimulus in stimuli]
    blocks.append(question_block(question, options, prompt_config))
    return ChatMessage(role="user", content=_with_images("\n\n".join(blocks), stimuli))


def _with_images(text: str, stimuli: Sequence[Stimulus]) -> str | list[dict[str, Any]]:
    """`image_mode: native` の画像をテキストパートの後ろに足す。

    画像が無ければ文字列のまま返す（マルチモーダル非対応のエンドポイントに、
    見た目だけ配列にしたメッセージを送らないため）。
    """
    native_images = [
        stimulus.image_uri
        for stimulus in stimuli
        if stimulus.image_mode is ImageMode.NATIVE and stimulus.image_uri
    ]
    if not native_images:
        return text

    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for uri in native_images:
        parts.append({"type": "image_url", "image_url": {"url": uri}})
    return parts


@dataclass(frozen=True)
class ReplayTurn:
    """再生する1ターン（設問と、その設問への回答）。

    `answer` が `None` なら「これから聞く設問」で、必ず列の最後に来る。
    """

    stimuli: tuple[Stimulus, ...]
    question: Question
    options: tuple[str, ...]
    answer: str | None = None


def replayed_messages(
    persona: Mapping[str, Any],
    prompt_config: PromptConfig,
    card_config: PersonaCardConfig,
    turns: Sequence[ReplayTurn],
    premise: PremiseBlock | None = None,
) -> list[ChatMessage]:
    """記憶として持たせる先行ターンを再生し、最後に今回の設問を置いたメッセージ列。

    **毎ターン組み直す。** 会話を伸ばし続ける作りだと、設問ごとに記憶の範囲が違う場合
    （`questions[].remember`）に「直前までの全部」しか表現できない。答えた文面さえ
    取っておけば列は組み直せるので、そちらに寄せる。

    3つの不変条件をここで守る。

    1. **プロフィールは1回だけ。** 先頭の [user] にだけ載せる。回答済みターンの
       [user] メッセージをそのまま使い回すと、単独のセッションで実行された設問
       （＝プロフィール入り）を再生したときに2回出る。
    2. **コンセプトはその列で初出のときだけ挟む。** 同じコンセプトの提示文を
       毎ターン繰り返さない一方、記憶が別コンセプトにまたがるときは切り替わりが読める。
    3. **[system] は今回の設問（列の最後）のもの。** 設問ごとに `system` を
       使い分けられる（§6.1）ので、記憶として再生する先行設問のものを使うと、
       **これから答えさせる設問が別の指示で聞かれる**ことになる。列は毎ターン
       組み直すので、先行ターンの回答文面はそのまま history として残る。

    `turns` が1件（記憶なし）なら `initial_messages()` と同じ列になる。
    """
    if not turns:
        raise ValueError("turns が空。少なくとも今回の設問が要る")

    messages = [
        ChatMessage(role="system", content=prompt_config.system_for(turns[-1].question.system))
    ]
    presented: set[str] = set()

    for index, turn in enumerate(turns):
        fresh = tuple(s for s in turn.stimuli if s.id not in presented)
        presented.update(s.id for s in turn.stimuli)

        if index == 0:
            content = build_user_message(
                persona, prompt_config, card_config, fresh, turn.question, turn.options, premise
            )
            messages.append(ChatMessage(role="user", content=content))
        else:
            messages.append(
                follow_up_message(turn.question, turn.options, prompt_config, fresh)
            )

        if turn.answer is not None:
            messages.append(ChatMessage(role="assistant", content=turn.answer))

    return messages


def presented_options(
    question: Question, seed: int, persona_uuid: str
) -> tuple[list[str], list[int]]:
    """実際に提示する選択肢とその順序を返す。

    戻り値は (提示順に並べた選択肢文字列, options_order)。options_order は
    「定義順での番号(1始まり)」を提示順に並べたもの。

    question.randomize_options が False なら定義順のまま（options_order は [1,2,3,...]）。
    True なら sha256(f"{seed}|{persona_uuid}|{question.id}") から決定論的に並べ替える。
    同じ (seed, persona_uuid, question_id) なら必ず同じ順序になる。

    `question.is_ordinal()` が True の設問は randomize_options を True にできないことは
    `validate` が既に保証しているので、ここでは再チェックしない。
    """
    options = list(question.options)
    order = list(range(1, len(options) + 1))

    if not question.randomize_options:
        return options, order

    key = f"{seed}|{persona_uuid}|{question.id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    int_seed = int(digest, 16)
    rng = random.Random(int_seed)
    rng.shuffle(order)

    shuffled_options = [options[i - 1] for i in order]
    return shuffled_options, order
