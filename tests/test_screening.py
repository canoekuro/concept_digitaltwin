"""スクリーニングの判定と前提ブロック（`persona_sim.panel.screening`）。

Spark を使わない部分だけを見る。方式ごとの振る舞いの違いが要点。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.schema import PersonaCardConfig, PromptHeadings, ScreeningConfig
from persona_sim.panel.screening import (
    INFER_QUESTION_ID,
    incidence_from_panels,
    infer_premise,
    judge,
    screening_fingerprint,
    skipped_session_count,
    unreachable_cells,
)
from persona_sim.run.prompt import persona_card
from tests.conftest import base_survey_dict

#: 対象者条件。自然言語のまま判定プロンプトと前提ブロックへ渡る。
CONDITIONS = ["ビールを月1回以上飲む", "自分で酒類を購入する"]


def _survey(**screener_overrides):
    """スクリーナーを持つ調査定義。"""
    data = base_survey_dict()
    screener: dict = {"conditions": CONDITIONS}
    screener.update(screener_overrides)
    data["screening"] = screener
    return survey_from_dict(data)


def _screener(**overrides):
    return _survey(**overrides).screening


# --------------------------------------------------------------------------- #
# 通過判定
# --------------------------------------------------------------------------- #


def test_judge_separates_passed_and_failed():
    """判定は候補1人につき1回。予約IDの下に通過（`[1]`）／非通過（`[]`）が入る。"""
    verdict = judge(
        {"u1": {INFER_QUESTION_ID: [1]}, "u2": {INFER_QUESTION_ID: []}},
    )
    assert verdict.passed == {"u1"}
    assert verdict.failed == {"u2"}
    assert verdict.tested == {"u1", "u2"}


def test_a_persona_without_a_verdict_does_not_pass():
    """判定の呼び出しが失敗した候補を、黙って通過させない。"""
    verdict = judge({"u1": {}})
    assert verdict.passed == set()
    assert verdict.tested == {"u1"}


# --------------------------------------------------------------------------- #
# 前提ブロック（§6.1）
# --------------------------------------------------------------------------- #


def test_premise_uses_the_survey_definition_wording_verbatim():
    """条件文を言い換えない。言い換えると判定に使った文と食い違う。"""
    premise = infer_premise(_screener())
    assert premise is not None
    assert premise.heading == PromptHeadings().infer_premise
    assert premise.lines == tuple(CONDITIONS)


def test_the_premise_heading_marks_where_the_condition_came_from():
    """本人が答えたのでも全員に一律で与えたのでもない、という由来を見出しに残す。"""
    headings = PromptHeadings()
    assert infer_premise(_screener()).heading == headings.infer_premise
    assert headings.infer_premise != headings.ask_premise


def test_premise_block_is_appended_to_persona_card():
    persona = {"sex": "男", "age": 34, "prefecture": "東京都", "persona": "会社員。"}
    card = persona_card(persona, PersonaCardConfig(), infer_premise(_screener()))

    assert f"【{PromptHeadings().infer_premise}】" in card
    assert "- ビールを月1回以上飲む" in card
    # プロフィールの後ろに来ること
    assert card.index("会社員。") < card.index(PromptHeadings().infer_premise)


def test_persona_card_has_no_premise_block_without_screener():
    persona = {"sex": "男", "age": 34, "persona": "会社員。"}
    card = persona_card(persona, PersonaCardConfig())
    assert PromptHeadings().infer_premise not in card


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


# --------------------------------------------------------------------------- #
# 判定設定の指紋（再利用の可否）
# --------------------------------------------------------------------------- #


def _infer_survey(**overrides):
    return _survey(**overrides)


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


def _config(**overrides) -> ScreeningConfig:
    base: dict = {"conditions": ("週1回以上飲む",)}
    base.update(overrides)
    return ScreeningConfig(**base)


def test_skipped_sessions_are_counted_in_batches():
    """1バッチ＝1呼び出し。人数のままだと `sessions_total` と単位が違う。

    これが揃っていないと、両方を「バッチ」というラベルで並べたときに
    「1バッチ中 380 スキップ」のように内訳が総数を超えて見える。
    """
    # 判定済み120人 = 12バッチぶん。120 と数えてはいけない。
    assert skipped_session_count(_config(batch_size=10), 120) == 12


def test_a_partial_batch_rounds_up():
    screener = _config(batch_size=10)
    assert skipped_session_count(screener, 1) == 1
    assert skipped_session_count(screener, 11) == 2


def test_nothing_skipped_counts_as_zero():
    assert skipped_session_count(_config(), 0) == 0
