"""静的検証（`persona_sim.panel.validate`）。

提示設計3軸の整合（E6）と、`AGENTS.md` の不変条件が機械的に守られることを確認する。
"""

from __future__ import annotations

import pytest

from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.validate import validate_static
from tests.conftest import base_survey_dict, with_remember


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_base_survey_passes(survey_dict):
    report = validate_static(survey_from_dict(survey_dict))
    assert report.ok, report.errors
    assert report.estimate is not None


# --------------------------------------------------------------------------- #
# E6: 提示設計3軸の整合（§5）
# --------------------------------------------------------------------------- #


def test_simultaneous_requires_same():
    data = base_survey_dict()
    data["design"]["presentation"] = "simultaneous"
    data["design"]["sample_overlap"] = "disjoint"
    report = validate_static(survey_from_dict(data))
    assert "E6" in _codes(report.errors)


def test_same_rejects_stimuli_per_persona():
    data = base_survey_dict()
    data["design"]["stimuli_per_persona"] = 2
    report = validate_static(survey_from_dict(data))
    assert "E6" in _codes(report.errors)


def test_disjoint_rejects_multiple_stimuli():
    data = base_survey_dict()
    data["design"]["sample_overlap"] = "disjoint"
    data["design"]["stimuli_per_persona"] = 2
    report = validate_static(survey_from_dict(data))
    assert "E6" in _codes(report.errors)


def test_disjoint_allows_explicit_one():
    data = base_survey_dict(slots=1)
    data["design"]["sample_overlap"] = "disjoint"
    data["design"]["stimuli_per_persona"] = 1
    assert validate_static(survey_from_dict(data)).ok


def test_allow_overlap_requires_stimuli_per_persona():
    data = base_survey_dict()
    data["design"]["sample_overlap"] = "allow_overlap"
    report = validate_static(survey_from_dict(data))
    assert "E6" in _codes(report.errors)


@pytest.mark.parametrize("value", [1, 3, 4])
def test_allow_overlap_rejects_out_of_range(value):
    """コンセプト3件のとき m は 2 のみが有効（1 は disjoint、3 は same）。"""
    data = base_survey_dict()
    data["design"]["sample_overlap"] = "allow_overlap"
    data["design"]["stimuli_per_persona"] = value
    report = validate_static(survey_from_dict(data))
    assert "E6" in _codes(report.errors)


def test_allow_overlap_accepts_middle_value():
    data = base_survey_dict(slots=2)
    data["design"]["sample_overlap"] = "allow_overlap"
    data["design"]["stimuli_per_persona"] = 2
    assert validate_static(survey_from_dict(data)).ok


def test_balanced_rotation_without_memory_warns():
    """記憶が無ければ順序効果は起きないのでラテン方格は無意味（停止はしない）。"""
    data = base_survey_dict()
    data["design"]["rotation"] = "balanced"
    report = validate_static(survey_from_dict(data))
    assert report.ok
    assert "W_ROTATION" in _codes(report.warnings)


