"""調査定義の読み込み（`persona_sim.panel.loader`）。

未知のキーを黙って捨てないこと、廃止フィールドが理由つきで止まることを重視する。
指定したつもりが効いていない状態が最も危険なため。
"""

from __future__ import annotations

import pytest

from persona_sim.errors import SurveyDefinitionError
from persona_sim.panel.loader import (
    RECORD_MODEL,
    load_survey,
    survey_from_dict,
    survey_from_record,
)
from persona_sim.panel.schema import (
    Design,
    PersonaCardConfig,
    Presentation,
    PromptConfig,
    Rotation,
    SampleOverlap,
    ScreenerLogic,
)
from tests.conftest import base_survey_dict


def test_base_survey_loads(survey_dict):
    survey = survey_from_dict(survey_dict)
    assert survey.survey_id == "survey_test"
    assert survey.stimuli_count == 3
    # 設問はコンセプトごとに繰り返すテンプレートではなく slot ごとに展開されるので、
    # 3コンセプト × 2問 = 6件。measure が 2 種類に畳む。
    assert len(survey.questions) == 6
    assert {q.measure_key for q in survey.questions} == {"q_intent", "q_reason"}
    assert survey.panel.quotas.cells[0].conditions.sex == "男"


def test_design_defaults_are_counterfactual_monadic():
    """design を省略したら same × sequential（§5.3）。記憶は設問側の既定（＝持たない）。"""
    data = base_survey_dict()
    del data["design"]
    survey = survey_from_dict(data)
    assert survey.design.sample_overlap is SampleOverlap.SAME
    assert survey.design.presentation is Presentation.SEQUENTIAL
    assert survey.design.rotation is Rotation.NONE
    assert all(not q.remember.retains for q in survey.questions)


@pytest.mark.parametrize(
    ("removed_field", "value", "expected_hint"),
    [
        ("type", "sequential_monadic", "sample_overlap"),
        ("balance_within_cell", True, "sample_overlap"),
        # 記憶は設問側へ移したので、案内が指す先も design ではなく questions になる。
        ("memory", "full_session", "questions[].remember"),
    ],
)
def test_removed_design_fields_are_rejected_with_migration_hint(
    removed_field, value, expected_hint
):
    """廃止した design のフィールドは黙って無視せず、書き換え先を添えて止める（§5.4）。"""
    data = base_survey_dict()
    data["design"][removed_field] = value
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert removed_field in message
    assert expected_hint in message  # 移行先を示していること


def test_removed_model_temperature_is_rejected_with_migration_hint():
    """廃止した model.temperature は黙って無視しない（§6.3）。

    無視できてしまうと「0 を指定したつもり」が残り、実際には指定していないことに
    気づけない。
    """
    data = base_survey_dict()
    data["main_survey"]["model"]["temperature"] = 0.0
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "model.temperature" in message
    assert "廃止" in message  # 綴り違いではなく廃止だと伝えていること


def test_removed_infer_model_temperature_is_rejected_with_migration_hint():
    """判定モデル（`screening.model`）側にも同じ廃止キーを書けてはならない。"""
    data = base_survey_dict()
    data["screening"] = {
        "mode": "infer",
        "oversample_factor": 2,
        "conditions": ["週に1回以上コーヒーを飲む"],
        "model": {"temperature": 0.4},
    }
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "temperature" in message
    assert "廃止" in message


def test_old_job_key_is_rejected_with_migration_hint():
    """旧 job: を黙って読まない。読めてしまうと用語が二重に残る。"""
    data = base_survey_dict()
    data["job"] = data.pop("survey")
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "survey:" in message
    assert "survey_id" in message  # 列名も変わったことを伝えていること


# --------------------------------------------------------------------------- #
# screening: / main_survey: への再編成（issue 202607301208 項目5）
#
# 旧構造を黙って無視すると「設定したのに効いていない」ことに実行後まで気づけない。
# 移行先を必ず添えて停止すること。移行の各パターンに1件ずつ対応させている。
# --------------------------------------------------------------------------- #


def test_old_top_level_model_is_rejected_with_migration_hint():
    data = base_survey_dict()
    data["model"] = data["main_survey"].pop("model")
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "main_survey.model" in str(excinfo.value)


