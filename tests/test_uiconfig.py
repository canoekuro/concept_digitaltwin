"""Web UI の設定層（`persona_sim.uiconfig`）。

要点は3つ。

1. 設定と画面入力を合成した調査定義が、**CLI と同じ検証を通る**こと
2. 整数配分を UI 側で書いていないこと（比率を渡すだけ）
3. 対象者条件が自然言語のまま `conditions` に入り、空欄ならスクリーナーが消えること
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from persona_sim.panel.validate import validate_static
from persona_sim.uiconfig import (
    Concept,
    SurveyForm,
    UIConfigError,
    age_bands,
    build_quotas,
    build_survey,
    build_survey_dict,
    build_survey_pair,
    estimate_cost,
    load_census,
    load_ui_config,
    survey_id,
    ui_config_from_dict,
)
from persona_sim.uiconfig.census import CensusRow, ratios

CONFIG_PATH = Path("app/config/ui_config.yaml")


@pytest.fixture(scope="module")
def ui():
    return load_ui_config(CONFIG_PATH)


def _form(**overrides) -> SurveyForm:
    base = dict(
        name="新ビールコンセプト評価調査",
        panel_size=500,
        sex_ranges={"男": (20, 69), "女": (20, 69)},
        allocation_pattern="sex_x_age10",
        concepts=(
            Concept(name="コンセプトA", text="【商品名】ゆずハイボール\n【価格】205円"),
            Concept(name="コンセプトB", text="【商品名】無糖レモンサワー\n【価格】190円"),
        ),
        condition="週1回以上ビールまたは発泡酒を飲む人",
        survey_id="ui_test",
    )
    base.update(overrides)
    return SurveyForm(**base)


# --------------------------------------------------------------------------- #
# 設定ファイルの読み込み
# --------------------------------------------------------------------------- #


def test_shipped_config_loads(ui):
    """リポジトリに入っている設定がそのまま読めること。"""
    assert ui.main_survey.model["deployment"]
    assert len(ui.default_questions) == 2
    assert {p.id for p in ui.allocation_patterns} == {"sex_x_age10", "sex_x_age5", "census_age5"}


def test_default_questions_carry_top_box(ui):
    """`top_box` が無いと T2B も平均スコアも None になり、結果画面が空になる。"""
    for question in ui.default_questions:
        assert question["top_box"] == [1, 2]
        assert question["randomize_options"] is False


def test_default_questions_point_at_their_own_system_prompt(ui):
    """購入意向と新規性は別々の [system] で聞く（docs/issues/202608131659.md、§6.1）。"""
    systems = ui.main_survey.prompt["systems"]
    bound = {question["id"]: question["system"] for question in ui.default_questions}

    assert bound == {"q_purchase_intent": "purchase_intent", "q_novelty": "novelty"}
    assert set(bound.values()) <= set(systems)
    assert systems["purchase_intent"] != systems["novelty"]


def test_the_system_prompt_survives_slot_expansion(ui):
    """slot 展開後の設問にも system が残り、調査定義として読めること。

    展開で落ちると、案ごとに違う [system] で聞くことになる（1案目だけ書き分けが効く 等）。
    """
    survey = build_survey(ui, _form())
    bound = {question.measure_key: question.system for question in survey.questions}

    assert bound == {"q_purchase_intent": "purchase_intent", "q_novelty": "novelty"}
    assert all(question.system in survey.prompt.systems for question in survey.questions)


def test_unknown_key_is_rejected():
    """綴り違いを黙って捨てると「設定したのに効いていない」ことに気づけない。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    data["storage"] = {"base_dir": "dbfs:/somewhere"}
    with pytest.raises(UIConfigError, match="storage"):
        ui_config_from_dict(data)


@pytest.mark.parametrize(
    "stray",
    ["model", "prompt", "default_questions", "screener", "allocation", "estimation_benchmarks"],
)
def test_keys_outside_the_two_groups_are_rejected(stray):
    """最上位に置けるのは survey_defaults と ui だけ（issue 202607301208 項目2）。

    黙って無視できてしまうと、設定を書いたのに既定で走ってしまう。
    """
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    data[stray] = {}
    with pytest.raises(UIConfigError) as excinfo:
        ui_config_from_dict(data)
    assert stray in str(excinfo.value)


