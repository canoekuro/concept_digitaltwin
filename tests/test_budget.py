"""出力予算の引き上げ再送（`persona_sim.llm.budget`）。

Spark もエンドポイントも使わない。`send` を差し替えて、予算の決め方だけを見る。
"""

from __future__ import annotations

import pytest

from persona_sim.llm.budget import (
    OUTPUT_LIMIT_CEILING,
    budgets,
    complete_within_budget,
    output_budget_warning,
)
from persona_sim.llm.client import Completion, LLMError, OutputLimitExceeded


def _sender(*, succeeds_at: int | None = None, partial_text: str = ""):
    """`succeeds_at` 以上の予算で成功する `send`。渡された予算を記録する。

    `FakeClient` の失敗注入はプロンプトのハッシュなので「予算が足りたら成功」を作れない。
    ここは予算そのものが分岐条件なので、専用のスタブが要る。
    """
    budgets_seen: list[int] = []

    def send(budget: int) -> Completion:
        budgets_seen.append(budget)
        if succeeds_at is None or budget < succeeds_at:
            raise OutputLimitExceeded(
                "上限に達した", max_tokens=budget, partial_text=partial_text
            )
        return Completion(text="1", latency_ms=1)

    return send, budgets_seen


# --------------------------------------------------------------------------- #
# 予算の並べ方
# --------------------------------------------------------------------------- #


def test_the_first_budget_is_always_the_configured_one():
    """引き上げは超過したときだけ。最初から広げると、通っていた呼び出しまで高くつく。"""
    assert budgets(100)[0] == 100


def test_budgets_stop_at_the_ceiling():
    """青天井にすると、終わらないプロンプトで延々とトークンを使い続ける。"""
    assert budgets(1000)[-1] <= OUTPUT_LIMIT_CEILING


def test_a_budget_already_at_the_ceiling_is_tried_only_once():
    """同じ枠で投げ直すだけの呼び出しを作らない。広げられないなら1回で見切る。"""
    assert budgets(OUTPUT_LIMIT_CEILING) == (OUTPUT_LIMIT_CEILING,)


def test_budgets_never_repeat_a_value():
    """天井で頭打ちにした結果が重複すると、同じ予算を2回投げることになる。"""
    for base in (8, 100, 256, 512, 2048, 4096, 8192):
        plan = budgets(base)
        assert len(plan) == len(set(plan)), base


# --------------------------------------------------------------------------- #
# 引き上げ再送
# --------------------------------------------------------------------------- #


def test_a_call_that_fits_the_budget_is_not_escalated():
    """超過していないのに広げると、記録上「予算が足りていない」と誤読される。"""
    send, seen = _sender(succeeds_at=0)
    completion, outcome = complete_within_budget(send, max_tokens=100)

    assert completion is not None
    assert seen == [100]
    assert (outcome.escalated, outcome.exhausted) == (False, False)


def test_the_budget_is_raised_until_the_call_goes_through():
    """同じ枠でのバックオフでは通らない。枠を広げることが対処そのもの。"""
    send, seen = _sender(succeeds_at=400)
    completion, outcome = complete_within_budget(send, max_tokens=100)

    assert completion is not None
    assert seen == [100, 400]
    assert (outcome.escalated, outcome.exhausted) == (True, False)
    assert outcome.final_max_tokens == 400


def test_exhaustion_returns_no_completion_instead_of_raising():
    """1設問の予算超過で調査を止めない。呼び出し側が記録して続けられる形で返す。"""
    send, seen = _sender(succeeds_at=None)
    completion, outcome = complete_within_budget(send, max_tokens=100)

    assert completion is None
    assert (outcome.escalated, outcome.exhausted) == (True, True)
    assert seen == list(budgets(100))


def test_exhaustion_keeps_the_partial_text():
    """途中まで生成できていた本文を捨てない。自由回答では唯一残る手がかりになる。"""
    send, _ = _sender(succeeds_at=None, partial_text="ここまでは書けた")
    completion, outcome = complete_within_budget(send, max_tokens=100)

    assert completion is None
    assert outcome.partial_text == "ここまでは書けた"


def test_other_failures_are_not_swallowed():
    """伝送層の失敗や構造化出力の非対応はここの関心事ではない。握り潰すと原因が消える。"""

    def send(budget: int) -> Completion:
        raise LLMError("別の失敗")

    with pytest.raises(LLMError):
        complete_within_budget(send, max_tokens=100)


# --------------------------------------------------------------------------- #
# 警告
# --------------------------------------------------------------------------- #


def test_no_warning_when_the_budget_was_never_hit():
    assert output_budget_warning(escalated=0, exhausted=0) is None


def test_a_single_escalation_is_already_worth_warning_about():
    """広げて通った回答にはフラグが立たない。ここで出さないと兆候がどこにも残らない。"""
    warning = output_budget_warning(escalated=1, exhausted=0)

    assert warning is not None
    assert "[W_OUTPUT_LIMIT]" in warning
    assert "max_tokens" in warning


def test_the_warning_says_when_answers_were_actually_lost():
    """広げて通ったのか、使い切って回答が欠けたのかで打つ手が違う。まとめない。"""
    warning = output_budget_warning(escalated=3, exhausted=2)

    assert warning is not None
    assert "output_limit" in warning
