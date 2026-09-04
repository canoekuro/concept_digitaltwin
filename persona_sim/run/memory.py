"""設問ごとの記憶の解決と、セッションへの分割（`SPEC_PHASE1.md` §5.1, §6.2）。

**参照の最小単位は設問。** 各設問の `remember` が「どの設問の Q&A を持ったまま
この設問に入るか」を決め、そこから逆算して実行単位（セッション）が決まる。

**`remember` の意味は調査定義の中だけで完結する。** 調査全体の設定を見て変わることはない。

| `remember` | 覚える範囲 | できるセッション |
|---|---|---|
| 書かない／`none` | 何も持たない | その設問だけで1つ |
| `all` | それまでに聞いた設問すべて（コンセプトをまたぐ） | 先行設問と同じ1つ |
| 設問IDのリスト | 列挙した設問だけ | 参照先と同じ1つ |

「同じコンセプトの中だけ覚える」は設問IDを並べて書く。そのための専用の値は用意しない
——コンセプト調査の都合を `remember` に持ち込むと、`all` の意味が文脈で変わってしまう。

このモジュールは **pyspark に依存しない**。`run/prompt.py` と同じく純関数だけで構成し、
Spark を起動せずに単体テストできるようにする。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from persona_sim.errors import SurveyDefinitionError
from persona_sim.panel.schema import (
    Question,
    Remember,
    RememberMode,
    Stimulus,
    SurveyDefinition,
)


def ask_order(survey: SurveyDefinition) -> tuple[Question, ...]:
    """ペルソナに聞く順番。`slot` 昇順、同じ slot の中は定義順。

    並べ替えは安定ソートなので、同じ slot 内の記述順は保たれる。
    """
    return tuple(sorted(survey.questions, key=lambda question: question.slot))


def resolve_remember(survey: SurveyDefinition) -> dict[str, Remember]:
    """設問IDごとの `remember`。

    調査全体の設定を混ぜないので、ここは設問が持っている値をそのまま並べるだけ。
    実際にどの設問を再生するかへの展開は `plan_remembers()` が行う。
    """
    return {question.id: question.remember for question in survey.questions}


def _replayed(remember: Remember, preceding: Sequence[Question]) -> tuple[str, ...]:
    """1設問が実際に再生する先行設問のIDを、ask order 順で返す。

    `preceding` は ask order 上でこの設問より前にある設問（前から順）。**分岐は
    モードだけで決まる**——`ALL` はコンセプトをまたいで先行設問すべて。

    `SELECTED` は列挙どおりで**推移しない**。Q4 が Q3 を、Q3 が Q1 を覚えていても、
    Q4 が見るのは Q3 だけ。列挙の順ではなく ask order に整列して再生するのは、
    会話履歴として自然な時系列に並べるため。
    """
    match remember.mode:
        case RememberMode.NONE:
            return ()
        case RememberMode.ALL:
            return tuple(q.id for q in preceding)
        case _:  # RememberMode.SELECTED
            selected = set(remember.question_ids)
            return tuple(q.id for q in preceding if q.id in selected)


def plan_remembers(survey: SurveyDefinition) -> dict[str, tuple[str, ...]]:
    """設問IDごとに、再生する先行設問のIDを ask order 順で返す。

    ペルソナに依らない（`slot` は提示順の位置であって、どのコンセプトが当たるかとは
    独立している）。だから1度計算すれば全ペルソナで使い回せる。

    前方参照——ask order 上で自分より後ろ、または自分自身への参照——はここで落ちる。
    `validate` が先に E6 で止める前提だが、黙って無視すると「覚えているつもり」の
    記録だけが残るので、通り抜けたときのために例外にする。
    """
    order = ask_order(survey)
    resolved = resolve_remember(survey)
    plans: dict[str, tuple[str, ...]] = {}

    for index, question in enumerate(order):
        remember = resolved[question.id]
        preceding = order[:index]
        if remember.mode is RememberMode.SELECTED:
            available = {q.id for q in preceding}
            missing = [qid for qid in remember.question_ids if qid not in available]
            if missing:
                raise SurveyDefinitionError(
                    f"questions[{question.id}].remember: {', '.join(missing)} は"
                    f"この設問より前に聞かれない。まだ答えていない設問の記憶は持てない"
                )
        plans[question.id] = _replayed(remember, preceding)
    return plans


def session_groups(survey: SurveyDefinition) -> list[tuple[str, ...]]:
    """設問IDを、逐次に実行しなければならない塊にまとめる。

    「設問 A が設問 B を覚えている」なら A と B は同じセッションに入る（B の回答が
    出てからでないと A を聞けないため）。この連結成分が、そのまま実行単位であり
    再開単位になる。

    現行3値との対応はモジュール docstring のとおりで、**分割は完全に一致する**。
    記憶を持たない設問は依存を持たないので単独のセッションになり、既定モード
    （`memory: none`）の並列度が落ちない。

    戻り値の各要素は ask order 順の設問ID、全体も先頭設問の ask order 順に並ぶ。
    """
    order = ask_order(survey)
    position = {question.id: index for index, question in enumerate(order)}
    plans = plan_remembers(survey)

    parent = {question.id: question.id for question in order}

    def find(qid: str) -> str:
        while parent[qid] != qid:
            parent[qid] = parent[parent[qid]]
            qid = parent[qid]
        return qid

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        # 若い方（ask order で先）を根にする。成分の先頭が安定して決まる。
        if position[left_root] > position[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root

    for question in order:
        for remembered in plans[question.id]:
            union(question.id, remembered)

    groups: dict[str, list[str]] = {}
    for question in order:
        groups.setdefault(find(question.id), []).append(question.id)
    return [tuple(members) for _, members in sorted(groups.items(), key=lambda kv: position[kv[0]])]


def stimulus_for(
    question: Question,
    assigned: Sequence[str],
    stimuli_by_id: Mapping[str, Stimulus],
) -> tuple[Stimulus, ...]:
    """その設問で提示するコンセプト。`slot` 番目の1件。

    ここが `assigned`（＝`panels.assigned_stimuli`）だけを見ているのが要点。調査定義の
    記述順（`survey.stimuli`）から引くと、ペルソナごとの提示順が失われる
    （`SPEC_PHASE1.md` §9.1 の再現性が壊れる）。

    範囲外の `slot` は読み込みの時点で止まる（`loader._reject_uncovered_slots()`）。
    **それでもここで確かめるのは、記録から復元した定義など別経路で来うるため。** 素の
    IndexError だと、100人ぶんのセッションが「list index out of range」で落ちるだけで
    調査定義のどこが悪いのか分からない。
    """
    if not 1 <= question.slot <= len(assigned):
        raise SurveyDefinitionError(
            f"questions[{question.id}].slot: {question.slot} は範囲外。"
            f"このペルソナが評価するコンセプトは {len(assigned)} 件なので"
            f" 1〜{len(assigned)} で指定すること。`panel.validate.validate_static()` で全件を確認できる"
        )
    return (stimuli_by_id[assigned[question.slot - 1]],)