def test_config_is_split_into_survey_defaults_and_ui():
    """2群に分かれていること。片方しか無い設定ファイルは受け付けない。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert set(data) == {"survey_defaults", "ui"}
    # 調査定義になるものと、画面のためだけのものが混ざっていないこと。
    assert set(data["ui"]) == {"allocation_patterns", "estimation_benchmarks"}


def test_output_no_longer_carries_formats():
    """出力形式は画面に要らない（結果はダウンロード時にその場で生成する）。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert "formats" not in data["survey_defaults"]["output"]

    data["survey_defaults"]["output"]["formats"] = ["csv"]
    with pytest.raises(UIConfigError, match="formats"):
        ui_config_from_dict(data)


def test_unsupported_band_is_rejected():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    data["ui"]["allocation_patterns"][0]["band"] = 7
    with pytest.raises(UIConfigError, match="7"):
        ui_config_from_dict(data)


def test_missing_benchmark_is_rejected():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    del data["ui"]["estimation_benchmarks"]["screener_session"]
    with pytest.raises(UIConfigError, match="input_tokens_per_session"):
        ui_config_from_dict(data)


def test_missing_pricing_is_rejected():
    """単価が無いまま予算目安を出させない。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    del data["ui"]["estimation_benchmarks"]["pricing"]
    with pytest.raises(UIConfigError, match="input_usd_per_million"):
        ui_config_from_dict(data)


def test_old_latency_key_is_rejected_with_migration_hint():
    """`avg_latency_sec`（秒/セッション）は `*_per_min`（件/分）に置き換わった。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    screener = data["ui"]["estimation_benchmarks"]["screener_session"]
    screener["avg_latency_sec"] = screener.pop("sessions_per_min")
    with pytest.raises(UIConfigError, match="sessions_per_min"):
        ui_config_from_dict(data)


@pytest.mark.parametrize("speed", [0, -1])
def test_non_positive_throughput_is_rejected(speed):
    """0 は所要時間の分母。落とすとゼロ除算で見積もりが壊れる。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    data["ui"]["estimation_benchmarks"]["survey_answer"]["answers_per_min"] = speed
    with pytest.raises(UIConfigError, match="answers_per_min"):
        ui_config_from_dict(data)


def test_budget_margin_below_one_is_rejected():
    """安全側の係数なので、標準の概算より下振れさせない。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    data["ui"]["estimation_benchmarks"]["pricing"]["budget_margin"] = 0.9
    with pytest.raises(UIConfigError, match="budget_margin"):
        ui_config_from_dict(data)


def test_segments_follow_the_allocation_band(ui):
    assert "age_band_10" in ui.segments_for(10)
    assert "sex_x_age_band_5" in ui.segments_for(5)


# --------------------------------------------------------------------------- #
# 割り付けセルの生成
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("age_min", "age_max", "band", "expected"),
    [
        (20, 69, 10, 5),
        (20, 69, 5, 10),
        (30, 39, 10, 1),
    ],
)
def test_age_bands_split_the_range(age_min, age_max, band, expected):
    bands = age_bands(age_min, age_max, band)
    assert len(bands) == expected
    assert bands[0][0] == age_min
    assert bands[-1][1] == age_max


def test_age_range_that_does_not_divide_is_rejected():
    """端数のセルを黙って作ると、そのセルだけ母集団が薄くなる。"""
    with pytest.raises(UIConfigError, match="割り切れない"):
        age_bands(25, 60, 10)


def test_equal_allocation_uses_proportions_not_counts(ui):
    """整数配分は UI で書かない。比率を渡して最大剰余法に委ねる（§4.1）。"""
    quotas = build_quotas(ui.pattern("sex_x_age10"), {"男": (20, 69), "女": (20, 69)})
    assert quotas["mode"] == "proportion"
    assert len(quotas["cells"]) == 10  # 男女 × 5階級
    assert all("n" not in cell for cell in quotas["cells"])
    assert sum(cell["proportion"] for cell in quotas["cells"]) == pytest.approx(1.0)