def test_old_top_level_prompt_is_rejected_with_migration_hint():
    data = base_survey_dict()
    data["prompt"] = {"system": "あなたは架空の人物です。"}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "main_survey.prompt" in message
    # 中身が3つに分かれることまで伝える（persona_fields と infer_system の行き先）。
    assert "main_survey.persona_card.persona_fields" in message
    assert "screening.prompt.system" in message


def test_old_panel_screener_is_rejected_with_migration_hint():
    data = base_survey_dict()
    data["panel"]["screener"] = {"mode": "assume", "conditions": ["コーヒーを飲む"]}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "screening:" in message
    assert "screening.batch_size" in message  # infer.* の行き先も伝えていること


def test_old_prompt_persona_fields_is_rejected_with_migration_hint():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {
        "persona_fields": [{"field": "cultural_background", "label": "生活背景"}]
    }
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "main_survey.persona_card.persona_fields" in str(excinfo.value)


def test_old_prompt_infer_system_is_rejected_with_migration_hint():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"infer_system": "あなたは選定担当です。"}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "screening.prompt.system" in str(excinfo.value)


def test_old_prompt_rules_infer_is_rejected_with_migration_hint():
    """`rules.infer` は設問タイプではないのでスクリーニング側へ移した。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"infer": "番号を挙げてください。"}}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "screening.prompt.rule" in str(excinfo.value)


@pytest.mark.parametrize("where", ["main_survey", "screening"])
def test_removed_include_attributes_is_rejected_with_migration_hint(where):
    """真偽値は廃止し attributes: のリストに寄せた。本調査・判定の両方で。"""
    data = base_survey_dict()
    if where == "main_survey":
        data["main_survey"]["persona_card"] = {"include_attributes": False}
    else:
        data["screening"] = {
            "mode": "infer",
            "conditions": ["コーヒーを飲む"],
            "persona_card": {"include_attributes": False},
        }
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "attributes" in message
    assert "廃止" in message


def test_new_structure_round_trips():
    """新構造がそのまま読めること（本調査・スクリーニングの両ブロック）。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"system": "なりきってください。"}
    data["main_survey"]["persona_card"] = {"attributes": [{"field": "sex"}]}
    data["screening"] = {
        "model": {"deployment": "judge", "max_tokens": 256},
        "prompt": {"system": "選定してください。", "rule": "番号を挙げてください。"},
        "persona_card": {"attributes": [], "include_summary": True},
        "mode": "infer",
        "conditions": ["コーヒーを週1回以上飲む"],
        "batch_size": 10,
    }
    survey = survey_from_dict(data)

    assert survey.prompt.system == "なりきってください。"
    assert [a.field for a in survey.persona_card.attributes] == ["sex"]
    assert survey.screening.model.deployment == "judge"
    assert survey.screening.prompt.system == "選定してください。"
    assert survey.screening.prompt.rule == "番号を挙げてください。"
    assert survey.screening.persona_card.attributes == ()
    assert survey.screening.batch_size == 10


def test_unknown_key_is_rejected():
    """綴り違いを検出する。"""
    data = base_survey_dict()
    data["design"]["stimulus_per_persona"] = 2  # 正しくは stimuli_per_persona
    with pytest.raises(SurveyDefinitionError, match="stimulus_per_persona"):
        survey_from_dict(data)


@pytest.mark.parametrize("question_type", ["rank", "maxdiff", "conjoint"])
def test_unsupported_question_types_are_rejected(question_type):
    """フェーズ1非対応の設問タイプ（§3.1）。"""
    data = base_survey_dict()
    data["questions"][0]["type"] = question_type
    with pytest.raises(SurveyDefinitionError, match="非対応"):
        survey_from_dict(data)


def test_unknown_enum_value_lists_allowed_values():
    data = base_survey_dict()
    data["design"]["memory"] = "forever"
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "within_stimulus" in str(excinfo.value)


def test_count_mode_requires_n():
    data = base_survey_dict()
    del data["panel"]["quotas"]["cells"][0]["n"]
    with pytest.raises(SurveyDefinitionError, match="n が必須"):
        survey_from_dict(data)


