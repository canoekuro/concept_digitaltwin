"""クロス集計表の算出（`SPEC.md` §7.1）。

このモジュールは **pyspark に依存しない**。純関数だけで構成し、集計の数値そのものを
Spark を起動せずに単体テストできるようにする（`run/parsing.py` と同じ方針）。

決めごと:

- 生データは**提示順**の番号で保存されている（§2.3）。ここで `options_order` を使って
  **定義順に戻してから**集計する。この読み替えを飛ばすと、選択肢をシャッフルした設問で
  結果の意味が変わる
- `n` は**ウェイト適用前**の実数、`%` は**ウェイト適用後**（§7.1）。ウェイトが全て 1.0 なら一致する
- `平均` は選択肢番号を逆順スコア化（5段階なら 1→5点）した平均。**順序尺度のときだけ**出す。
  非順序の選択肢に平均を出しても意味を持たないため
- 品質フラグの立った回答も**除外しない**（§8）。`n` に含めたまま、除いた場合の `n` を併記する
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from persona_sim.aggregate import segments as segment_axes
from persona_sim.panel.schema import Question, QuestionType, SurveyDefinition

#: 値を持たないセルの表示。
BLANK = "-"


@dataclass(frozen=True)
class Answer:
    """集計に使う1回答（`responses` の1行を読み替えたもの）。

    `codes` は**定義順**に直した選択肢番号。`single` / `scale` は1要素、`multi` は
    選ばれたぶん全部、`open` / `numeric` は空。
    """

    persona_uuid: str
    stimulus_id: str
    question_id: str
    codes: tuple[int, ...] = ()
    weight: float = 1.0
    flagged: bool = False
    attributes: Mapping[str, object] = field(default_factory=dict)
    #: `numeric` 設問の値。読めなければ None。
    number: float | None = None
    #: `open` 設問の本文。
    text: str | None = None


@dataclass(frozen=True)
class Metric:
    """1つのセグメント行の集計値。"""

    #: 有効回答者数（ウェイト適用前の実数）。
    n: int
    #: そのうち品質フラグが1つも立っていない数。
    n_unflagged: int
    #: 選択肢ごとの比率（定義順、ウェイト適用後）。0.0〜1.0。
    percentages: tuple[float, ...] = ()
    #: T2B（設問定義の `top_box`）。定義が無ければ None。
    top_box: float | None = None
    #: 逆順スコアの平均。順序尺度でなければ None。
    mean: float | None = None


@dataclass(frozen=True)
class CrosstabRow:
    segment: str
    segment_value: str
    metric: Metric


@dataclass(frozen=True)
class Crosstab:
    """コンセプト × 設問 の1表（§7.1）。

    束ねる単位は設問IDではなく `measure`。設問は `slot`（何番目に提示するコンセプトか）
    ごとに別IDで展開されるので、「コンセプトAの購入意向」を出すには
    `measure: purchase_intent` を持つ設問すべてから、コンセプトAへの回答を集める必要がある。
    どのペルソナがAを何番目に見たかは人によって違う。
    """

    stimulus_id: str
    stimulus_name: str
    #: 集計上の設問の同一性キー（`Question.measure_key`）。
    measure: str
    question_text: str
    question_type: QuestionType
    option_labels: tuple[str, ...]
    rows: tuple[CrosstabRow, ...]
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 番号の読み替え
# --------------------------------------------------------------------------- #


def to_defined_codes(
    answer_codes: Sequence[int] | None, options_order: Sequence[int] | None
) -> tuple[int, ...]:
    """提示順の番号を定義順に戻す（§2.3）。

    `options_order` はその提示順を「定義順での番号」で表したもの。したがって
    定義順の番号は `options_order[提示順の番号 - 1]` で引ける。
    シャッフルしていない設問では入力と同じ値が返る。

    範囲外の番号（`out_of_range`）は読み替えられないので、そのまま返して集計側で落とす。
    """
    order = list(options_order or ())
    codes = []
    for code in answer_codes or ():
        if 1 <= code <= len(order):
            codes.append(order[code - 1])
        else:
            codes.append(code)
    return tuple(codes)


def option_count(question: Question) -> int:
    return len(question.options)


def is_tabulated(question: Question) -> bool:
    """クロス集計表を作る設問か。自由回答は §7.3 の長持ちテーブルへ回す。"""
    return question.type is not QuestionType.OPEN


def tabulated_measures(survey: SurveyDefinition) -> list[Question]:
    """集計対象の設問を `measure` ごとに畳んだ代表の一覧（§8）。

    同じ `measure` を持つ設問は `type` と `options` が一致していること（`validate` が
    E6 で保証する）。だから代表1つから表頭も指標の出し方も決まる。並びは調査定義に
    最初に現れた順で、設問の記述順がそのまま表の並びになる。
    """
    representatives: dict[str, Question] = {}
    for question in survey.questions:
        if is_tabulated(question):
            representatives.setdefault(question.measure_key, question)
    return list(representatives.values())


def measure_of(survey: SurveyDefinition) -> dict[str, str]:
    """設問ID → `measure` の対応。回答を measure に畳むときに使う。"""
    return {question.id: question.measure_key for question in survey.questions}


# --------------------------------------------------------------------------- #
# 集計
# --------------------------------------------------------------------------- #


def compute_metric(question: Question, answers: Sequence[Answer]) -> Metric:
    """1グループ（あるセグメント値の回答群）の集計値を出す。"""
    if question.type is QuestionType.NUMERIC:
        return _numeric_metric(answers)

    count = option_count(question)
    valid = [answer for answer in answers if _valid_codes(answer, count)]
    n = len(valid)
    n_unflagged = sum(1 for answer in valid if not answer.flagged)
    if n == 0:
        return Metric(n=0, n_unflagged=0, percentages=tuple(0.0 for _ in range(count)))

    # 分母は「回答者のウェイト合計」。multi では選択の合計が分母を超えるので、
    # 各選択肢の比率の和は 100% を超えうる（§7.1 の注記）。
    denominator = sum(answer.weight for answer in valid)
    weighted = [0.0] * count
    for answer in valid:
        for code in set(answer.codes):
            if 1 <= code <= count:
                weighted[code - 1] += answer.weight

    percentages = tuple(
        (value / denominator if denominator else 0.0) for value in weighted
    )
    top_box = _top_box(question, percentages)
    return Metric(
        n=n,
        n_unflagged=n_unflagged,
        percentages=percentages,
        top_box=top_box,
        mean=_mean_score(question, valid, denominator),
    )


def _valid_codes(answer: Answer, count: int) -> bool:
    """集計に使える回答か。範囲内の番号が1つ以上あること。"""
    return any(1 <= code <= count for code in answer.codes)


def _top_box(question: Question, percentages: Sequence[float]) -> float | None:
    if not question.top_box:
        return None
    return sum(
        percentages[code - 1] for code in question.top_box if 1 <= code <= len(percentages)
    )


def _mean_score(question: Question, valid: Sequence[Answer], denominator: float) -> float | None:
    """逆順スコアの加重平均（§7.1）。

    順序尺度でなければ None。複数回答は「1人1スコア」にできないので対象外。
    """
    if not question.is_ordinal() or question.type is QuestionType.MULTI:
        return None
    count = option_count(question)
    if not count or not denominator:
        return None
    total = 0.0
    for answer in valid:
        code = next((c for c in answer.codes if 1 <= c <= count), None)
        if code is None:
            continue
        total += answer.weight * (count + 1 - code)
    return total / denominator


def _numeric_metric(answers: Sequence[Answer]) -> Metric:
    """`numeric` 設問。選択肢が無いので比率は出さず、平均だけを出す。"""
    valid = [answer for answer in answers if answer.number is not None]
    if not valid:
        return Metric(n=0, n_unflagged=0)
    denominator = sum(answer.weight for answer in valid)
    mean = (
        sum(answer.weight * (answer.number or 0.0) for answer in valid) / denominator
        if denominator
        else None
    )
    return Metric(
        n=len(valid),
        n_unflagged=sum(1 for answer in valid if not answer.flagged),
        mean=mean,
    )


def group_by_segments(
    answers: Sequence[Answer], segments: Sequence[str]
) -> list[tuple[str, str, list[Answer]]]:
    """セグメント軸ごとに `(軸, 値, 回答群)` へ畳む。

    値の並びは決定論的にするため文字列順に揃える（`total` は1行だけ）。
    """
    grouped: list[tuple[str, str, list[Answer]]] = []
    for segment in segments:
        buckets: dict[str, list[Answer]] = {}
        for answer in answers:
            value = segment_axes.value_of(segment, answer.attributes)
            buckets.setdefault(value, []).append(answer)
        for value in sorted(buckets):
            grouped.append((segment, value, buckets[value]))
    return grouped


def crosstab(
    question: Question,
    stimulus_id: str,
    stimulus_name: str,
    answers: Sequence[Answer],
    segments: Sequence[str],
) -> Crosstab:
    """コンセプト × 設問 の集計表を作る（§7.1）。"""
    rows = [
        CrosstabRow(segment=segment, segment_value=value, metric=compute_metric(question, group))
        for segment, value, group in group_by_segments(answers, segments)
    ]
    notes: list[str] = []
    if question.type is QuestionType.MULTI:
        notes.append("複数回答のため、選択肢の比率の合計は100%を超えることがある")
    if question.type is not QuestionType.NUMERIC and not question.is_ordinal():
        notes.append("順序尺度ではないため平均は算出していない")
    return Crosstab(
        stimulus_id=stimulus_id,
        stimulus_name=stimulus_name,
        measure=question.measure_key,
        question_text=question.text,
        question_type=question.type,
        option_labels=tuple(question.options),
        rows=tuple(rows),
        notes=tuple(notes),
    )


def _by_stimulus(answers: Sequence[Answer]) -> dict[str, list[Answer]]:
    """`stimulus_id` ごとに畳む。

    表ごとに `answers` を線形走査すると、コンセプト数の回数だけ全件を舐めることになる。
    1,000人 × 10コンセプト × 10設問（＝10万件）では待ち時間に直に効く。
    """
    buckets: dict[str, list[Answer]] = {}
    for answer in answers:
        buckets.setdefault(answer.stimulus_id, []).append(answer)
    return buckets


def _by_measure(answers: Sequence[Answer], measures: Mapping[str, str]) -> dict[str, list[Answer]]:
    """`measure` ごとに畳む。`_by_stimulus()` と同じ理由。

    設問IDではなく measure で畳むのが要点。同じコンセプトへの回答でも、そのコンセプトを
    何番目に見たかによって答えた設問IDが違う（`q_intent_1` / `q_intent_2` …）。
    設問ID単位で畳むと1つの指標が slot の数だけに割れる。
    """
    buckets: dict[str, list[Answer]] = {}
    for answer in answers:
        key = measures.get(answer.question_id, answer.question_id)
        buckets.setdefault(key, []).append(answer)
    return buckets


def crosstabs(
    survey: SurveyDefinition, answers: Sequence[Answer], segments: Sequence[str]
) -> list[Crosstab]:
    """調査全体のクロス集計表（コンセプト × measure ごとに1表）。"""
    names = {stimulus.id: stimulus.name for stimulus in survey.stimuli}
    measures = measure_of(survey)
    # 表ごとに全件を走査しない（コンセプト数 × 設問数の回数だけ舐めることになる）。
    by_stimulus = _by_stimulus(answers)
    tables = []
    for stimulus in survey.stimuli:
        by_measure = _by_measure(by_stimulus.get(stimulus.id, []), measures)
        for question in tabulated_measures(survey):
            tables.append(
                crosstab(
                    question,
                    stimulus.id,
                    names.get(stimulus.id, stimulus.id),
                    by_measure.get(question.measure_key, []),
                    segments,
                )
            )
    return tables