def test_equal_allocation_gives_every_cell_the_same_share(ui):
    quotas = build_quotas(ui.pattern("sex_x_age5"), {"男": (20, 29), "女": (20, 29)}, None)
    shares = {round(cell["proportion"], 12) for cell in quotas["cells"]}
    assert len(shares) == 1


def test_census_allocation_without_data_is_rejected(ui):
    """データが無いまま「人口構成比で割り付けた」と表示する方が害が大きい。"""
    with pytest.raises(UIConfigError, match="国勢調査"):
        build_quotas(ui.pattern("census_age5"), {"男": (20, 29), "女": (20, 29)}, None)


def test_build_quotas_rejects_no_sex_selected(ui):
    with pytest.raises(UIConfigError, match="性別"):
        build_quotas(ui.pattern("sex_x_age10"), {})


def test_build_quotas_excludes_the_unselected_sex(ui):
    """チェックを外した性別はセルを作らない。"""
    quotas = build_quotas(ui.pattern("sex_x_age10"), {"男": (20, 69)})
    assert len(quotas["cells"]) == 5  # 男のみ5階級
    assert all(cell["sex"] == "男" for cell in quotas["cells"])
    assert sum(cell["proportion"] for cell in quotas["cells"]) == pytest.approx(1.0)


def test_build_quotas_allows_different_ranges_per_sex(ui):
    """性別ごとに違う年齢範囲を指定できる。"""
    quotas = build_quotas(ui.pattern("sex_x_age10"), {"男": (20, 39), "女": (40, 69)})
    male_cells = [cell for cell in quotas["cells"] if cell["sex"] == "男"]
    female_cells = [cell for cell in quotas["cells"] if cell["sex"] == "女"]
    assert len(male_cells) == 2
    assert len(female_cells) == 3
    assert min(cell["age_min"] for cell in male_cells) == 20
    assert max(cell["age_max"] for cell in male_cells) == 39
    assert min(cell["age_min"] for cell in female_cells) == 40
    assert max(cell["age_max"] for cell in female_cells) == 69


def test_census_ratios_are_renormalized_within_the_range():
    rows = [
        CensusRow("男", 20, 24, 100),
        CensusRow("女", 20, 24, 100),
        CensusRow("男", 25, 29, 300),
        CensusRow("女", 25, 29, 100),
        CensusRow("男", 70, 74, 9999),  # 範囲外。落ちること
    ]
    result = ratios(rows, {"男": (20, 29), "女": (20, 29)}, 5)
    assert sum(result.values()) == pytest.approx(1.0)
    assert result[("男", 25, 29)] == pytest.approx(0.5)


def test_census_ratios_aggregate_five_year_bands_into_ten():
    rows = [
        CensusRow("男", 20, 24, 100),
        CensusRow("男", 25, 29, 100),
        CensusRow("女", 20, 24, 150),
        CensusRow("女", 25, 29, 50),
    ]
    result = ratios(rows, {"男": (20, 29), "女": (20, 29)}, 10)
    assert set(result) == {("男", 20, 29), ("女", 20, 29)}
    assert result[("男", 20, 29)] == pytest.approx(0.5)


def test_census_ratios_reject_a_range_outside_the_table():
    rows = [CensusRow("男", 20, 24, 100), CensusRow("女", 20, 24, 100)]
    with pytest.raises(UIConfigError, match="人口が無い"):
        ratios(rows, {"男": (20, 29), "女": (20, 29)}, 5)


def test_census_ratios_allow_a_single_sex():
    """性別を1つしか選ばなければ、その性別だけで再標準化する。"""
    rows = [CensusRow("男", 20, 24, 100), CensusRow("女", 20, 24, 999)]
    result = ratios(rows, {"男": (20, 24)}, 5)
    assert set(result) == {("男", 20, 24)}
    assert result[("男", 20, 24)] == pytest.approx(1.0)