def test_proportion_mode_rejects_n():
    data = base_survey_dict()
    data["panel"]["quotas"]["mode"] = "proportion"
    with pytest.raises(SurveyDefinitionError, match="proportion が必須"):
        survey_from_dict(data)


def test_bool_is_not_accepted_as_int():
    data = base_survey_dict()
    data["panel"]["size"] = True
    with pytest.raises(SurveyDefinitionError, match="整数"):
        survey_from_dict(data)


def test_load_survey_reads_yaml(tmp_path):
    import yaml

    path = tmp_path / "survey.yaml"
    path.write_text(yaml.safe_dump(base_survey_dict(), allow_unicode=True), encoding="utf-8")
    assert load_survey(path).survey_id == "survey_test"


def test_sample_yaml_is_valid():
    """リポジトリ同梱のサンプルが常に読めて、**検証も通る**こと。

    読めるだけを見ていたので、割り付けの合計が `panel.size` と一致しない状態が
    ずっと残っていた。書き写して使う人が最初に踏むので、`validate_static` まで通す。
    """
    from pathlib import Path

    from persona_sim.panel.validate import validate_static

    sample = Path(__file__).resolve().parents[1] / "examples" / "survey_sample.yaml"
    survey = load_survey(sample)
    assert survey.stimuli_count == 3
    assert survey.design.sample_overlap is SampleOverlap.SAME

    report = validate_static(survey)
    assert report.ok, [str(issue) for issue in report.errors]


def test_smoke_sample_yaml_is_valid():
    """run_survey.ipynb の単体動作確認用サンプル（Volumes 配置想定）が常に読めること。"""
    from pathlib import Path

    from persona_sim.panel.validate import validate_static

    sample = Path(__file__).resolve().parents[1] / "examples" / "survey_sample_smoke.yaml"
    survey = load_survey(sample)
    report = validate_static(survey)
    assert report.ok, [str(issue) for issue in report.errors]
    assert survey.model.endpoint == "fake"


# --------------------------------------------------------------------------- #
# prompt の上書き（§6.1）
# --------------------------------------------------------------------------- #


def test_prompt_overrides_are_loaded():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {
        "system": "あなたは架空の人物です。",
        "headings": {"question": "●質問"},
        "rules": {"single": "1つだけ番号で答えよ。"},
    }
    prompt = survey_from_dict(data).prompt

    assert prompt.system == "あなたは架空の人物です。"
    assert prompt.headings.question == "●質問"
    assert prompt.rules.single == "1つだけ番号で答えよ。"


def test_removed_prompt_template_version_is_rejected_with_migration_hint():
    """廃止した prompt.template_version は黙って無視しない（§6.1）。

    無視できてしまうと「版管理をしているつもり」が残り、実際には管理していないことに
    気づけない。
    """
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"template_version": "v2"}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "prompt.template_version" in message
    assert "廃止" in message


def test_prompt_overrides_keep_defaults_for_omitted_keys():
    """一部だけ上書きしても、書かなかったキーは既定のまま残る。"""
    from persona_sim.panel.schema import PromptHeadings, PromptRules

    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"headings": {"question": "●質問"}, "rules": {"single": "番号で。"}}
    prompt = survey_from_dict(data).prompt

    assert prompt.headings.profile == PromptHeadings().profile
    assert prompt.headings.stimulus == PromptHeadings().stimulus
    assert prompt.rules.multi == PromptRules().multi
    assert prompt.system  # 既定の [system] が入っていること


def test_named_system_prompts_are_loaded_and_bound_to_questions():
    """設問ごとに使い分ける [system]（§6.1）。設問側は名前で指す。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {
        "systems": {"purchase_intent": "買うかで判断せよ。", "novelty": "違いで判断せよ。"}
    }
    data["questions"][0]["system"] = "purchase_intent"
    survey = survey_from_dict(data)

    assert survey.prompt.systems["novelty"] == "違いで判断せよ。"
    assert survey.questions[0].system == "purchase_intent"
    assert survey.prompt.system_for(survey.questions[0].system) == "買うかで判断せよ。"
    # 指していない設問は既定のまま。
    assert survey.questions[1].system is None
    assert survey.prompt.system_for(None) == survey.prompt.system


def test_question_pointing_at_an_undefined_system_prompt_is_rejected():
    """綴り違いを既定へ黙って落とさない。書き分けたつもりが既定で聞かれてしまう。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"systems": {"novelty": "違いで判断せよ。"}}
    data["questions"][0]["system"] = "novelity"
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "novelity" in message
    assert "novelty" in message  # 使える名前を添える


