"""スクリーニングの判定と前提ブロック（`persona_sim.panel.screening`）。

Spark を使わない部分だけを見る。方式ごとの振る舞いの違いが要点。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.schema import (
    PersonaCardConfig,
    PromptHeadings,
    ScreenerLogic,
    ScreenerMode,
    ScreeningConfig,
)
from persona_sim.panel.screening import (
    ask_premise,
    assume_premise,
    build_screener_sessions,
    incidence_from_panels,
    judge,
    persona_passed,
    question_passed,
    screener_session_count,
    screening_fingerprint,
    skipped_session_count,
    to_question,
    unreachable_cells,
)
from persona_sim.run.prompt import persona_card
from persona_sim.run.session import NO_STIMULUS
from tests.conftest import base_survey_dict

BEER = {
    "id": "sc1",
    "text": "ビールをどのくらいの頻度で飲みますか。",
    "label": "ビールの飲用頻度",
    "type": "single",
    "options": ["週2回以上", "週1回", "月2〜3回", "月1回", "それ以下・飲まない"],
    "pass_if": [1, 2, 3, 4],
    "premise": "ビールを月1回以上飲む",
}
AGE_CHECK = {
    "id": "sc2",
    "text": "自分で酒類を購入しますか。",
    "type": "single",
    "options": ["する", "しない"],
    "pass_if": [1],
    "premise": "自分で酒類を購入する",
}


#: `assume` / `infer` の書き方。`ask` の premise と同じ文言を自然言語で並べる。
CONDITIONS = ["ビールを月1回以上飲む", "自分で酒類を購入する"]


def _survey(**screener_overrides):
    """方式に合った形のスクリーナーを持つ調査定義。

    `ask` は `questions`、`assume` / `infer` は `conditions`（§4.2）。
    """
    data = base_survey_dict()
    mode = screener_overrides.get("mode", "ask")
    screener: dict = {"questions": [BEER, AGE_CHECK]} if mode == "ask" else {"conditions": CONDITIONS}
    screener.update(screener_overrides)
    data["screening"] = screener
    return survey_from_dict(data)


def _screener(**overrides):
    return _survey(**overrides).screening


# --------------------------------------------------------------------------- #
# 通過判定
# --------------------------------------------------------------------------- #


def test_question_passes_when_code_is_in_pass_if():
    question = _screener().questions[0]
    assert question_passed(question, [1]) is True
    assert question_passed(question, [4]) is True
    assert question_passed(question, [5]) is False


def test_parse_failure_is_treated_as_not_passing():
    """番号が取れなかった回答で通過させると、条件を満たさない人が混ざる。"""
    question = _screener().questions[0]
    assert question_passed(question, []) is False


def test_multi_passes_when_any_selected_code_qualifies():
    data = base_survey_dict()
    data["screening"] = {
        "questions": [
            {
                "id": "sc1",
                "text": "飲むものを選んでください。",
                "type": "multi",
                "options": ["ビール", "ワイン", "日本酒"],
                "pass_if": [1],
                "premise": "ビールを飲む",
            }
        ]
    }
    question = survey_from_dict(data).screening.questions[0]
    assert question_passed(question, [2, 1]) is True
    assert question_passed(question, [2, 3]) is False


def test_logic_all_requires_every_question():
    screener = _screener(logic="all")
    assert persona_passed(screener, {"sc1": [1], "sc2": [1]}) is True
    assert persona_passed(screener, {"sc1": [1], "sc2": [2]}) is False


def test_logic_any_requires_one_question():
    screener = _screener(logic="any")
    assert screener.logic is ScreenerLogic.ANY
    assert persona_passed(screener, {"sc1": [1], "sc2": [2]}) is True
    assert persona_passed(screener, {"sc1": [5], "sc2": [2]}) is False


def test_missing_answer_fails_under_all():
    screener = _screener(logic="all")
    assert persona_passed(screener, {"sc1": [1]}) is False


def test_judge_separates_passed_and_failed():
    screener = _screener()
    verdict = judge(
        screener,
        {"u1": {"sc1": [1], "sc2": [1]}, "u2": {"sc1": [5], "sc2": [1]}},
    )
    assert verdict.passed == {"u1"}
    assert verdict.failed == {"u2"}
    assert verdict.tested == {"u1", "u2"}


# --------------------------------------------------------------------------- #
# 前提ブロック（§6.1）
# --------------------------------------------------------------------------- #


def test_ask_premise_uses_the_chosen_option_verbatim():
    """本人が選んだ選択肢をそのまま載せる。言い換えると答えていない内容が混ざる。"""
    premise = ask_premise(_screener(), {"sc1": [3], "sc2": [1]})
    assert premise is not None
    assert premise.heading == PromptHeadings().ask_premise
    assert premise.lines == ("ビールの飲用頻度: 月2〜3回", "自分で酒類を購入しますか。: する")


def test_ask_premise_falls_back_to_question_text_without_label():
    premise = ask_premise(_screener(), {"sc2": [1]})
    assert premise is not None
    assert premise.lines == ("自分で酒類を購入しますか。: する",)


def test_ask_premise_is_none_without_answers():
    assert ask_premise(_screener(), {}) is None


def test_assume_premise_uses_survey_definition_wording():
    premise = assume_premise(_screener(mode="assume"))
    assert premise is not None
    assert premise.heading == PromptHeadings().assume_premise
    assert premise.lines == ("ビールを月1回以上飲む", "自分で酒類を購入する")


def test_premise_block_is_appended_to_persona_card():
    persona = {"sex": "男", "age": 34, "prefecture": "東京都", "persona": "会社員。"}
    premise = assume_premise(_screener(mode="assume"))
    card = persona_card(persona, PersonaCardConfig(), premise)

    assert f"【{PromptHeadings().assume_premise}】" in card
    assert "- ビールを月1回以上飲む" in card
    # プロフィールの後ろに来ること
    assert card.index("会社員。") < card.index(PromptHeadings().assume_premise)


def test_persona_card_has_no_premise_block_without_screener():
    persona = {"sex": "男", "age": 34, "persona": "会社員。"}
    card = persona_card(persona, PersonaCardConfig())
    assert PromptHeadings().ask_premise not in card
    assert PromptHeadings().assume_premise not in card


# --------------------------------------------------------------------------- #
# セッション組み立て
# --------------------------------------------------------------------------- #


def test_screener_questions_are_never_shuffled():
    """スクリーナーは頻度尺度が大半で、順序を崩す意味が無い。"""
    question = to_question(_screener().questions[0])
    assert question.randomize_options is False
    assert question.options == tuple(BEER["options"])


def test_sessions_have_no_stimulus():
    """スクリーナーはコンセプトを見せない。"""
    sessions = build_screener_sessions(_screener(), ["u1", "u2"])
    assert len(sessions) == 4  # 2人 × 2設問
    for session in sessions:
        assert len(session.units) == 1
        assert session.units[0].stimulus_id == NO_STIMULUS
        assert session.units[0].stimuli_to_present == ()


# --------------------------------------------------------------------------- #
# インシデンスと見積もり
# --------------------------------------------------------------------------- #


def test_incidence_counts_only_judged_rows():
    """判定前の候補は分母に入れない。"""
    rows = [
        {"cell_id": "A", "role": "main"},
        {"cell_id": "A", "role": "reserve"},
        {"cell_id": "A", "role": "screened_out"},
        {"cell_id": "A", "role": "screened_out"},
        {"cell_id": "B", "role": "candidate"},
    ]
    incidence = incidence_from_panels(rows)
    assert incidence["A"] == 0.5
    assert incidence["total"] == 0.5
    assert "B" not in incidence


def test_incidence_is_empty_without_judged_rows():
    assert incidence_from_panels([{"cell_id": "A", "role": "candidate"}]) == {}


@pytest.mark.parametrize("mode", ["ask", "assume"])
def test_screener_session_count_is_the_ask_cost(mode):
    """`assume` でも「ask ならいくらか」を出せる必要がある（方式選択の材料）。"""
    survey = _survey(mode=mode, oversample_factor=3)
    assert survey.screening.mode is ScreenerMode(mode)
    assert screener_session_count(survey) == survey.panel.size * 3 * 2


# --------------------------------------------------------------------------- #
# 判定設定の指紋（再利用の可否）
# --------------------------------------------------------------------------- #


def _infer_survey(**overrides):
    return _survey(mode="infer", **overrides)


def test_fingerprint_is_stable_for_the_same_definition():
    assert screening_fingerprint(_infer_survey()) == screening_fingerprint(_infer_survey())


def test_fingerprint_is_empty_without_a_screener():
    assert screening_fingerprint(survey_from_dict(base_survey_dict())) == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", {"system": "別の system"}),
        ("prompt", {"rule": "別の rule"}),
        ("model", {"deployment": "別のエンドポイント"}),
        ("model", {"max_tokens": 999}),
        ("persona_card", {"include_summary": False}),
        ("batch_size", 7),
        ("conditions", ["別の条件"]),
    ],
)
def test_fingerprint_changes_when_the_judgement_changes(field, value):
    """判定の中身を変えるものはすべて指紋に効く。

    効かないと、プロンプトやモデルを変えても古い判定が再利用され、
    「いくら直しても結果が変わらない」になる（docs/issues/screeningの問題.md）。
    """
    base = screening_fingerprint(_infer_survey())
    assert screening_fingerprint(_infer_survey(**{field: value})) != base


def test_fingerprint_ignores_oversample_factor():
    """再試行で倍になるだけで判定の中身は変わらない。

    ここを含めてしまうと、通過者不足の再試行のたびに全候補を聞き直すことになる。
    """
    base = screening_fingerprint(_infer_survey(oversample_factor=4))
    assert screening_fingerprint(_infer_survey(oversample_factor=32)) == base


def test_fingerprint_covers_ask_as_well():
    """`ask` も設問文を変えれば別の回答になる。指紋は方式によらず持つ。"""
    base = screening_fingerprint(_survey(mode="ask"))
    changed = _survey(mode="ask")
    assert base != ""
    assert screening_fingerprint(_survey(mode="ask", logic="any")) != base
    assert screening_fingerprint(changed) == base


# --------------------------------------------------------------------------- #
# 再試行の早期停止
# --------------------------------------------------------------------------- #


@dataclass
class _Finalized:
    """`FinalizeResult` のうち `unreachable_cells` が見る部分だけ。"""

    achieved: dict
    reserve: dict
    screened_out: dict
    shortfalls: dict


def _finalized(passed: int, judged: int, needed: int = 10) -> _Finalized:
    return _Finalized(
        achieved={"A": min(passed, needed)},
        reserve={"A": max(0, passed - needed)},
        screened_out={"A": judged - passed},
        shortfalls={"A": needed - min(passed, needed)},
    )


def test_unreachable_when_incidence_is_far_too_low():
    """課題の実データ相当（必要10・通過3・判定320＝0.9%）。

    32倍まで上げても320名までしか増やせず、必要な候補は約1067名。
    上限まで試す前に止め、見積もりを添えて返す。
    """
    reasons = unreachable_cells(_finalized(passed=3, judged=320), {"A": 10}, 4, 3)
    assert "A" in reasons
    assert "1067" in reasons["A"]
    assert "0.9%" in reasons["A"]


def test_unreachable_when_nobody_passes():
    """通過0は何倍にしても0。固定閾値を持たなくてもここで止まる。"""
    reasons = unreachable_cells(_finalized(passed=0, judged=40), {"A": 10}, 4, 3)
    assert "0名" in reasons["A"]


def test_reachable_incidence_is_not_stopped():
    """通過率50%なら倍率を上げれば届く。止めてはいけない。"""
    assert unreachable_cells(_finalized(passed=5, judged=10), {"A": 10}, 4, 3) == {}


def test_remaining_attempts_change_the_verdict():
    """同じ通過率でも、あと何回倍にできるかで到達可能性が変わる。

    通過率10%・倍率4なら、残り3回（上限32倍）では届くが、残り0回では届かない。
    """
    finalized = _finalized(passed=4, judged=40)
    assert unreachable_cells(finalized, {"A": 10}, 4, 3) == {}
    assert "A" in unreachable_cells(finalized, {"A": 10}, 4, 0)


def test_cells_without_judgement_are_left_alone():
    """判定していないセルは通過率が出せない。0除算もしない。"""
    finalized = _Finalized(
        achieved={"A": 0}, reserve={"A": 0}, screened_out={"A": 0}, shortfalls={"A": 10}
    )
    assert unreachable_cells(finalized, {"A": 10}, 4, 3) == {}


# --------------------------------------------------------------------------- #
# スキップ数の単位（`docs/issues/20260807002.md` M7）
# --------------------------------------------------------------------------- #


def _config(mode: ScreenerMode, **overrides) -> ScreeningConfig:
    base: dict = {"mode": mode}
    if mode is ScreenerMode.ASK:
        base["questions"] = ()
    else:
        base["conditions"] = ("週1回以上飲む",)
    base.update(overrides)
    return ScreeningConfig(**base)


def test_ask_counts_skipped_sessions_per_question():
    """`ask` は1設問＝1セッション。人数 × 設問数で数える。"""
    screener = _config(ScreenerMode.ASK)
    assert skipped_session_count(screener, 30, ("q1", "q2")) == 60


def test_infer_counts_skipped_sessions_in_batches():
    """`infer` は1バッチ＝1呼び出し。人数のままだと `sessions_total` と単位が違う。

    これが揃っていないと、CLI が両方を「バッチ」というラベルで並べるため
    「1バッチ中 380 スキップ」のように内訳が総数を超えて見える。
    """
    screener = _config(ScreenerMode.INFER, batch_size=10)
    # 判定済み120人 = 12バッチぶん。120 と数えてはいけない。
    assert skipped_session_count(screener, 120, ("_infer",)) == 12


def test_infer_rounds_a_partial_batch_up():
    screener = _config(ScreenerMode.INFER, batch_size=10)
    assert skipped_session_count(screener, 1, ("_infer",)) == 1
    assert skipped_session_count(screener, 11, ("_infer",)) == 2


def test_nothing_skipped_counts_as_zero():
    for mode in (ScreenerMode.ASK, ScreenerMode.INFER):
        assert skipped_session_count(_config(mode), 0, ("q1",)) == 0