def test_shipped_census_table_is_optional():
    """実データ未投入でも読み込みで落ちないこと（パターンが選べなくなるだけ）。"""
    assert load_census(Path("does/not/exist.csv")) is None


# --------------------------------------------------------------------------- #
# 調査定義の合成
# --------------------------------------------------------------------------- #


def test_built_survey_passes_the_same_validation_as_the_cli(ui):
    """UI 経由と調査定義ファイル経由で通る検証が違ってはいけない。"""
    report = validate_static(build_survey(ui, _form()))
    assert report.ok, report.errors


@pytest.mark.parametrize("pattern", ["sex_x_age10", "sex_x_age5"])
def test_built_survey_is_valid_for_every_equal_pattern(ui, pattern):
    survey = build_survey(ui, _form(allocation_pattern=pattern))
    assert validate_static(survey).ok


def test_sessions_match_the_counterfactual_monadic_formula(ui):
    """反実仮想モナディックなので N × コンセプト数 × 設問数（§5.3）。"""
    form = _form()
    estimate = validate_static(build_survey(ui, form)).estimate
    assert estimate is not None
    assert estimate.sessions == form.panel_size * len(form.concepts) * len(ui.default_questions)


def test_condition_becomes_a_natural_language_screener(ui):
    """選択肢や pass_if を捏造しない（§4.2）。"""
    data = build_survey_dict(ui, _form())
    screening = data["screening"]
    assert screening["conditions"] == ["週1回以上ビールまたは発泡酒を飲む人"]
    assert "questions" not in screening


def test_blank_condition_skips_screening(ui):
    data = build_survey_dict(ui, _form(condition="   "))
    assert "screening" not in data
    assert build_survey(ui, _form(condition="")).screening is None


def test_global_filters_pin_sex_when_only_one_is_selected(ui):
    data = build_survey_dict(ui, _form(sex_ranges={"男": (20, 49)}))
    assert data["panel"]["filters"] == {"sex": "男", "age_min": 20, "age_max": 49}


def test_global_filters_envelope_the_range_when_both_sexes_differ(ui):
    """複数性別・範囲違いのときは、性別を絞らず範囲だけ全体を覆う envelope にする。

    性別・年齢の厳密な絞り込みは割り付けセル条件（`panel.quotas.cells`）が担う。
    """
    data = build_survey_dict(ui, _form(sex_ranges={"男": (20, 39), "女": (40, 69)}))
    assert data["panel"]["filters"] == {"age_min": 20, "age_max": 69}


def test_survey_dict_rejects_no_sex_selected(ui):
    with pytest.raises(UIConfigError, match="性別"):
        build_survey_dict(ui, _form(sex_ranges={}))


def test_design_is_always_counterfactual_monadic(ui):
    """提示設計は設定で変えられないので、調査定義に design は書かない（§5）。"""
    data = build_survey_dict(ui, _form())
    assert "design" not in data
    # 記憶は設問側。画面は案どうしを独立に評価させるので、どの設問にも書かない。
    assert all("remember" not in question for question in data["questions"])


def test_concepts_become_numbered_stimuli(ui):
    data = build_survey_dict(ui, _form())
    assert [s["id"] for s in data["stimuli"]] == ["c1", "c2"]
    assert data["stimuli"][0]["name"] == "コンセプトA"


def test_survey_without_concepts_is_rejected(ui):
    with pytest.raises(UIConfigError, match="コンセプト"):
        build_survey_dict(ui, _form(concepts=()))


def test_survey_id_is_ascii_and_unique_per_run():
    first = survey_id("新ビールコンセプト評価調査")
    assert first.startswith("survey_")  # 日本語だけなら slug が空になる
    assert first.isascii()
    assert survey_id("Beer Concept Test").startswith("beer_concept_test_")


def test_survey_id_does_not_collide_within_the_same_second():
    """時刻だけだと、日本語名の調査は同じ秒に投入すると ID が衝突する。
    衝突すると後続の実行が先行実行の panels / responses を消してしまう。"""
    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    ids = {survey_id("新ビールコンセプト評価調査", now) for _ in range(50)}
    assert len(ids) == 50