def test_empty_system_prompt_body_is_rejected():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"systems": {"novelty": ""}}
    with pytest.raises(SurveyDefinitionError, match="systems.novelty"):
        survey_from_dict(data)


def test_unknown_prompt_key_is_rejected():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"systen": "誤字"}
    with pytest.raises(SurveyDefinitionError, match="systen"):
        survey_from_dict(data)


def test_unknown_rule_key_is_rejected():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"sngle": "誤字"}}
    with pytest.raises(SurveyDefinitionError, match="sngle"):
        survey_from_dict(data)


def test_open_rule_accepts_max_length_placeholder():
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"open": "{max_length}字まで。"}}
    assert survey_from_dict(data).prompt.rules.open == "{max_length}字まで。"


@pytest.mark.parametrize(
    ("key", "template"),
    [("open", "{max_len}字まで。"), ("single", "{max_length}番号で。"), ("numeric", "{}で。")],
)
def test_unresolvable_placeholder_in_rules_is_rejected(key, template):
    """実行時まで気づけない差し込みは読み込みで止める。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {key: template}}
    with pytest.raises(SurveyDefinitionError, match=f"rules.{key}"):
        survey_from_dict(data)


# --------------------------------------------------------------------------- #
# スクリーナー: 方式ごとの書き方（§4.2）
# --------------------------------------------------------------------------- #


def _ask_question() -> dict:
    return {
        "id": "sc1",
        "text": "週に1回以上コーヒーを飲みますか。",
        "type": "single",
        "options": ["はい", "いいえ"],
        "pass_if": [1],
    }


@pytest.mark.parametrize("mode", ["assume", "infer"])
def test_questions_are_rejected_for_modes_that_show_no_options(mode):
    """選択肢を提示しない方式に設問を書かせない。

    黙って読めると「条件を書いたのに効いていない」ことに実行後まで気づけない。
    """
    data = base_survey_dict()
    data["screening"] = {"mode": mode, "questions": [_ask_question()]}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "conditions" in message  # 移し先を示していること


def test_conditions_are_rejected_for_ask():
    data = base_survey_dict()
    data["screening"] = {"mode": "ask", "conditions": ["コーヒーを週1回以上飲む"]}
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    assert "questions" in str(excinfo.value)


def test_logic_is_rejected_for_infer():
    """`infer` では `logic` が効かないので、書けたら止める。

    以前は `logic` から判定プロンプトに一文を差し込んでいたが、その文が
    `prompt.rule` の直前という最も効く位置に入るため、プロンプトを書き換えても
    判定が変わらなかった。文を消した以上 `logic` は何もしないので、
    「設定したつもり」の記録を残させない（`AGENTS.md` の不変条件）。
    """
    data = base_survey_dict()
    data["screening"] = {
        "mode": "infer",
        "conditions": ["コーヒーを週1回以上飲む"],
        "logic": "any",
    }
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "screening.logic" in message
    assert "screening.prompt.rule" in message  # 移し先を示していること


def test_infer_without_logic_is_accepted():
    """既定のまま（キーを書かない）は通す。既定値まで拒むと何も書けなくなる。"""
    data = base_survey_dict()
    data["screening"] = {"mode": "infer", "conditions": ["コーヒーを週1回以上飲む"]}
    assert survey_from_dict(data).screening.logic is ScreenerLogic.ALL


def test_logic_still_works_for_ask():
    """`ask` では通過判定の結合方法として今も効く。"""
    data = base_survey_dict()
    data["screening"] = {"mode": "ask", "logic": "any", "questions": [_ask_question()]}
    assert survey_from_dict(data).screening.logic is ScreenerLogic.ANY


@pytest.mark.parametrize("mode", ["assume", "infer"])
def test_conditions_are_read_as_written(mode):
    data = base_survey_dict()
    data["screening"] = {
        "mode": mode,
        "conditions": ["コーヒーを週1回以上飲む", "自宅で豆から淹れる"],
    }
    screener = survey_from_dict(data).screening
    assert screener.conditions == ("コーヒーを週1回以上飲む", "自宅で豆から淹れる")
    assert screener.questions == ()
    assert screener.condition_texts() == screener.conditions


def test_ask_builds_condition_texts_from_questions():
    """`ask` でも条件文の出口は同じ。前提文が無ければ設問文と通過選択肢から組み立てる。"""
    data = base_survey_dict()
    data["screening"] = {"mode": "ask", "questions": [_ask_question()]}
    screener = survey_from_dict(data).screening
    assert screener.conditions == ()
    assert screener.condition_texts() == ("週に1回以上コーヒーを飲みますか。: はい のいずれか",)


# --------------------------------------------------------------------------- #
# 調査の種類（§3.0）
# --------------------------------------------------------------------------- #


def test_survey_type_defaults_to_concept():
    """既存の調査定義を壊さない。書いていなければコンセプト調査とみなす。"""
    from persona_sim.panel.schema import SurveyType

    data = base_survey_dict()
    assert "type" not in data["survey"]
    assert survey_from_dict(data).survey_type is SurveyType.CONCEPT


def test_survey_type_is_read_as_written():
    from persona_sim.panel.schema import SurveyType

    data = base_survey_dict()
    data["survey"]["type"] = "concept"
    assert survey_from_dict(data).survey_type is SurveyType.CONCEPT


def test_unknown_survey_type_is_rejected_with_the_allowed_values():
    """自由文字列にすると綴り違いがそのまま runs に残り、種別で集計できなくなる。"""
    data = base_survey_dict()
    data["survey"]["type"] = "package"
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "survey.type" in message
    assert "concept" in message  # 指定できる値を示していること


def test_survey_type_is_kept_in_raw_for_the_run_record():
    """`runs.metadata_json` には調査定義の全文が入る（§9）。種別も残ること。"""
    data = base_survey_dict()
    data["survey"]["type"] = "concept"
    assert survey_from_dict(data).raw["survey"]["type"] == "concept"


def test_removed_question_scale_points_is_rejected_with_migration_hint():
    """廃止した questions[].scale_points は黙って無視しない。

    読み込みも検証もしていたのにプロンプトにも集計にも効いておらず、
    「尺度の点数を設定したつもり」だけが残っていた。選択肢の数は options の長さで決まる。
    """
    data = base_survey_dict()
    data["questions"][0]["scale_points"] = 5
    with pytest.raises(SurveyDefinitionError) as excinfo:
        survey_from_dict(data)
    message = str(excinfo.value)
    assert "scale_points" in message
    assert "廃止" in message
    assert "options" in message  # 代わりに何を見ているかを伝えていること


def test_omitted_model_and_screening_defaults_match_the_dataclass_defaults():
    """省略時の既定は ModelConfig / ScreeningConfig の既定そのもの。

    loader がリテラルで既定値を持ち直すと、片方だけ直したときに黙ってずれる
    （実際 `request_timeout_sec` は `llm/databricks.py` と `panel/schema.py` の
    二重定義になっていた）。既定の出どころはデータクラス1つに保つ。

    捉えられるのは**値がずれたとき**で、同じ値の二重定義そのものは検出できない。
    ずれた瞬間に落ちる網を張っておくのが目的。
    """
    from persona_sim.panel.schema import Endpoint, ModelConfig, ScreeningConfig

    data = base_survey_dict()
    data["main_survey"]["model"] = {"endpoint": "fake", "deployment": "test-deployment"}
    data["screening"] = {"mode": "ask", "questions": [_ask_question()]}
    survey = survey_from_dict(data)

    defaults = ModelConfig(endpoint=Endpoint.FAKE, deployment="test-deployment")
    assert survey.model.max_tokens == defaults.max_tokens
    assert survey.model.max_tokens_open == defaults.max_tokens_open
    assert survey.model.max_tokens_reasoning == defaults.max_tokens_reasoning
    assert survey.model.concurrency == defaults.concurrency
    assert survey.model.request_timeout_sec == defaults.request_timeout_sec

    assert survey.screening.oversample_factor == ScreeningConfig().oversample_factor


# --------------------------------------------------------------------------- #
# 理由を書かせる設問（`questions[].reasoning`、§6.3）
# --------------------------------------------------------------------------- #


def test_reasoning_keys_are_read():
    data = base_survey_dict()
    data["questions"][0]["reasoning"] = True
    data["questions"][0]["reasoning_max_length"] = 40
    survey = survey_from_dict(data)

    question = survey.questions[0]
    assert question.reasoning is True
    assert question.reasoning_max_length == 40


def test_reasoning_max_length_defaults_without_being_written():
    from persona_sim.panel.schema import DEFAULT_REASONING_MAX_LENGTH

    data = base_survey_dict()
    data["questions"][0]["reasoning"] = True
    survey = survey_from_dict(data)

    assert survey.questions[0].reasoning_max_length == DEFAULT_REASONING_MAX_LENGTH


def test_questions_do_not_carry_reasoning_by_default(survey_dict):
    survey = survey_from_dict(survey_dict)
    assert all(question.reasoning is False for question in survey.questions)


def test_max_tokens_reasoning_is_read():
    data = base_survey_dict()
    data["main_survey"]["model"]["max_tokens_reasoning"] = 1024
    assert survey_from_dict(data).model.max_tokens_reasoning == 1024


def test_reasoning_rules_are_read_per_question_type():
    from persona_sim.panel.schema import ReasoningRules

    data = base_survey_dict()
    data["main_survey"]["prompt"] = {
        "rules": {"reasoning": {"multi": "理由を{max_length}字で書き、すべて挙げよ。"}}
    }
    rules = survey_from_dict(data).prompt.rules.reasoning
    assert rules.multi == "理由を{max_length}字で書き、すべて挙げよ。"
    # 書かなかったキーは既定のまま残る
    assert rules.single == ReasoningRules().single


def test_reasoning_rule_rejects_other_placeholders():
    """実行時まで気づけない差し込みを読み込みで止める（`open` と同じ扱い）。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"reasoning": {"single": "理由を{unknown}字で。"}}}
    with pytest.raises(SurveyDefinitionError, match="reasoning.single"):
        survey_from_dict(data)


