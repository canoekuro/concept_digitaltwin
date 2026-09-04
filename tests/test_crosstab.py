"""集計の算術（`SPEC.md` §7.1）。

期待値はすべて手計算で置く。Spark を起動しないので、集計の意味が変わる変更は
このファイルが最初に落ちる。
"""

from __future__ import annotations

import pytest

from persona_sim.aggregate.crosstab import (
    Answer,
    compute_metric,
    crosstab,
    crosstabs,
    to_defined_codes,
)
from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.schema import Question, QuestionType

INTENT = Question(
    id="q_intent_1",
    measure="q_intent",
    text="購入したいと思いますか。",
    type=QuestionType.SINGLE,
    options=("ぜひ", "やや", "どちらとも", "あまり", "まったく"),
    top_box=(1, 2),
)


def answer(code: int | None, **kwargs) -> Answer:
    return Answer(
        persona_uuid=kwargs.pop("persona_uuid", "p1"),
        stimulus_id=kwargs.pop("stimulus_id", "c1"),
        question_id=kwargs.pop("question_id", "q_intent_1"),
        codes=() if code is None else (code,),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 番号の読み替え（§2.3）
# --------------------------------------------------------------------------- #


def test_defined_codes_pass_through_when_not_shuffled():
    assert to_defined_codes([2], [1, 2, 3, 4, 5]) == (2,)


def test_defined_codes_translate_shuffled_order():
    # 提示順 [3,1,2] は「1番目に見せたのが定義順3番」の意味。提示順の2番＝定義順1番。
    assert to_defined_codes([2], [3, 1, 2]) == (1,)
    assert to_defined_codes([1], [3, 1, 2]) == (3,)


def test_defined_codes_keep_out_of_range_untouched():
    """範囲外の番号は読み替えられない。集計側で落とすのでそのまま返す。"""
    assert to_defined_codes([9], [1, 2, 3]) == (9,)


def test_defined_codes_handle_multiple_selections():
    assert to_defined_codes([1, 3], [2, 3, 1]) == (2, 1)


# --------------------------------------------------------------------------- #
# 比率・T2B・平均
# --------------------------------------------------------------------------- #


def test_percentages_and_top_box():
    answers = [answer(1), answer(1), answer(2), answer(4), answer(5)]
    metric = compute_metric(INTENT, answers)

    assert metric.n == 5
    assert metric.percentages == pytest.approx([0.4, 0.2, 0.0, 0.2, 0.2])
    # T2B = 選択肢1と2の合計 = 40% + 20%
    assert metric.top_box == pytest.approx(0.6)


def test_mean_is_reverse_scored():
    """5段階なら 1→5点。選択肢1を選んだ人が最高点になる（§7.1）。"""
    metric = compute_metric(INTENT, [answer(1), answer(5)])
    # (5 + 1) / 2 = 3.0
    assert metric.mean == pytest.approx(3.0)

    metric = compute_metric(INTENT, [answer(2), answer(2), answer(3)])
    # (4 + 4 + 3) / 3
    assert metric.mean == pytest.approx(11 / 3)


def test_weight_moves_percentages_but_not_n():
    """n はウェイト適用前の実数、% はウェイト適用後（§7.1）。"""
    answers = [answer(1, weight=3.0), answer(5, weight=1.0)]
    metric = compute_metric(INTENT, answers)

    assert metric.n == 2
    assert metric.percentages[0] == pytest.approx(0.75)
    assert metric.percentages[4] == pytest.approx(0.25)
    # 加重平均: (3×5 + 1×1) / 4
    assert metric.mean == pytest.approx(4.0)


def test_unweighted_and_weighted_agree_when_all_weights_are_one():
    plain = compute_metric(INTENT, [answer(1), answer(2)])
    weighted = compute_metric(INTENT, [answer(1, weight=1.0), answer(2, weight=1.0)])
    assert plain == weighted


def test_flagged_answers_stay_in_n_and_only_leave_n_unflagged():
    """フラグは立てるだけで除外しない（§8）。"""
    answers = [answer(1), answer(2, flagged=True), answer(2, flagged=True)]
    metric = compute_metric(INTENT, answers)

    assert metric.n == 3
    assert metric.n_unflagged == 1
    assert metric.percentages[1] == pytest.approx(2 / 3)


def test_answers_without_codes_are_not_counted():
    """パース失敗（番号なし）は母数に入らない。"""
    metric = compute_metric(INTENT, [answer(1), answer(None)])
    assert metric.n == 1
    assert metric.percentages[0] == pytest.approx(1.0)


def test_out_of_range_codes_are_dropped_from_the_base():
    metric = compute_metric(INTENT, [answer(1), answer(99)])
    assert metric.n == 1


def test_empty_group_yields_zero_metric():
    metric = compute_metric(INTENT, [])
    assert metric.n == 0
    assert metric.percentages == (0.0, 0.0, 0.0, 0.0, 0.0)


def test_no_mean_for_non_ordinal_question():
    """順序尺度でない選択肢に平均を出しても意味を持たない。"""
    non_ordinal = Question(
        id="q_brand",
        text="どれを知っていますか。",
        type=QuestionType.SINGLE,
        options=("A", "B", "C"),
    )
    metric = compute_metric(non_ordinal, [answer(1, question_id="q_brand")])
    assert metric.mean is None
    assert metric.top_box is None


# --------------------------------------------------------------------------- #
# multi / numeric
# --------------------------------------------------------------------------- #


MULTI = Question(
    id="q_uses",
    text="どの場面で飲みますか。",
    type=QuestionType.MULTI,
    options=("夕食時", "入浴後", "外出先"),
)


def test_multi_counts_respondents_as_the_base():
    answers = [
        Answer(persona_uuid="p1", stimulus_id="c1", question_id="q_uses", codes=(1, 2)),
        Answer(persona_uuid="p2", stimulus_id="c1", question_id="q_uses", codes=(1,)),
    ]
    metric = compute_metric(MULTI, answers)

    assert metric.n == 2
    # 分母は回答者2人。合計は100%を超える。
    assert metric.percentages == pytest.approx([1.0, 0.5, 0.0])
    assert sum(metric.percentages) > 1.0


def test_multi_ignores_duplicate_selections_of_the_same_option():
    answers = [Answer(persona_uuid="p1", stimulus_id="c1", question_id="q_uses", codes=(1, 1))]
    metric = compute_metric(MULTI, answers)
    assert metric.percentages[0] == pytest.approx(1.0)


def test_multi_has_no_mean():
    """複数回答は1人1スコアにできないので平均を出さない。"""
    with_top_box = Question(
        id="q_uses", text="", type=QuestionType.MULTI, options=("A", "B"), top_box=(1,)
    )
    metric = compute_metric(
        with_top_box,
        [Answer(persona_uuid="p1", stimulus_id="c1", question_id="q_uses", codes=(1,))],
    )
    assert metric.mean is None
    assert metric.top_box == pytest.approx(1.0)


NUMERIC = Question(id="q_price", text="いくらなら買いますか。", type=QuestionType.NUMERIC)


def test_numeric_averages_the_values():
    answers = [
        Answer(persona_uuid="p1", stimulus_id="c1", question_id="q_price", number=200.0),
        Answer(persona_uuid="p2", stimulus_id="c1", question_id="q_price", number=300.0),
    ]
    metric = compute_metric(NUMERIC, answers)
    assert metric.n == 2
    assert metric.mean == pytest.approx(250.0)
    assert metric.percentages == ()


def test_numeric_skips_unparsed_values():
    answers = [
        Answer(persona_uuid="p1", stimulus_id="c1", question_id="q_price", number=None),
        Answer(persona_uuid="p2", stimulus_id="c1", question_id="q_price", number=100.0),
    ]
    assert compute_metric(NUMERIC, answers).n == 1


# --------------------------------------------------------------------------- #
# セグメント展開
# --------------------------------------------------------------------------- #


def test_crosstab_expands_every_requested_segment():
    answers = [
        answer(1, persona_uuid="p1", attributes={"sex": "男", "age_band_10": "20代"}),
        answer(5, persona_uuid="p2", attributes={"sex": "女", "age_band_10": "20代"}),
    ]
    table = crosstab(INTENT, "c1", "コンセプトA", answers, ["total", "sex", "sex_x_age_band_10"])

    labels = [(row.segment, row.segment_value) for row in table.rows]
    assert ("total", "全体") in labels
    assert ("sex", "男") in labels
    assert ("sex", "女") in labels
    assert ("sex_x_age_band_10", "男 × 20代") in labels

    total = next(row for row in table.rows if row.segment == "total")
    assert total.metric.n == 2
    male = next(row for row in table.rows if row.segment_value == "男")
    assert male.metric.n == 1
    assert male.metric.percentages[0] == pytest.approx(1.0)


def test_missing_attribute_becomes_an_explicit_bucket():
    answers = [answer(1, attributes={"sex": None})]
    table = crosstab(INTENT, "c1", "コンセプトA", answers, ["sex"])
    assert table.rows[0].segment_value == "(不明)"


# --------------------------------------------------------------------------- #
# 調査全体
# --------------------------------------------------------------------------- #


def test_crosstabs_cover_every_stimulus_and_closed_question(survey_dict):
    survey = survey_from_dict(survey_dict)
    tables = crosstabs(survey, [], ["total"])

    keys = {(table.stimulus_id, table.measure) for table in tables}
    # コンセプト3件 × 選択式1問。自由回答（q_reason）は表を作らない。
    assert keys == {("c1", "q_intent"), ("c2", "q_intent"), ("c3", "q_intent")}