def test_a_pinned_survey_id_survives_repeated_assembly(ui):
    """画面は組み立てを複数回通る。そのたびに ID が変わると、利用者に見せた ID と
    ジョブが結果を書き込む ID が食い違う。"""
    form = _form(survey_id="pinned_id")
    assert build_survey_dict(ui, form)["survey"]["id"] == "pinned_id"
    assert build_survey_dict(ui, form)["survey"]["id"] == "pinned_id"


def test_build_survey_pair_returns_one_definition_in_two_shapes(ui):
    """dict と検証済み定義を別々に組み立てると survey_id がずれる。"""
    built, survey = build_survey_pair(ui, _form(survey_id=None))
    assert built["survey"]["id"] == survey.survey_id


# --------------------------------------------------------------------------- #
# 見積もり
# --------------------------------------------------------------------------- #


def test_cost_estimate_converts_answers_with_the_benchmarks(ui):
    """本調査は回答件数、判定はセッション数で換算する（単位が違う）。"""
    form = _form()
    estimate = validate_static(build_survey(ui, form)).estimate
    assert estimate is not None

    cost = estimate_cost(ui.benchmarks, estimate)
    survey_input = estimate.answers * ui.benchmarks.survey_answer.input_tokens_per_answer
    screener_input = (
        estimate.screener_sessions * ui.benchmarks.screener_session.input_tokens_per_session
    )
    assert cost.input_tokens == survey_input + screener_input
    assert cost.answers == estimate.answers
    assert cost.total_sessions == estimate.sessions + estimate.screener_sessions


