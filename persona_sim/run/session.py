"""セッション単位の実行（`SPEC_PHASE1.md` §5.1, §6.2, §6.3）。

**セッションが実行と再開の両方の単位になる。** その範囲を決めるのは設問ごとの
`remember`（§5.1）で、「設問 A が設問 B を覚えている」なら A と B は同じセッションに入る
——B の回答が出てからでないと A を聞けないため。この連結成分の計算は
`persona_sim.run.memory.session_groups()` が持つ。

セッションの一部だけをやり直すと、本来あったはずの履歴が無い状態で後半の設問に
答えることになり、再開の前後で回答の意味が変わる。だから再開もこの粒度で行う。

記憶を持たない設問は依存を持たないので単独のセッションになり、並列度が落ちない。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from persona_sim.llm.budget import complete_within_budget
from persona_sim.llm.client import (
    Completion,
    LLMClient,
    StructuredOutputUnsupported,
)
from persona_sim.panel.schema import (
    Presentation,
    QuestionType,
    Stimulus,
    StructuredOutput,
    SurveyDefinition,
)
from persona_sim.run import flags as flag_names
from persona_sim.run.memory import plan_remembers, session_groups, stimulus_for
from persona_sim.run.parsing import answer_schema, parse_answer
from persona_sim.run.prompt import (
    ReplayTurn,
    presented_options,
    replayed_messages,
)

#: 同時提示（`presentation: simultaneous`）のときの `responses.stimulus_id`。
#: 回答が特定の1案に紐づかないため、予約値で「全案を同時に見せた」ことを表す。
#: 何を見せたかは `panels.assigned_stimuli` から復元できる。
ALL_STIMULI = "*"

#: コンセプトを見せないセッション（スクリーニング。§4.2）で使う `stimulus_id`。
#: `screener_responses` には書き出さない（コンセプトに紐づかないため）。
NO_STIMULUS = "-"

#: パース失敗時の最大試行回数（初回を含む。§6.3）。
PARSE_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Unit:
    """1つの設問への回答。`responses` の1行に対応する。"""

    persona_uuid: str
    stimulus_id: str
    question_id: str
    sequence: int
    #: この設問で提示するコンセプト。**履歴に既に出ていても空にしない。**
    #: 実際にプロンプトへ出すかは `replayed_messages()` が「その列で初出か」で決める。
    #: ここで間引くと、提示物を持たない unit を `ctx.stimuli`（＝調査定義の記述順）から
    #: 補うことになり、ペルソナごとの提示順が定義順に化ける（§9.1 の再現性が壊れる）。
    stimuli_to_present: tuple[Stimulus, ...] = ()
    #: 記憶として再生する先行設問のID。ask order 順。
    remembers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Session:
    """逐次に実行する単位。セッション間は並列に流してよい。"""

    persona_uuid: str
    units: tuple[Unit, ...]

    @property
    def key(self) -> tuple[str, ...]:
        return (self.persona_uuid, *(f"{u.stimulus_id}:{u.question_id}" for u in self.units))


@dataclass
class StructuredOutputState:
    """構造化出力が使えるかどうかの実行時状態（§6.3）。

    `auto` で1度でも拒否されたら以後は正規表現パースに切り替える。切り替えた事実は
    実行メタデータに残す。黙って劣化させない。
    """

    mode: StructuredOutput
    enabled: bool = True
    fell_back: bool = False

    def __post_init__(self) -> None:
        if self.mode is StructuredOutput.NEVER:
            self.enabled = False

    def note_unsupported(self, error: Exception) -> None:
        if self.mode is StructuredOutput.ALWAYS:
            raise error
        self.enabled = False
        self.fell_back = True


@dataclass
class OutputBudgetState:
    """出力予算の引き上げが何回起きたかの実行時状態。

    引き上げて通った回答にはフラグを立てない（`run/flags.py` の `output_limit` 参照）ので、
    「予算が足りていない」という設定の兆候はここでしか残らない。`StructuredOutputState` の
    `fell_back` と同じ扱いで、実行メタデータと警告に出す。黙って劣化させない。

    並列実行では複数スレッドから増やされる。競合しても失われるのは件数の1つで、
    「起きたかどうか」は保たれる。ここに鍵を置いて回答生成を直列化する価値は無い。
    """

    #: 予算を広げて通った呼び出しの数。
    escalated: int = 0
    #: 天井まで広げても通らなかった呼び出しの数。
    exhausted: int = 0

    def note(self, outcome) -> None:
        if outcome.exhausted:
            self.exhausted += 1
        if outcome.escalated:
            self.escalated += 1

    @property
    def occurred(self) -> bool:
        return bool(self.escalated or self.exhausted)


@dataclass
class ResponseRecord:
    """`responses` の1行（§2.3）。"""

    survey_id: str
    persona_uuid: str
    stimulus_id: str
    question_id: str
    sequence: int
    answer_raw: str
    #: 選択された選択肢番号の並び。**提示順**の番号空間（§2.3）。
    #: single / scale は1要素、multi は選んだ番号すべて、open / numeric は空。
    #: 定義順に戻すには `options_order[code - 1]` を引く。
    #: 選択式でここが空なら、番号を取れなかった（パース失敗）ということ。
    answer_codes: list[int]
    answer_text: str | None
    #: `reasoning: true` の設問でモデルが書いた理由（§6.3）。それ以外は None。
    #: `answer_raw` にも同じ内容が JSON として入っているが、読むために毎回
    #: JSON を割るのは集計側の仕事ではないので列に出す。
    answer_reasoning: str | None
    options_order: list[int]
    flags: list[str]
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    attempt: int
    ts: Any = field(default=None)
    #: エンドポイントが返したモデル識別子。`responses` には書かず、実行メタデータ（§9）に集約する。
    model_version: str | None = None


@dataclass
class SessionContext:
    """セッション実行に必要なもの一式。"""

    survey: SurveyDefinition
    client: LLMClient
    personas: Mapping[str, Mapping[str, Any]]
    stimuli: Mapping[str, Stimulus]
    structured_output: StructuredOutputState
    #: 出力予算の引き上げ実績。既定を持たせているのは、渡し忘れても計測が落ちるだけで
    #: 回答生成は成り立つため（`structured_output` と違い挙動を決めるものではない）。
    output_budget: OutputBudgetState = field(default_factory=OutputBudgetState)
    #: 使う設問。既定は調査本体の設問。スクリーニングは自前の設問表を渡す（§4.2）。
    questions: Mapping[str, Any] | None = None
    #: ペルソナごとの前提ブロック（§6.1）。スクリーナーが無ければ空。
    premises: Mapping[str, Any] = field(default_factory=dict)

    def question_map(self) -> Mapping[str, Any]:
        if self.questions is not None:
            return self.questions
        return {question.id: question for question in self.survey.questions}


def build_sessions(
    survey: SurveyDefinition,
    panel_rows: Sequence[Mapping[str, Any]],
) -> list[Session]:
    """パネルからセッションの一覧を作る。

    設問の分け方（＝どの設問どうしが同じセッションに入るか）はペルソナに依らないので、
    1度だけ計算して全ペルソナに使い回す。`slot` は提示順の位置であって、どのコンセプトが
    当たるかとは独立しているため成り立つ。

    投入順は prefix cache が効く向きに並べる（§6.2）。記憶を持つ設問が1つも無ければ
    コンセプト外側・ペルソナ内側に回せる。
    """
    stimuli_by_id = {stimulus.id: stimulus for stimulus in survey.stimuli}
    simultaneous = survey.design.presentation is Presentation.SIMULTANEOUS
    questions_by_id = {question.id: question for question in survey.questions}
    remembers = plan_remembers(survey)
    groups = session_groups(survey)

    sessions = [
        Session(
            persona_uuid=row["persona_uuid"],
            units=tuple(
                _unit(
                    questions_by_id[question_id],
                    row["persona_uuid"],
                    list(row["assigned_stimuli"]),
                    stimuli_by_id,
                    remembers[question_id],
                    simultaneous=simultaneous,
                )
                for question_id in group
            ),
        )
        for row in panel_rows
        for group in groups
    ]

    if all(len(session.units) == 1 for session in sessions):
        # 記憶を持つ設問が無い＝設問どうしが独立している。同じコンセプト文・同じ設問文が
        # 連続するように並べ替えると prefix cache が効く。
        #
        # 記憶を持つ設問と持たない設問が混ざる場合の投入順は、まだ決めていない。
        # セッションの長さがまちまちで「何を先頭に揃えるか」が一意に決まらないため、
        # ここでは並べ替えずパネル順（ペルソナ順）のまま流す。
        sessions.sort(key=lambda s: (s.units[0].stimulus_id, s.units[0].question_id, s.persona_uuid))
    return sessions


def _unit(
    question,
    persona_uuid: str,
    assigned: Sequence[str],
    stimuli_by_id: Mapping[str, Stimulus],
    remembers: tuple[str, ...],
    *,
    simultaneous: bool,
) -> Unit:
    """1設問ぶんの実行単位。コンセプトは `slot` からペルソナの提示順で引く。"""
    stimuli = stimulus_for(question, assigned, stimuli_by_id, simultaneous=simultaneous)
    return Unit(
        persona_uuid=persona_uuid,
        # 同時提示は回答が特定の1案に紐づかないので予約値を使う（§2.3）。
        stimulus_id=ALL_STIMULI if simultaneous else stimuli[0].id,
        question_id=question.id,
        sequence=1 if simultaneous else question.slot,
        stimuli_to_present=stimuli,
        remembers=remembers,
    )


def run_session(session: Session, ctx: SessionContext) -> list[ResponseRecord]:
    """1セッションを頭から順に実行する。

    各設問の入力は、その設問が覚えている先行設問だけを再生して**毎ターン組み直す**
    （`replayed_messages()`）。会話を伸ばし続ける作りだと「直前までの全部」しか
    表現できず、`remember` で飛ばし越しを指定できない。

    取っておくのは回答の文面（`answer_raw`）だけでよい。設問文も選択肢の提示順も
    純関数から組み直せる（`presented_options()` は同じ入力なら必ず同じ順序を返す）。
    """
    survey = ctx.survey
    questions = ctx.question_map()
    persona = ctx.personas[session.persona_uuid]
    premise = ctx.premises.get(session.persona_uuid)

    turns_by_question: dict[str, ReplayTurn] = {}
    records: list[ResponseRecord] = []

    for unit in session.units:
        question = questions[unit.question_id]
        options, options_order = presented_options(question, survey.panel.seed, session.persona_uuid)
        current = ReplayTurn(
            stimuli=unit.stimuli_to_present or _fallback_stimuli(unit, ctx),
            question=question,
            options=tuple(options),
        )

        # 覚えている設問は、必ずこのセッションの中で先に実行されている
        # （`session_groups()` が依存のある設問を同じ成分に入れるため）。
        replayed = [turns_by_question[qid] for qid in unit.remembers]
        messages = replayed_messages(
            persona, survey.prompt, survey.persona_card, [*replayed, current], premise
        )

        completion, parsed, attempt, outcome = _ask(messages, question, len(options), ctx)

        turns_by_question[unit.question_id] = replace(current, answer=completion.text)

        records.append(
            _to_record(
                survey.survey_id,
                unit,
                completion,
                parsed,
                options_order,
                attempt,
                question,
                outcome,
            )
        )

    return records


#: 記憶として再生すべき回答を `responses` から引けなかったときに置く目印（§9）。
#: 黙って [assistant] を落とすとメッセージ列の形が変わり、「実際に送った内容と一致する」
#: という記録の前提が読み手に分からないまま崩れる。
MISSING_ANSWER = "（この設問の回答を responses から復元できなかった）"


@dataclass(frozen=True)
class SampleMessage:
    """記録用に写し取った1メッセージ。`ChatMessage` と同じ形。"""

    role: str
    content: str | list[dict[str, Any]]


@dataclass(frozen=True)
class PromptSample:
    """1設問ぶんの、実際に送ったメッセージ列（§9）。

    実行後に「何を聞いたのか」を確認するための記録。1名ぶんだけ残す。

    **`system` / `user` の2本ではなく列そのものを持つ。** 記憶を持つ設問は
    [system] / [user] / [assistant] / [user] … という列で送られるので、
    2本に潰すと実際に送った内容を表現できない。
    """

    persona_uuid: str
    stimulus_id: str
    question_id: str
    sequence: int
    messages: tuple[SampleMessage, ...]
    #: 記憶として再生した先行設問のうち、回答を引けなかったもの。
    #: 空でなければ、その [assistant] は `MISSING_ANSWER` で埋まっている。
    missing_answers: tuple[str, ...] = ()


def sample_prompts(
    session: Session,
    ctx: SessionContext,
    answers: Mapping[str, str] | None = None,
) -> list[PromptSample]:
    """セッションの**全設問**のメッセージ列を、実行時と同じ経路で組み直す。

    `run_session()` と同じループを回す。違うのは、[assistant] に置く回答を
    エンドポイントから得るのではなく `answers`（＝`responses.answer_raw`）から
    引くところだけ。プロンプト組み立ては純関数なので、LLM を呼ばずに再構築しても
    実際に送った内容と一致する。executor に記録用の状態を通さずに済ませるため、
    こうしている。

    `answers` を渡さない／設問が欠けている場合は `MISSING_ANSWER` で埋め、
    その設問IDを `PromptSample.missing_answers` に残す。記憶を持たない設問
    （＝セッションの先頭）は回答を要らないので、この経路を通らない。

    ペルソナが読み込まれていない場合は空（実行対象が無かったということ）。
    """
    persona = ctx.personas.get(session.persona_uuid)
    if persona is None:
        return []

    survey = ctx.survey
    questions = ctx.question_map()
    premise = ctx.premises.get(session.persona_uuid)
    recorded = answers or {}

    turns_by_question: dict[str, ReplayTurn] = {}
    samples: list[PromptSample] = []

    for unit in session.units:
        question = questions[unit.question_id]
        options, _ = presented_options(question, survey.panel.seed, session.persona_uuid)
        current = ReplayTurn(
            stimuli=unit.stimuli_to_present or _fallback_stimuli(unit, ctx),
            question=question,
            options=tuple(options),
        )

        replayed = [turns_by_question[qid] for qid in unit.remembers]
        messages = replayed_messages(
            persona, survey.prompt, survey.persona_card, [*replayed, current], premise
        )

        samples.append(
            PromptSample(
                persona_uuid=session.persona_uuid,
                stimulus_id=unit.stimulus_id,
                question_id=unit.question_id,
                sequence=unit.sequence,
                messages=tuple(
                    SampleMessage(role=message.role, content=message.content)
                    for message in messages
                ),
                missing_answers=tuple(qid for qid in unit.remembers if qid not in recorded),
            )
        )

        turns_by_question[unit.question_id] = replace(
            current, answer=recorded.get(unit.question_id, MISSING_ANSWER)
        )

    return samples


def _fallback_stimuli(unit: Unit, ctx: SessionContext) -> tuple[Stimulus, ...]:
    """提示物を持たないユニットを補う。

    スクリーニング（§4.2）はコンセプトを見せないので、`NO_STIMULUS` のときは空を返す
    （`build_screener_sessions()` が手で組む Unit がここを通る）。

    **同時提示（`ALL_STIMULI`）はここで補わない。** `ctx.stimuli` は調査定義の記述順で、
    ペルソナごとの `assigned_stimuli` の順序を持っていない。ここから補うと提示順が
    定義順に化ける。提示順は `_unit()` が `stimulus_for()` 経由で全ユニットに持たせるのが
    正しい経路で、そこを通っていれば `ALL_STIMULI` でこの関数には来ない。
    """
    if unit.stimulus_id == NO_STIMULUS:
        return ()
    return (ctx.stimuli[unit.stimulus_id],)


def _ask(messages, question, option_count: int, ctx: SessionContext):
    """1設問を、パースできるまで最大 `PARSE_MAX_ATTEMPTS` 回試す（§6.3）。

    再送は同じ入力で行う。サンプリングパラメータは指定していないので、揺れを作るのは
    エンドポイント側の非決定性だけ。試行回数は `responses.attempt` に残す。

    出力予算の引き上げは**この試行回数を消費しない**。構造化出力のフォールバックと同じ扱いで、
    `attempt` は「パース失敗を何回やり直したか」の意味を保つ。混ぜると `responses.attempt` から
    パース失敗の頻度が読めなくなる。
    """
    model = ctx.survey.model
    max_tokens = _budget_for(question, model)
    schema = answer_schema(question, option_count)

    def send(budget: int) -> Completion:
        response_format = schema if (schema and ctx.structured_output.enabled) else None
        try:
            return ctx.client.complete(
                messages,
                max_tokens=budget,
                response_format=response_format,
            )
        except StructuredOutputUnsupported as exc:
            # auto なら正規表現パースへ落として同じ試行をやり直す（always なら送出）。
            ctx.structured_output.note_unsupported(exc)
            return ctx.client.complete(messages, max_tokens=budget)

    completion = None
    parsed = None
    for attempt in range(1, PARSE_MAX_ATTEMPTS + 1):
        completion, outcome = complete_within_budget(send, max_tokens=max_tokens)
        ctx.output_budget.note(outcome)

        if outcome.exhausted:
            # ここで打ち切る。番号が取れないので `_needs_retry` は真になるが、残りの試行も
            # 同じように天井まで広げて使い切るだけで、1設問あたりの呼び出しが3倍に膨れる。
            # 得られていた分を記録し、フラグを立てて先へ進む（§6.3 項目4 と同じ扱い）。
            completion = Completion(text=outcome.partial_text, latency_ms=outcome.latency_ms)
            return completion, parse_answer(completion.text, question, option_count), attempt, outcome

        parsed = parse_answer(completion.text, question, option_count)
        if not _needs_retry(question, parsed):
            return completion, parsed, attempt, outcome

    return completion, parsed, PARSE_MAX_ATTEMPTS, outcome


def _budget_for(question, model) -> int:
    """その設問の出力予算（§6.3）。

    自由回答と、理由を書かせる選択式は本文を返させるので番号ぶんでは足りない。
    `reasoning` を先に見るのは、`open` に `reasoning` を書けないため
    （`validate` が止める）——順序が結果を変えることはないが、判定の主従を揃えておく。
    """
    if question.reasoning:
        return model.max_tokens_reasoning
    if question.type is QuestionType.OPEN:
        return model.max_tokens_open
    return model.max_tokens


def _answer_missing(question, parsed) -> bool:
    """選択式で番号が1つも取れなかったか。

    `multi` は `code` を持たない（複数選ばれうるので単一の番号に潰せない）。
    `code is None` で判定すると multi が常に失敗扱いになるため、選択式は `codes` で見る。
    """
    if question.type in (QuestionType.OPEN, QuestionType.NUMERIC):
        return False
    return not parsed.codes


def _needs_retry(question, parsed) -> bool:
    """選択式で番号が取れなかったときだけリトライする。

    範囲外の番号は「答えは返ってきた」のでリトライしない。フラグを立てて先へ進む。
    """
    return _answer_missing(question, parsed)


def _to_record(
    survey_id, unit, completion, parsed, options_order, attempt, question, outcome=None
) -> ResponseRecord:
    record_flags = []
    if _answer_missing(question, parsed):
        record_flags.append(flag_names.PARSE_ERROR)
    if parsed.out_of_range:
        record_flags.append(flag_names.OUT_OF_RANGE)
    if parsed.refusal:
        record_flags.append(flag_names.REFUSAL)
    if attempt > 1:
        record_flags.append(flag_names.RETRIED)
    # 広げて通ったぶんには立てない。回答そのものが欠けている・切り詰められている場合だけ。
    if outcome is not None and outcome.exhausted:
        record_flags.append(flag_names.OUTPUT_LIMIT)

    return ResponseRecord(
        survey_id=survey_id,
        persona_uuid=unit.persona_uuid,
        stimulus_id=unit.stimulus_id,
        question_id=unit.question_id,
        sequence=unit.sequence,
        answer_raw=completion.text,
        answer_codes=list(parsed.codes),
        answer_text=parsed.text,
        answer_reasoning=parsed.reasoning,
        options_order=list(options_order),
        flags=record_flags,
        latency_ms=completion.latency_ms,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        attempt=attempt,
        ts=time.time(),
        model_version=completion.model_version,
    )