def test_unused_named_system_prompt_warns():
    """書いたのにどの設問も指していない [system] は、設問側の書き忘れが多い（§6.1）。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"systems": {"novelty": "違いで判断せよ。"}}
    report = validate_static(survey_from_dict(data))
    assert report.ok
    assert "W_UNUSED_SYSTEM_PROMPT" in _codes(report.warnings)


def test_a_referenced_system_prompt_does_not_warn():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"systems": {"novelty": "違いで判断せよ。"}}
    for question in data["questions"]:
        question["system"] = "novelty"
    assert not validate_static(survey_from_dict(data)).warnings


def test_balanced_rotation_with_memory_does_not_warn():
    """記憶を持つ設問があれば順序効果が起きうるので、ラテン方格は意味を持つ。"""
    data = with_remember(base_survey_dict(), "full_session")
    data["design"]["rotation"] = "balanced"
    report = validate_static(survey_from_dict(data))
    assert not report.warnings


# --------------------------------------------------------------------------- #
# 不変条件（AGENTS.md）
# --------------------------------------------------------------------------- #


def test_thinking_must_be_off():
    data = base_survey_dict()
    data["main_survey"]["model"]["thinking"] = True
    report = validate_static(survey_from_dict(data))
    assert "INVARIANT" in _codes(report.errors)


def test_ordinal_options_must_not_be_shuffled():
    """top_box を持つ設問は順序尺度とみなす。"""
    data = base_survey_dict()
    data["questions"][0]["randomize_options"] = True
    report = validate_static(survey_from_dict(data))
    assert "INVARIANT" in _codes(report.errors)


def test_non_ordinal_options_may_be_shuffled():
    data = base_survey_dict()
    data["questions"][0] = {
        "id": "q_brand",
        "text": "知っているブランドは。",
        "type": "multi",
        "options": ["A社", "B社", "C社"],
        "randomize_options": True,
    }
    assert validate_static(survey_from_dict(data)).ok


def test_scale_type_must_not_be_shuffled():
    data = base_survey_dict()
    data["questions"][0] = {
        "id": "q_scale",
        "text": "満足度は。",
        "type": "scale",
        "options": ["1", "2", "3", "4", "5"],
        "randomize_options": True,
    }
    report = validate_static(survey_from_dict(data))
    assert "INVARIANT" in _codes(report.errors)


# --------------------------------------------------------------------------- #
# 調査定義の整合
# --------------------------------------------------------------------------- #


def test_quota_total_must_match_panel_size():
    data = base_survey_dict()
    data["panel"]["size"] = 5
    report = validate_static(survey_from_dict(data))
    assert any("一致しない" in issue.message for issue in report.errors)


def test_proportion_must_sum_to_one():
    data = base_survey_dict()
    data["panel"]["quotas"]["mode"] = "proportion"
    for cell in data["panel"]["quotas"]["cells"]:
        del cell["n"]
        cell["proportion"] = 0.3
    report = validate_static(survey_from_dict(data))
    assert any("1.0" in issue.message for issue in report.errors)


def test_duplicate_stimulus_id_is_rejected():
    data = base_survey_dict()
    data["stimuli"][1]["id"] = "c1"
    report = validate_static(survey_from_dict(data))
    assert any("重複" in issue.message for issue in report.errors)


def test_top_box_out_of_range_is_rejected():
    data = base_survey_dict()
    data["questions"][0]["top_box"] = [1, 9]
    report = validate_static(survey_from_dict(data))
    assert any("top_box" in issue.message for issue in report.errors)


# --------------------------------------------------------------------------- #
# スクリーナー（§4.2）
# --------------------------------------------------------------------------- #


def _screener(**overrides):
    """方式に合った形のスクリーナー定義（§4.2）。

    `ask` は選択肢を見せて答えさせるので `questions`、`assume` / `infer` は
    選択肢を提示しないので自然言語の `conditions`。
    """
    mode = overrides.get("mode", "ask")
    screener: dict = {"oversample_factor": 4}
    if mode == "ask":
        screener["questions"] = [
            {
                "id": "sc1",
                "text": "飲用頻度は。",
                "type": "single",
                "options": ["週2回以上", "それ以下"],
                "pass_if": [1],
            }
        ]
    else:
        screener["conditions"] = ["週2回以上飲む"]
    screener.update(overrides)
    return screener


def test_ask_screener_passes():
    data = base_survey_dict()
    data["screening"] = _screener()
    assert validate_static(survey_from_dict(data)).ok


def test_pass_if_out_of_range_is_rejected():
    data = base_survey_dict()
    data["screening"] = _screener()
    data["screening"]["questions"][0]["pass_if"] = [1, 5]
    report = validate_static(survey_from_dict(data))
    assert any("pass_if" in issue.message for issue in report.errors)


def test_open_screener_question_is_rejected():
    """自由回答では通過を機械判定できない。"""
    data = base_survey_dict()
    data["screening"] = _screener()
    data["screening"]["questions"][0]["type"] = "open"
    report = validate_static(survey_from_dict(data))
    assert any("single / multi" in issue.message for issue in report.errors)


def test_empty_pass_if_is_rejected():
    data = base_survey_dict()
    data["screening"] = _screener()
    data["screening"]["questions"][0]["pass_if"] = []
    report = validate_static(survey_from_dict(data))
    assert any("pass_if" in issue.message for issue in report.errors)


def test_assume_requires_conditions():
    """条件の文言が無ければ、何もペルソナに付与できない。"""
    from persona_sim.errors import SurveyDefinitionError

    data = base_survey_dict()
    data["screening"] = _screener(mode="assume", conditions=[])
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "screening.conditions" in str(excinfo.value)


def test_assume_rejects_blank_conditions():
    data = base_survey_dict()
    data["screening"] = _screener(mode="assume", conditions=["   "])
    report = validate_static(survey_from_dict(data))
    assert any("conditions[0]" in issue.message for issue in report.errors)


def test_assume_with_conditions_passes():
    data = base_survey_dict()
    data["screening"] = _screener(mode="assume")
    del data["screening"]["oversample_factor"]
    assert validate_static(survey_from_dict(data)).ok


def test_assume_warns_about_unused_oversample_factor():
    """効かない指定を黙って無視しない。"""
    data = base_survey_dict()
    data["screening"] = _screener(mode="assume")
    report = validate_static(survey_from_dict(data))
    assert report.ok
    assert "W_SCREENER_OVERSAMPLE" in _codes(report.warnings)


def test_estimate_shows_both_screening_methods():
    """費用とのバーターで方式を選ぶので、選ばなかった側も見えている必要がある。"""
    data = base_survey_dict()
    data["screening"] = _screener(mode="assume")
    estimate = validate_static(survey_from_dict(data)).estimate

    assert estimate is not None
    assert estimate.screener_sessions == 0
    assert estimate.screener_sessions_if_ask == 4 * 4 * 1  # size × factor × 条件数
    assert any("ask なら" in line for line in estimate.lines())


def test_estimate_counts_screener_sessions_for_ask():
    data = base_survey_dict()
    data["screening"] = _screener()
    estimate = validate_static(survey_from_dict(data)).estimate
    assert estimate is not None
    assert estimate.screener_sessions == 16


# --------------------------------------------------------------------------- #
# 見積もり（§10.1）
# --------------------------------------------------------------------------- #


def test_estimate_for_same_multiplies_sessions_by_stimuli():
    """既定の same ではセッション数がコンセプト数倍になる。"""
    report = validate_static(survey_from_dict(base_survey_dict()))
    estimate = report.estimate
    assert estimate is not None
    assert estimate.stimuli_per_persona == 3
    assert estimate.sessions == 4 * 3 * 2
    assert estimate.effective_n_per_stimulus == 4


def test_estimate_for_disjoint_divides_effective_n():
    data = base_survey_dict(slots=1)
    data["design"]["sample_overlap"] = "disjoint"
    estimate = validate_static(survey_from_dict(data)).estimate
    assert estimate is not None
    assert estimate.stimuli_per_persona == 1
    assert estimate.sessions == 4 * 1 * 2
    assert estimate.effective_n_per_stimulus == 4 // 3


def test_answers_equal_sessions_without_memory():
    """記憶を持たない調査では1セッション＝1回答になる。"""
    estimate = validate_static(survey_from_dict(base_survey_dict())).estimate
    assert estimate is not None
    assert estimate.answers == 4 * 3 * 2
    assert estimate.answers == estimate.sessions


def test_answers_exceed_sessions_with_memory():
    """記憶を持つ設問があると、セッション数は回答件数より少なくなる。

    回答1件あたりで取った実績値をセッション数に掛けると過小に出るので、
    見積もりの換算はこの2つを取り違えてはいけない（`persona_sim.uiconfig.cost`）。
    """
    estimate = validate_static(
        survey_from_dict(with_remember(base_survey_dict(), "full_session"))
    ).estimate
    assert estimate is not None
    assert estimate.answers == 4 * 3 * 2
    # 全設問が1つにまとまるので、1人あたり1セッション。
    assert estimate.sessions == 4
    assert estimate.answers > estimate.sessions


# --------------------------------------------------------------------------- #
# 出力設定（§7）
# --------------------------------------------------------------------------- #


def test_unknown_segment_axis_stops_validation():
    """綴り違いの軸を黙って捨てると、集計後まで気づけない。"""
    data = base_survey_dict()
    data["output"]["segments"] = ["total", "age_bands_10"]
    report = validate_static(survey_from_dict(data))

    assert not report.ok
    assert any("age_bands_10" in str(issue) for issue in report.errors)


def test_composite_segment_axis_is_accepted():
    data = base_survey_dict()
    data["output"]["segments"] = ["total", "sex_x_age_band_10", "cell_id"]
    assert validate_static(survey_from_dict(data)).ok


def test_unknown_output_format_stops_validation():
    data = base_survey_dict()
    data["output"]["formats"] = ["csv", "pdf"]
    report = validate_static(survey_from_dict(data))

    assert not report.ok
    assert any("pdf" in str(issue) for issue in report.errors)


def test_sample_survey_output_settings_pass():
    data = base_survey_dict()
    data["output"] = {
        "segments": ["total", "sex", "age_band_10", "sex_x_age_band_10", "cell_id"],
        "formats": ["csv", "xlsx"],
    }
    assert validate_static(survey_from_dict(data)).ok


# --------------------------------------------------------------------------- #
# prompt の上書き（§6.1）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["system", "headings", "rules"])
def test_prompt_override_validates_ok(key):
    """版管理を廃止したので、上書きしても警告なくそのまま通る。"""
    overrides = {
        "system": "あなたは架空の人物です。",
        "headings": {"question": "●質問"},
        "rules": {"single": "番号で。"},
    }
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {key: overrides[key]}
    report = validate_static(survey_from_dict(data))

    assert report.ok, report.errors


def test_persona_card_override_validates_ok():
    """ペルソナカードは prompt から独立して上書きできる（§6.1）。"""
    data = base_survey_dict()
    data["main_survey"]["persona_card"] = {
        "attributes": [{"field": "sex"}, {"field": "age", "suffix": "歳"}],
        "include_summary": False,
        "persona_fields": [{"field": "cultural_background", "label": "生活背景"}],
    }
    report = validate_static(survey_from_dict(data))

    assert report.ok, report.errors


# --------------------------------------------------------------------------- #
# image_uri は Unity Catalog Volumes のパス（§3）
# --------------------------------------------------------------------------- #


def test_volumes_image_uri_does_not_warn():
    data = base_survey_dict()
    data["stimuli"][0]["image_uri"] = "/Volumes/main/persona_lab/stimuli/c1.png"
    report = validate_static(survey_from_dict(data))

    assert report.ok, report.errors
    assert "W_IMAGE_URI" not in _codes(report.warnings)


@pytest.mark.parametrize(
    "uri", ["https://example.invalid/c1.png", "abfss://container@account/c1.png", "c1.png"]
)
def test_non_volumes_image_uri_warns(uri):
    data = base_survey_dict()
    data["stimuli"][0]["image_uri"] = uri
    report = validate_static(survey_from_dict(data))

    assert report.ok, report.errors  # 警告であって停止はしない
    assert "W_IMAGE_URI" in _codes(report.warnings)


@pytest.mark.parametrize("uri", ["dbfs:/mnt/stimuli/c1.png", "/dbfs/mnt/stimuli/c1.png"])
def test_dbfs_image_uri_warns_as_legacy(uri):
    """旧形式と分かるように別の警告にする。"""
    data = base_survey_dict()
    data["stimuli"][0]["image_uri"] = uri
    codes = _codes(validate_static(survey_from_dict(data)).warnings)

    assert "W_IMAGE_URI_LEGACY" in codes
    assert "W_IMAGE_URI" not in codes


def test_no_image_uri_does_not_warn(survey_dict):
    codes = _codes(validate_static(survey_from_dict(survey_dict)).warnings)
    assert "W_IMAGE_URI" not in codes
    assert "W_IMAGE_URI_LEGACY" not in codes


def test_native_image_mode_with_uri_passes():
    data = base_survey_dict()
    data["stimuli"][0]["image_mode"] = "native"
    data["stimuli"][0]["image_uri"] = "/Volumes/catalog/schema/volume/img.png"
    report = validate_static(survey_from_dict(data))
    assert report.ok, report.errors


def test_native_image_mode_without_uri_fails():
    data = base_survey_dict()
    data["stimuli"][0]["image_mode"] = "native"
    data["stimuli"][0]["image_uri"] = None
    report = validate_static(survey_from_dict(data))
    assert not report.ok
    assert any(
        "image_mode が native ですが image_uri が指定されていません" in issue.message
        for issue in report.errors
    )


# --------------------------------------------------------------------------- #
# 理由を書かせる設問（`questions[].reasoning`、§6.3）
# --------------------------------------------------------------------------- #


def _with_reasoning(*, structured_output: str = "always", question_index: int = 0) -> dict:
    data = base_survey_dict()
    data["questions"][question_index]["reasoning"] = True
    data["main_survey"]["model"]["structured_output"] = structured_output
    return data


def test_reasoning_passes_with_structured_output_always():
    report = validate_static(survey_from_dict(_with_reasoning()))
    assert report.ok, report.errors


@pytest.mark.parametrize("mode", ["auto", "never"])
def test_reasoning_requires_structured_output_always(mode):
    """正規表現パースへ落ちると、理由の文中の数字を回答番号として拾う。"""
    report = validate_static(survey_from_dict(_with_reasoning(structured_output=mode)))
    assert "SURVEY" in _codes(report.errors)
    assert any("structured_output" in issue.message for issue in report.errors)


def test_reasoning_cannot_be_set_on_open_questions():
    """自由回答は本文そのものが回答なので、理由を分ける意味が無い。"""
    data = _with_reasoning(question_index=1)  # q_reason_1 は type: open
    report = validate_static(survey_from_dict(data))
    assert any("reasoning は書けない" in issue.message for issue in report.errors)


def test_reasoning_max_length_must_be_positive():
    data = _with_reasoning()
    data["questions"][0]["reasoning_max_length"] = 0
    report = validate_static(survey_from_dict(data))
    assert any("reasoning_max_length" in issue.message for issue in report.errors)


def test_reasoning_warns_about_convergence():
    """使ったこと自体を警告に残す（§13）。ばらつきが縮む懸念は思考モードと同じ。"""
    report = validate_static(survey_from_dict(_with_reasoning()))
    assert "W_REASONING" in _codes(report.warnings)


def test_no_reasoning_warning_without_reasoning_questions(survey_dict):
    report = validate_static(survey_from_dict(survey_dict))
    assert "W_REASONING" not in _codes(report.warnings)


# --------------------------------------------------------------------------- #
# 字数と出力予算の整合（§6.3）
#
# 字数（`max_length` / `reasoning_max_length`）はモデルへの指示、`max_tokens_*` は
# エンドポイント側の打ち切りで、単位も役割も違う。片方から他方を導出しないかわりに、
# 明らかに足りない組み合わせを警告で拾う。
# --------------------------------------------------------------------------- #


def test_a_short_reasoning_budget_warns():
    data = _with_reasoning()
    data["questions"][0]["reasoning_max_length"] = 400
    data["main_survey"]["model"]["max_tokens_reasoning"] = 128

    report = validate_static(survey_from_dict(data))

    assert "W_OUTPUT_BUDGET" in _codes(report.warnings)
    assert report.ok, report.errors  # 止めない。推定値で調査を拒まない


def test_a_sufficient_reasoning_budget_does_not_warn():
    data = _with_reasoning()
    data["questions"][0]["reasoning_max_length"] = 80
    data["main_survey"]["model"]["max_tokens_reasoning"] = 512

    report = validate_static(survey_from_dict(data))

    assert "W_OUTPUT_BUDGET" not in _codes(report.warnings)


def test_a_short_open_budget_warns():
    """同じ穴が `open` 側にもあったので、こちらも見る。"""
    data = base_survey_dict()
    data["questions"][1]["max_length"] = 600  # q_reason_1 は type: open
    data["main_survey"]["model"]["max_tokens_open"] = 256

    report = validate_static(survey_from_dict(data))

    assert "W_OUTPUT_BUDGET" in _codes(report.warnings)


def test_open_questions_without_max_length_are_not_checked():
    """字数を指示していない設問は、予算と突き合わせる相手がいない。"""
    data = base_survey_dict()
    for question in data["questions"]:
        question.pop("max_length", None)
    data["main_survey"]["model"]["max_tokens_open"] = 8

    report = validate_static(survey_from_dict(data))

    assert "W_OUTPUT_BUDGET" not in _codes(report.warnings)


def test_reasoning_questions_are_checked_against_the_reasoning_budget_only():
    """reasoning 設問の予算は `max_tokens_open` ではない（設問タイプは single）。"""
    data = _with_reasoning()
    data["questions"][0]["reasoning_max_length"] = 300
    data["main_survey"]["model"]["max_tokens_reasoning"] = 4096
    data["main_survey"]["model"]["max_tokens_open"] = 8

    report = validate_static(survey_from_dict(data))

    warnings = [issue for issue in report.warnings if issue.code == "W_OUTPUT_BUDGET"]
    assert all("max_tokens_reasoning" not in issue.message for issue in warnings)