def test_cost_uses_answers_not_sessions(ui):
    """記憶を持つ調査（セッション数 < 回答件数）でも、換算は回答件数に従う。

    実績値は1回答あたりで取っている。セッション数に掛けると、`remember` でまとまった
    ぶんだけ出力トークンと予算が過小に出る。
    """
    estimate = validate_static(build_survey(ui, _form())).estimate
    assert estimate is not None
    # セッションだけ半分にまとまった調査（回答件数は変わらない）を作る。
    grouped = replace(estimate, sessions=estimate.sessions // 2)
    assert grouped.sessions < grouped.answers

    assert estimate_cost(ui.benchmarks, grouped).input_tokens == (
        estimate_cost(ui.benchmarks, estimate).input_tokens
    )
    assert estimate_cost(ui.benchmarks, grouped).budget_jpy == pytest.approx(
        estimate_cost(ui.benchmarks, estimate).budget_jpy
    )


def test_duration_follows_throughput(ui):
    """所要時間は実測スループット（件/分）に反比例する。並列度では割らない。"""
    estimate = validate_static(build_survey(ui, _form())).estimate
    assert estimate is not None
    survey = ui.benchmarks.survey_answer
    halved = replace(
        ui.benchmarks,
        survey_answer=replace(survey, answers_per_min=survey.answers_per_min / 2),
        screener_session=replace(
            ui.benchmarks.screener_session,
            sessions_per_min=ui.benchmarks.screener_session.sessions_per_min / 2,
        ),
    )
    assert estimate_cost(halved, estimate).duration_sec == pytest.approx(
        estimate_cost(ui.benchmarks, estimate).duration_sec * 2
    )


def test_budget_uses_configured_pricing(ui):
    """予算目安は設定した単価・為替・安全側の係数どおりに出る。"""
    estimate = validate_static(build_survey(ui, _form())).estimate
    assert estimate is not None
    cost = estimate_cost(ui.benchmarks, estimate)
    pricing = ui.benchmarks.pricing

    usd = (
        cost.input_tokens * pricing.input_usd_per_million
        + cost.output_tokens * pricing.output_usd_per_million
    ) / 1_000_000
    assert cost.budget_jpy == pytest.approx(usd * pricing.jpy_per_usd * pricing.budget_margin)


def test_budget_matches_the_measured_rate_per_answer(ui):
    """実測どおり、判定を含まない調査は1回答あたり約 0.28 円になる（issue の式）。"""
    estimate = validate_static(build_survey(ui, _form(condition=""))).estimate
    assert estimate is not None
    assert estimate.screener_sessions == 0
    cost = estimate_cost(ui.benchmarks, estimate)
    assert cost.budget_jpy / cost.answers == pytest.approx(0.28, abs=0.005)


def test_estimate_notes_that_concept_length_moves_the_input_tokens(ui):
    """コンセプト文の分量で入力トークンが変わる旨を画面に出せるようにする。"""
    estimate = validate_static(build_survey(ui, _form())).estimate
    assert estimate is not None
    assert any("コンセプト文の分量" in note for note in estimate_cost(ui.benchmarks, estimate).notes)


def test_estimate_warns_that_screening_can_retry(ui):
    """再試行でセッション数が上振れしうることを画面に出せるようにする（E2）。"""
    estimate = validate_static(build_survey(ui, _form())).estimate
    assert estimate is not None
    cost = estimate_cost(ui.benchmarks, estimate, oversample_factor=4)
    assert any("再試行" in note for note in cost.notes)


# --------------------------------------------------------------------------- #
# 調査の種類（§3.0）
# --------------------------------------------------------------------------- #


def test_shipped_config_declares_the_survey_type(ui):
    assert ui.survey_type == "concept"


def test_built_survey_carries_the_type(ui):
    """実行後にその調査が何だったのかを判別できるようにする。"""
    from persona_sim.panel.schema import SurveyType

    data = build_survey_dict(ui, _form())
    assert data["survey"]["type"] == "concept"
    assert build_survey(ui, _form()).survey_type is SurveyType.CONCEPT


def test_built_survey_has_no_warnings(ui):
    """画面に出る警告は、利用者が対処できるものだけにする。

    設定ファイルがプロンプト文言を持つ以上、既定と同じ版を名乗ってはいけない
    （W_PROMPT_TEMPLATE_VERSION）。ここが緑でないと、全調査で対処不能な警告が出る。
    """
    report = validate_static(build_survey(ui, _form()))
    assert report.warnings == [], [str(w) for w in report.warnings]


def test_built_survey_uses_the_screening_and_main_survey_blocks(ui):
    """合成結果が新構造になっていること（issue 202607301208 項目5）。"""
    data = build_survey_dict(ui, _form())

    assert set(data["main_survey"]) == {"model", "prompt", "persona_card"}
    # 設定ファイルの中身がそのまま素通しされること。
    assert data["main_survey"]["model"] == dict(ui.main_survey.model)
    assert data["main_survey"]["persona_card"] == dict(ui.main_survey.persona_card)
    # スクリーニング側は判定用のモデル・プロンプト・カードを自前で持つ。
    assert data["screening"]["prompt"] == dict(ui.screening.prompt)
    assert data["screening"]["persona_card"] == dict(ui.screening.persona_card)
    # 設定ファイルの値をそのまま渡すこと。ここに数値を直書きすると、
    # ui_config.yaml を変えるたびに本質と関係のない失敗になる。
    assert data["screening"]["batch_size"] == ui.screening.batch_size
    # 旧構造の名残が残っていないこと。
    assert "model" not in data
    assert "prompt" not in data
    assert "screener" not in data["panel"]


def test_built_survey_omits_output_formats(ui):
    """ジョブ側でファイルを書き出さない（調査定義の既定 delta のみになる）。"""
    data = build_survey_dict(ui, _form())
    assert "formats" not in data["output"]
    assert build_survey(ui, _form()).output.formats == ("csv", "xlsx")


def test_screening_carries_the_judge_settings(ui):
    """判定用 LLM のモデル・プロンプト・カードを調査定義に載せる（§4.2）。"""
    screening = build_survey_dict(ui, _form())["screening"]

    assert screening["oversample_factor"] == ui.screening.oversample_factor
    for key in ("model", "prompt", "persona_card"):
        assert screening[key]


def test_screening_does_not_carry_a_mode_or_logic(ui):
    """方式は1つしか無いので書かない。条件の結合は prompt.rule の文面が決める。"""
    screening = build_survey_dict(ui, _form())["screening"]
    assert "mode" not in screening
    assert "logic" not in screening