def test_reasoning_rules_reject_unknown_question_type():
    """理由を書かせられない設問タイプの指示行は受け付けない。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"reasoning": {"open": "理由を書け。"}}}
    with pytest.raises(SurveyDefinitionError, match="reasoning"):
        survey_from_dict(data)


def test_flat_reasoning_rule_is_rejected_with_a_hint():
    """1本の文で書いていた旧形式は読まずに止める（§6.3）。

    黙って全タイプに使い回すと、置き換えで `multi` の「すべて」が落ちる。
    """
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"reasoning": "理由を{max_length}字で。"}}
    with pytest.raises(SurveyDefinitionError, match="single / scale / multi"):
        survey_from_dict(data)


# --------------------------------------------------------------------------- #
# 実行記録からの復元（`survey_from_record()`）
# --------------------------------------------------------------------------- #
#
# 記録は実行済みの事実で、書き直させる相手がいない。書き方を変えたことを理由に
# 過去の調査の結果が読めなくなってはならない。一方で、数字の意味を決める block を
# 黙って既定値にすり替えるのは論外——読む範囲と態度をここで押さえる。


def test_record_restores_what_the_aggregation_reads(survey_dict):
    survey = survey_from_record(survey_dict)

    assert survey.survey_id == "survey_test"
    assert survey.name == "テスト調査"
    assert survey.stimuli_count == 3
    assert len(survey.questions) == 6
    assert {q.measure_key for q in survey.questions} == {"q_intent", "q_reason"}
    assert [cell.cell_id for cell in survey.panel.quotas.cells] == ["M_20s", "F_20s"]
    assert survey.panel.size == 4
    assert survey.output.segments == ("total",)


def test_record_keeps_the_definition_in_full(survey_dict):
    """読まなかった block も含め、記録の全文は `raw` に残す。"""
    survey = survey_from_record(survey_dict)

    assert survey.from_record is True
    assert survey.raw == survey_dict


def test_record_does_not_read_how_it_was_asked():
    """聞き方の block は読まない。集計側に参照が無く、書き方も変わっていくため。"""
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"system": "あなたは架空の人物です。"}
    data["main_survey"]["persona_card"] = {"attributes": []}
    data["design"] = {"sample_overlap": "disjoint"}

    survey = survey_from_record(data)

    assert survey.prompt == PromptConfig()
    assert survey.persona_card == PersonaCardConfig()
    assert survey.model == RECORD_MODEL
    assert survey.design == Design()


def test_record_with_the_old_flat_reasoning_rule_is_readable():
    """1本の文で書いていた頃（§6.3 の入れ子より前）の記録も読める。

    実行はもう終わっている。`multi` の指示行が落ちる問題は**これから実行する定義**の
    話で、記録を読ませない理由にはならない。
    """
    data = base_survey_dict()
    data["main_survey"]["prompt"] = {"rules": {"reasoning": "理由を{max_length}字で。"}}

    # 同じ内容を実行しようとすれば、従来どおり書き分けを求めて止まる。
    with pytest.raises(SurveyDefinitionError, match="single / scale / multi"):
        survey_from_dict(data)

    assert survey_from_record(data).survey_id == "survey_test"


def test_record_with_legacy_top_level_blocks_is_readable():
    """`main_survey:` に移す前（トップレベルの model: / prompt:）の記録も読める。"""
    data = base_survey_dict()
    data["model"] = data.pop("main_survey")["model"]
    data["prompt"] = {"system": "あなたは架空の人物です。"}

    with pytest.raises(SurveyDefinitionError, match="main_survey"):
        survey_from_dict(data)

    assert survey_from_record(data).stimuli_count == 3


def test_record_with_removed_model_fields_is_readable():
    """廃止した `temperature` が残っている記録も読める（`main_survey` は読まないため）。"""
    data = base_survey_dict()
    data["main_survey"]["model"]["temperature"] = 0.7

    with pytest.raises(SurveyDefinitionError, match="temperature"):
        survey_from_dict(data)

    assert survey_from_record(data).model == RECORD_MODEL


def test_record_reads_the_screening_mode():
    """通過率の注記が方式を見る。実測・推定・未測定の区別は数字の意味そのもの。"""
    data = base_survey_dict()
    data["screening"] = {"mode": "infer", "conditions": ["週1回以上コーヒーを飲む"]}

    screening = survey_from_record(data).screening

    assert screening is not None
    assert screening.infers is True


def test_record_reads_the_screening_mode_from_the_old_place():
    """`screening:` に移す前は `panel.screener` に入っていた（§4.2）。"""
    data = base_survey_dict()
    data["panel"]["screener"] = {
        "mode": "ask",
        "questions": [
            {
                "id": "s1",
                "text": "コーヒーを飲みますか。",
                "options": ["飲む", "飲まない"],
                "pass_if": [1],
            }
        ],
    }

    with pytest.raises(SurveyDefinitionError, match="panel.screener"):
        survey_from_dict(data)

    screening = survey_from_record(data).screening
    assert screening is not None
    assert screening.asks is True


def test_record_without_screening_stays_none(survey_dict):
    assert survey_from_record(survey_dict).screening is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["questions"][0].update(type="unknown"), "type"),
        (lambda data: data["questions"][0].update(slot=0), "slot"),
        (lambda data: data["questions"][0].update(top_box="1,2"), "top_box"),
        (lambda data: data["panel"]["quotas"]["cells"][0].pop("n"), "n が必須"),
        (lambda data: data.pop("stimuli"), "stimuli"),
        (lambda data: data.pop("panel"), "panel"),
    ],
)
def test_record_still_stops_when_the_numbers_are_unreadable(mutate, message):
    """緩めるのは聞き方だけ。集計の数字を決める block は従来どおり止める。

    黙って既定値で埋めると、誤った表を正しい表として読んでしまう。
    """
    data = base_survey_dict()
    mutate(data)
    with pytest.raises(SurveyDefinitionError, match=message):
        survey_from_record(data)
