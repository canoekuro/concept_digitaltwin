"""調査定義（YAML / dict）の読み込み（`SPEC_PHASE1.md` §3, §10.2）。

構造の不正（未知のキー・型違い・列挙値の誤り・廃止フィールド）はここで停止する。
フィールドをまたぐ整合性（E1 / E6）と警告は `persona_sim.panel.validate` が扱う。

未知のキーを黙って捨てないのは、`stimulus_per_persona` のような綴り違いが
「指定したつもりが効いていない」事故に直結するため。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import EnumType
from pathlib import Path
from typing import Any

import yaml

from persona_sim.errors import SurveyDefinitionError
from persona_sim.panel.schema import (
    DEFAULT_REASONING_MAX_LENGTH,
    MISPLACED_SCREENER_KEYS,
    NO_MEMORY,
    REMOVED_MODEL_FIELDS,
    UNSUPPORTED_QUESTION_TYPES,
    UNUSED_INFER_SCREENER_KEYS,
    Design,
    Endpoint,
    ImageMode,
    InferModelOverrides,
    ModelConfig,
    OutputConfig,
    PanelConfig,
    PersonaAttribute,
    PersonaCardConfig,
    PersonaField,
    PersonaFilter,
    Presentation,
    PromptConfig,
    PromptHeadings,
    PromptRules,
    Question,
    QuestionType,
    QuotaCell,
    QuotaMode,
    Quotas,
    ReasoningRules,
    Remember,
    RememberMode,
    Rotation,
    SampleOverlap,
    ScreenerLogic,
    ScreenerMode,
    ScreenerQuestion,
    ScreeningConfig,
    ScreeningPromptConfig,
    Stimulus,
    StructuredOutput,
    SurveyDefinition,
    SurveyType,
)

_FILTER_KEYS = (
    "sex",
    "age_min",
    "age_max",
    "prefecture_in",
    "region_in",
    "area_in",
    "marital_status_in",
    "education_level_in",
)


def load_survey(path: str | Path) -> SurveyDefinition:
    """YAML ファイルから調査定義を読み込む。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SurveyDefinitionError(f"YAML として読めない: {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise SurveyDefinitionError(f"調査定義はマッピングである必要がある: {path}")
    return survey_from_dict(data)


def survey_from_dict(data: Mapping[str, Any]) -> SurveyDefinition:
    """dict から調査定義を組み立てる（ノートブックからの利用: §10.2）。"""
    _reject_unknown(
        data,
        (
            "survey",
            "panel",
            "screening",
            "main_survey",
            "stimuli",
            "design",
            "questions",
            "output",
        ),
        "",
    )

    survey = _mapping(data, "survey", "survey")
    _reject_unknown(survey, ("id", "name", "type"), "survey")

    main_survey = _mapping(data, "main_survey", "main_survey")
    _reject_unknown(main_survey, ("model", "prompt", "persona_card"), "main_survey")

    stimuli = tuple(
        _stimulus(item, f"stimuli[{i}]") for i, item in enumerate(_sequence(data, "stimuli", "stimuli"))
    )
    question_items = list(_sequence(data, "questions", "questions"))
    questions = tuple(
        _question(item, f"questions[{i}]") for i, item in enumerate(question_items)
    )
    design = _design(data.get("design") or {})
    _reject_unusable_questions(design, stimuli, questions, question_items)
    prompt = _prompt(main_survey.get("prompt") or {})
    _reject_unknown_system_prompts(prompt, questions)
    screening = data.get("screening")

    return SurveyDefinition(
        survey_id=_str(survey, "id", "survey.id"),
        name=_str(survey, "name", "survey.name"),
        survey_type=_enum(SurveyType, survey.get("type", SurveyType.CONCEPT), "survey.type"),
        panel=_panel(_mapping(data, "panel", "panel")),
        stimuli=stimuli,
        design=design,
        questions=questions,
        model=_model(_mapping(main_survey, "model", "main_survey.model")),
        output=_output(data.get("output") or {}),
        prompt=prompt,
        persona_card=_persona_card(
            main_survey.get("persona_card"), "main_survey.persona_card", PersonaCardConfig()
        ),
        screening=_screening(screening) if screening is not None else None,
        raw=dict(data),
    )


#: 記録から復元した定義に入る `model`（`survey_from_record()`）。
#:
#: `main_survey`（model / prompt / persona_card）は**どう聞いたか**だけを決めるもので、
#: 集計はどれも読まない。だから記録からは組み立てない。それでも
#: `SurveyDefinition.model` は省略できないので、実行には使えないと読み取れる値を入れる。
#: 実際に何で回したかは `runs.metadata_json` の `model` 欄と `SurveyDefinition.raw` にある。
RECORD_MODEL = ModelConfig(endpoint=Endpoint.FAKE, deployment="")


def survey_from_record(record: Mapping[str, Any]) -> SurveyDefinition:
    """実行記録に残った調査定義から、**集計が読む範囲だけ**を復元する（`SPEC_UI.md` §4.3）。

    `survey_from_dict()` との違いは、読む範囲と、読まなかったものへの態度。

    調査定義の書き方は変わっていく。`survey_from_dict()` が旧い書き方を止めるのは、
    **これから実行する定義**に「指定したつもり」を残さないため。記録は違う——実行は
    もう終わっていて、書き直させる相手がいない。書き方を変えたからといって、過去の
    調査の結果が読めなくなってはならない。実際、`prompt.rules.reasoning` を設問
    タイプごとの入れ子にしたとき（§6.3）、それ以前に実行した調査の結果閲覧が
    **聞き方の文言を理由に**丸ごと落ちていた。

    そこで境界を「集計の数字を決めるものか」で引く。

    - **読む**（`survey_from_dict()` と同じ厳格さで）: `survey` / `panel` / `stimuli` /
      `questions` / `output` と `screening` の方式。セル・目標人数・コンセプト・設問
      タイプ・選択肢・`measure` / `slot`・セグメント軸は数字の意味そのもので、黙って
      既定値にすり替えると誤った表が出る
    - **読まない**: `main_survey`（model / prompt / persona_card）と `design`。集計側に
      参照が無い。ここに何がどの書き方で入っていても結果には効かない
    - **未知のキー・旧い書き方は無視する。** 綴りの誤りを教える相手がいない

    読まなかった block は既定値のまま入る。**返る定義を実行や再現の根拠にしない**こと
    （`SurveyDefinition.from_record` が印になる）。定義の全文は `raw` に残してある。
    """
    survey = _mapping(record, "survey", "survey")
    panel = _mapping(record, "panel", "panel")
    # `panel.screener` は §4.2 で `screening:` に移した。記録には旧い置き場所のまま
    # 残っているので、panel からは外し、方式だけそちらから読む。
    screening = record.get("screening")
    if screening is None:
        screening = panel.get("screener")
        screening_path = "panel.screener"
    else:
        screening_path = "screening"

    return SurveyDefinition(
        survey_id=_str(survey, "id", "survey.id"),
        name=_str(survey, "name", "survey.name"),
        survey_type=_enum(SurveyType, survey.get("type", SurveyType.CONCEPT), "survey.type"),
        # 記録には `panel.screener:` 時代のものが残る。書き直させる相手がいないので落として読む。
        panel=_panel({key: value for key, value in panel.items() if key != "screener"}),
        stimuli=tuple(
            _stimulus(item, f"stimuli[{i}]")
            for i, item in enumerate(_sequence(record, "stimuli", "stimuli"))
        ),
        design=Design(),
        questions=tuple(
            _question(item, f"questions[{i}]")
            for i, item in enumerate(_sequence(record, "questions", "questions"))
        ),
        model=RECORD_MODEL,
        output=_output(record.get("output") or {}),
        screening=_record_screening(screening, screening_path),
        raw=dict(record),
        from_record=True,
    )


def _record_screening(source: Any, path: str) -> ScreeningConfig | None:
    """記録に残ったスクリーニングから**方式だけ**読む（`survey_from_record()`）。

    通過率の注記（`aggregate.panel_composition_table_from_rows`）が方式を見る。実測
    （`ask`）か推定（`infer`）か未測定（`assume`）かで数字の意味が変わるため、ここは
    復元する。条件文・判定プロンプト・判定モデルは聞き方なので読まない。
    """
    if source is None:
        return None
    mapping = _as_mapping(source, path)
    return ScreeningConfig(
        mode=_enum(ScreenerMode, mapping.get("mode", ScreenerMode.ASK), f"{path}.mode")
    )


# --------------------------------------------------------------------------- #
# セクションごとの組み立て
# --------------------------------------------------------------------------- #


def _panel(panel: Mapping[str, Any]) -> PanelConfig:
    _reject_unknown(panel, ("size", "seed", "quotas", "filters"), "panel")
    quotas = _mapping(panel, "quotas", "panel.quotas")
    _reject_unknown(quotas, ("mode", "cells"), "panel.quotas")

    mode = _enum(QuotaMode, quotas.get("mode", QuotaMode.COUNT), "panel.quotas.mode")
    cells = tuple(
        _cell(item, mode, f"panel.quotas.cells[{i}]")
        for i, item in enumerate(_sequence(quotas, "cells", "panel.quotas.cells"))
    )
    return PanelConfig(
        size=_int(panel, "size", "panel.size"),
        seed=_int(panel, "seed", "panel.seed"),
        quotas=Quotas(mode=mode, cells=cells),
        filters=_filter(panel.get("filters") or {}, "panel.filters"),
    )


def _cell(item: Any, mode: QuotaMode, path: str) -> QuotaCell:
    cell = _as_mapping(item, path)
    _reject_unknown(cell, ("cell_id", "n", "proportion", *_FILTER_KEYS), path)
    cell_id = _str(cell, "cell_id", f"{path}.cell_id")

    n = cell.get("n")
    proportion = cell.get("proportion")
    if mode is QuotaMode.COUNT:
        if n is None:
            raise SurveyDefinitionError(f"{path}: quotas.mode が count なので n が必須")
        if proportion is not None:
            raise SurveyDefinitionError(f"{path}: quotas.mode が count なので proportion は指定できない")
    else:
        if proportion is None:
            raise SurveyDefinitionError(f"{path}: quotas.mode が proportion なので proportion が必須")
        if n is not None:
            raise SurveyDefinitionError(f"{path}: quotas.mode が proportion なので n は指定できない")

    return QuotaCell(
        cell_id=cell_id,
        conditions=_filter(cell, path, ignore_unknown=True),
        n=None if n is None else _as_int(n, f"{path}.n"),
        proportion=None if proportion is None else _as_float(proportion, f"{path}.proportion"),
    )


def _filter(source: Any, path: str, *, ignore_unknown: bool = False) -> PersonaFilter:
    mapping = _as_mapping(source, path)
    if not ignore_unknown:
        _reject_unknown(mapping, _FILTER_KEYS, path)
    return PersonaFilter(
        sex=_opt_str(mapping.get("sex"), f"{path}.sex"),
        age_min=_opt_int(mapping.get("age_min"), f"{path}.age_min"),
        age_max=_opt_int(mapping.get("age_max"), f"{path}.age_max"),
        prefecture_in=_opt_str_tuple(mapping.get("prefecture_in"), f"{path}.prefecture_in"),
        region_in=_opt_str_tuple(mapping.get("region_in"), f"{path}.region_in"),
        area_in=_opt_str_tuple(mapping.get("area_in"), f"{path}.area_in"),
        marital_status_in=_opt_str_tuple(
            mapping.get("marital_status_in"), f"{path}.marital_status_in"
        ),
        education_level_in=_opt_str_tuple(
            mapping.get("education_level_in"), f"{path}.education_level_in"
        ),
    )


def _screening(source: Any) -> ScreeningConfig:
    """スクリーニング定義（`screening:`）。**方式ごとに書ける形が違う**（§4.2）。

    `ask` は `questions`、`assume` / `infer` は `conditions`。
    片方に他方の書き方を混ぜたら、移し方を添えて停止する。黙って無視すると
    「条件を書いたのに効いていない」ことに実行後まで気づけない。
    """
    path = "screening"
    mapping = _as_mapping(source, path)
    _reject_unknown(
        mapping,
        (
            "model",
            "prompt",
            "persona_card",
            "mode",
            "questions",
            "conditions",
            "logic",
            "oversample_factor",
            "batch_size",
        ),
        path,
    )
    mode = _enum(ScreenerMode, mapping.get("mode", ScreenerMode.ASK), f"{path}.mode")

    misplaced = "conditions" if mode is ScreenerMode.ASK else "questions"
    if mapping.get(misplaced) is not None:
        raise SurveyDefinitionError(f"{path}: {MISPLACED_SCREENER_KEYS[misplaced]}")

    if mode is ScreenerMode.INFER:
        # 値ではなくキーの有無で見る。既定のままなら書いていないので通す。
        for unused, hint in UNUSED_INFER_SCREENER_KEYS.items():
            if unused in mapping:
                raise SurveyDefinitionError(f"{path}.{unused}: {hint}")

    if mode is ScreenerMode.ASK:
        questions = tuple(
            _screener_question(item, f"{path}.questions[{i}]")
            for i, item in enumerate(_sequence(mapping, "questions", f"{path}.questions"))
        )
        conditions: tuple[str, ...] = ()
    else:
        questions = ()
        conditions = tuple(
            _as_str(item, f"{path}.conditions[{i}]")
            for i, item in enumerate(_sequence(mapping, "conditions", f"{path}.conditions"))
        )

    defaults = ScreeningConfig()
    return ScreeningConfig(
        model=_infer_model(mapping.get("model"), f"{path}.model"),
        prompt=_screening_prompt(mapping.get("prompt"), f"{path}.prompt"),
        persona_card=_persona_card(
            mapping.get("persona_card"), f"{path}.persona_card", defaults.persona_card
        ),
        mode=mode,
        questions=questions,
        conditions=conditions,
        logic=_enum(ScreenerLogic, mapping.get("logic", ScreenerLogic.ALL), f"{path}.logic"),
        oversample_factor=_as_int(
            mapping.get("oversample_factor", defaults.oversample_factor),
            f"{path}.oversample_factor",
        ),
        batch_size=_as_int(mapping.get("batch_size", defaults.batch_size), f"{path}.batch_size"),
    )


def _screening_prompt(source: Any, path: str) -> ScreeningPromptConfig:
    """スクリーニング判定のプロンプト。省略時は既定のまま。"""
    defaults = ScreeningPromptConfig()
    if source is None:
        return defaults
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, ("system", "rule"), path)
    return ScreeningPromptConfig(
        system=_opt_str(mapping.get("system"), f"{path}.system") or defaults.system,
        rule=_opt_str(mapping.get("rule"), f"{path}.rule") or defaults.rule,
    )


def _persona_card(source: Any, path: str, defaults: PersonaCardConfig) -> PersonaCardConfig:
    """ペルソナカードの中身。本調査とスクリーニングで既定が違うので渡してもらう。

    `attributes` を空リストと書けば属性行が出ない。省略（キー無し）と空リストは別物なので、
    `None` かどうかで見分ける。
    """
    if source is None:
        return defaults
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, ("attributes", "include_summary", "persona_fields"), path)

    raw_attributes = mapping.get("attributes")
    if raw_attributes is None:
        attributes = defaults.attributes
    else:
        attributes = tuple(
            _persona_attribute(item, f"{path}.attributes[{index}]")
            for index, item in enumerate(_as_sequence(raw_attributes, f"{path}.attributes"))
        )

    raw_fields = mapping.get("persona_fields")
    if raw_fields is None:
        persona_fields = defaults.persona_fields
    else:
        persona_fields = tuple(
            _persona_field(item, f"{path}.persona_fields[{index}]")
            for index, item in enumerate(_as_sequence(raw_fields, f"{path}.persona_fields"))
        )

    return PersonaCardConfig(
        attributes=attributes,
        include_summary=bool(mapping.get("include_summary", defaults.include_summary)),
        persona_fields=persona_fields,
    )


def _persona_attribute(source: Any, path: str) -> PersonaAttribute:
    entry = _as_mapping(source, path)
    _reject_unknown(entry, ("field", "suffix"), path)
    return PersonaAttribute(
        field=_str(entry, "field", f"{path}.field"),
        suffix=_opt_str(entry.get("suffix"), f"{path}.suffix") or "",
    )


def _persona_field(source: Any, path: str) -> PersonaField:
    entry = _as_mapping(source, path)
    _reject_unknown(entry, ("field", "label"), path)
    return PersonaField(
        field=_str(entry, "field", f"{path}.field"),
        label=_str(entry, "label", f"{path}.label"),
    )


def _infer_model(source: Any, path: str) -> InferModelOverrides:
    """判定モデルの部分指定。書かれたキーだけを持つ。

    `thinking` を受け付けないのは、常に OFF が不変条件だから（`AGENTS.md`）。
    書ける形にしておくと「切ったつもり／入れたつもり」の余地が残る。
    """
    if source is None:
        return InferModelOverrides()
    mapping = _as_mapping(source, path)
    _reject_removed_model_fields(mapping, path)
    _reject_unknown(
        mapping,
        ("endpoint", "deployment", "max_tokens", "concurrency", "structured_output"),
        path,
    )
    return InferModelOverrides(
        endpoint=(
            _enum(Endpoint, mapping["endpoint"], f"{path}.endpoint")
            if mapping.get("endpoint") is not None
            else None
        ),
        deployment=_opt_str(mapping.get("deployment"), f"{path}.deployment"),
        max_tokens=_opt_int(mapping.get("max_tokens"), f"{path}.max_tokens"),
        concurrency=_opt_int(mapping.get("concurrency"), f"{path}.concurrency"),
        structured_output=(
            _enum(StructuredOutput, mapping["structured_output"], f"{path}.structured_output")
            if mapping.get("structured_output") is not None
            else None
        ),
    )


def _screener_question(item: Any, path: str) -> ScreenerQuestion:
    mapping = _as_mapping(item, path)
    _reject_unknown(mapping, ("id", "text", "type", "options", "pass_if", "label", "premise"), path)
    return ScreenerQuestion(
        id=_str(mapping, "id", f"{path}.id"),
        text=_str(mapping, "text", f"{path}.text"),
        type=_question_type(mapping.get("type", QuestionType.SINGLE), f"{path}.type"),
        options=_opt_str_tuple(mapping.get("options"), f"{path}.options") or (),
        pass_if=tuple(
            _as_int(v, f"{path}.pass_if[{i}]")
            for i, v in enumerate(_as_sequence(mapping.get("pass_if") or [], f"{path}.pass_if"))
        ),
        label=_opt_str(mapping.get("label"), f"{path}.label"),
        premise=_opt_str(mapping.get("premise"), f"{path}.premise"),
    )


def _stimulus(item: Any, path: str) -> Stimulus:
    mapping = _as_mapping(item, path)
    _reject_unknown(mapping, ("id", "name", "text", "image_uri", "image_mode"), path)
    return Stimulus(
        id=_str(mapping, "id", f"{path}.id"),
        name=_opt_str(mapping.get("name"), f"{path}.name") or _str(mapping, "id", f"{path}.id"),
        text=_str(mapping, "text", f"{path}.text"),
        image_uri=_opt_str(mapping.get("image_uri"), f"{path}.image_uri"),
        image_mode=_enum(ImageMode, mapping.get("image_mode", ImageMode.NONE), f"{path}.image_mode"),
    )


def _design(source: Any) -> Design:
    path = "design"
    mapping = _as_mapping(source, path)
    _reject_unknown(
        mapping,
        ("sample_overlap", "presentation", "stimuli_per_persona", "rotation"),
        path,
    )
    return Design(
        sample_overlap=_enum(
            SampleOverlap, mapping.get("sample_overlap", SampleOverlap.SAME), f"{path}.sample_overlap"
        ),
        presentation=_enum(
            Presentation, mapping.get("presentation", Presentation.SEQUENTIAL), f"{path}.presentation"
        ),
        stimuli_per_persona=_opt_int(
            mapping.get("stimuli_per_persona"), f"{path}.stimuli_per_persona"
        ),
        rotation=_enum(Rotation, mapping.get("rotation", Rotation.NONE), f"{path}.rotation"),
    )


def _question(item: Any, path: str) -> Question:
    mapping = _as_mapping(item, path)
    _reject_unknown(
        mapping,
        (
            "id",
            "text",
            "type",
            "options",
            "randomize_options",
            "top_box",
            "max_length",
            "slot",
            "measure",
            "remember",
            "system",
            "reasoning",
            "reasoning_max_length",
        ),
        path,
    )
    top_box = mapping.get("top_box")
    slot = _as_int(mapping.get("slot", 1), f"{path}.slot")
    if slot < 1:
        raise SurveyDefinitionError(f"{path}.slot: 1 以上の整数（何番目に提示するコンセプトか）")
    return Question(
        id=_str(mapping, "id", f"{path}.id"),
        text=_str(mapping, "text", f"{path}.text"),
        type=_question_type(mapping.get("type"), f"{path}.type"),
        options=_opt_str_tuple(mapping.get("options"), f"{path}.options") or (),
        randomize_options=bool(mapping.get("randomize_options", False)),
        top_box=None
        if top_box is None
        else tuple(
            _as_int(v, f"{path}.top_box[{i}]")
            for i, v in enumerate(_as_sequence(top_box, f"{path}.top_box"))
        ),
        max_length=_opt_int(mapping.get("max_length"), f"{path}.max_length"),
        slot=slot,
        measure=_opt_str(mapping.get("measure"), f"{path}.measure"),
        remember=_remember(mapping.get("remember"), f"{path}.remember"),
        system=_opt_str(mapping.get("system"), f"{path}.system"),
        reasoning=bool(mapping.get("reasoning", False)),
        reasoning_max_length=_as_int(
            mapping.get("reasoning_max_length", DEFAULT_REASONING_MAX_LENGTH),
            f"{path}.reasoning_max_length",
        ),
    )


def _reject_unusable_questions(
    design: Design,
    stimuli: Sequence[Stimulus],
    questions: Sequence[Question],
    question_items: Sequence[Any],
) -> None:
    """**設問が黙って消える**書き方を、読み込みの時点で止める。

    `validate` も同じ不備を E6 として報告するが、あちらは通らずに実行できる——
    `run` / `screen` / `aggregate` はどれも `load_survey()` を呼ぶだけで
    `validate_static()` を通らない（`cli.py`）。`survey_from_dict()` は**これから実行
    する定義**を組み立てる唯一の場所で、CLI・ノートブック・画面（`uiconfig/build.py`）の
    すべてがここを通る。**結果の意味が変わる不備はここで止める。**

    実行済みの記録を読み直す経路（`survey_from_record()`）はここを通らない。止めても
    直せる相手がいないうえ、結果を読ませないことのほうが害が大きい。

    役割は分けている。`validate` は全件を集めて報告する（1つ直すたびに再実行させない）、
    ここは最初の1件で止める。
    """
    _reject_duplicate_question_ids(questions)

    if design.presentation is Presentation.SIMULTANEOUS:
        # 全案を1度に見せるので slot を持たない。被覆の概念が無い。
        return
    try:
        per_persona = design.stimuli_count_per_persona(len(stimuli))
    except SurveyDefinitionError:
        # stimuli_per_persona の欠落。E6 として validate が扱うので、ここでは判定しない。
        return

    _reject_uncovered_slots(per_persona, questions)


def _reject_unknown_system_prompts(prompt: PromptConfig, questions: Sequence[Question]) -> None:
    """`questions[].system` が `main_survey.prompt.systems` にある名前かを確かめる。

    綴り違いを既定へ黙って落とすと、**書き分けたつもりの設問が既定の `[system]` で
    聞かれる**。プロンプトの違いは回答の傾向そのものを変えるので、実行後に
    `prompt_sample.md` を読み比べるまで気づけない。読み込みの時点で止める。
    """
    for question in questions:
        if question.system is None or question.system in prompt.systems:
            continue
        available = ", ".join(sorted(prompt.systems)) or "（1つも定義されていない）"
        raise SurveyDefinitionError(
            f"questions[{question.id}].system: {question.system!r} は"
            " main_survey.prompt.systems に無い。"
            f" 定義されているのは {available}。"
            "ここに書くのは文面ではなく systems の名前"
        )


def _reject_duplicate_question_ids(questions: Sequence[Question]) -> None:
    """設問IDの重複を止める。

    実行計画は設問IDをキーにした対応表で組み立てるので、**重複した設問は畳まれて
    1問まるごと聞かれずに消える**。`remember` も設問IDで参照するので、重複していると
    どちらを指すのかも決まらない。
    """
    seen: set[str] = set()
    for question in questions:
        if question.id in seen:
            raise SurveyDefinitionError(
                f"questions[{question.id}]: 設問IDが重複している。"
                "実行計画は設問IDで組み立てるので、重複した設問は聞かれずに消える。"
                "IDは調査定義の中で一意にすること（slot ごとに展開した設問も別IDにする）"
            )
        seen.add(question.id)


def _reject_uncovered_slots(per_persona: int, questions: Sequence[Question]) -> None:
    """`1..m` の slot が過不足なく埋まっているかを見る。

    抜けたコンセプトは `panels.assigned_stimuli` に入って**評価枠を消費する**
    （有効サンプル数にも数えられる）のに、1問も聞かれない。集計表には n=0 の行だけが残る。

    範囲外は `run/memory.py::stimulus_for()` でも確かめているが、あちらは1ペルソナぶんの
    割り当てしか見えない最後の砦。**どの slot が抜けているかを言えるのは、定義全体を
    見るここだけ。**
    """
    used = {question.slot for question in questions}
    out_of_range = sorted(slot for slot in used if not 1 <= slot <= per_persona)
    if out_of_range:
        raise SurveyDefinitionError(
            f"questions: 1ペルソナが評価するコンセプトは {per_persona} 件なので"
            f" slot は 1〜{per_persona}。範囲外の slot がある: {out_of_range}"
        )
    missing = sorted(set(range(1, per_persona + 1)) - used)
    if missing:
        raise SurveyDefinitionError(
            f"questions: slot {missing} の設問が無い。そのコンセプトは提示されて"
            "評価枠を消費するのに1問も聞かれない（集計表には n=0 の行だけが残る）。"
            f"1〜{per_persona} のすべての slot に設問を書くこと"
        )



def _remember(value: Any, path: str) -> Remember:
    """`remember` を読む。`none` / `all` / 設問IDのリストの3形だけ受ける。

    書かなかった場合は「何も覚えない」。**調査全体の設定からは引かない**——
    `remember` の値は調査定義を読むだけで意味が決まる（§5.1）。

    空リストは `none` と同義として受ける。YAML で `remember: []` と書いたときに
    「書いたのに効かない」にならないようにする。
    """
    if value is None:
        return NO_MEMORY
    if isinstance(value, str):
        try:
            mode = RememberMode(value)
        except ValueError:
            raise SurveyDefinitionError(
                f"{path}: {value!r} は解釈できない。none / all / 設問IDのリストのいずれかを書くこと"
            ) from None
        if mode is RememberMode.SELECTED:
            raise SurveyDefinitionError(
                f"{path}: 'selected' は直接書けない。覚えさせたい設問IDをリストで並べること"
            )
        return Remember(mode)
    ids = tuple(
        _as_str(v, f"{path}[{i}]") for i, v in enumerate(_as_sequence(value, path))
    )
    if not ids:
        return Remember(RememberMode.NONE)
    duplicated = sorted({qid for qid in ids if ids.count(qid) > 1})
    if duplicated:
        raise SurveyDefinitionError(f"{path}: 設問IDが重複している（{', '.join(duplicated)}）")
    return Remember(RememberMode.SELECTED, ids)


def _question_type(value: Any, path: str) -> QuestionType:
    if isinstance(value, str) and value in UNSUPPORTED_QUESTION_TYPES:
        raise SurveyDefinitionError(
            f"{path}: 設問タイプ {value!r} はフェーズ1では非対応（SPEC_PHASE1.md §3.1）。"
            f" 対応するのは {', '.join(t.value for t in QuestionType)}"
        )
    return _enum(QuestionType, value, path)


def _model(mapping: Mapping[str, Any], path: str = "main_survey.model") -> ModelConfig:
    _reject_removed_model_fields(mapping, path)
    _reject_unknown(
        mapping,
        (
            "endpoint",
            "deployment",
            "thinking",
            "max_tokens",
            "max_tokens_open",
            "max_tokens_reasoning",
            "concurrency",
            "structured_output",
            "request_timeout_sec",
        ),
        path,
    )
    # 省略時の既定は `ModelConfig` から引く。ここにリテラルで書くと二重定義になり、
    # 片方を変えたときに黙ってずれる（`_prompt_rules()` と同じ作法）。
    endpoint = _enum(
        Endpoint, _str(mapping, "endpoint", f"{path}.endpoint"), f"{path}.endpoint"
    )
    deployment = _str(mapping, "deployment", f"{path}.deployment")
    defaults = ModelConfig(endpoint=endpoint, deployment=deployment)

    return ModelConfig(
        endpoint=endpoint,
        deployment=deployment,
        thinking=bool(mapping.get("thinking", defaults.thinking)),
        max_tokens=_as_int(mapping.get("max_tokens", defaults.max_tokens), f"{path}.max_tokens"),
        max_tokens_open=_as_int(
            mapping.get("max_tokens_open", defaults.max_tokens_open), f"{path}.max_tokens_open"
        ),
        max_tokens_reasoning=_as_int(
            mapping.get("max_tokens_reasoning", defaults.max_tokens_reasoning),
            f"{path}.max_tokens_reasoning",
        ),
        concurrency=_as_int(mapping.get("concurrency", defaults.concurrency), f"{path}.concurrency"),
        structured_output=_enum(
            StructuredOutput,
            mapping.get("structured_output", defaults.structured_output),
            f"{path}.structured_output",
        ),
        request_timeout_sec=_as_int(
            mapping.get("request_timeout_sec", defaults.request_timeout_sec),
            f"{path}.request_timeout_sec",
        ),
    )


def _prompt(source: Any, path: str = "main_survey.prompt") -> PromptConfig:
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, ("system", "headings", "rules", "systems"), path)

    defaults = PromptConfig()
    return PromptConfig(
        system=_opt_str(mapping.get("system"), f"{path}.system") or defaults.system,
        systems=_prompt_systems(mapping.get("systems"), f"{path}.systems"),
        headings=_prompt_headings(mapping.get("headings"), f"{path}.headings"),
        rules=_prompt_rules(mapping.get("rules"), f"{path}.rules"),
    )


def _prompt_systems(source: Any, path: str) -> dict[str, str]:
    """設問ごとに使い分ける `[system]`。`{名前: 文面}`（§6.1）。

    空の文面を通さない。省略と区別がつかないまま「書いたのに既定で聞かれた」ことになる。
    """
    if source is None:
        return {}
    mapping = _as_mapping(source, path)
    systems: dict[str, str] = {}
    for name, value in mapping.items():
        if not isinstance(name, str) or not name.strip():
            raise SurveyDefinitionError(f"{path}: 名前は空でない文字列で書くこと（実際: {name!r}）")
        text = _opt_str(value, f"{path}.{name}")
        if not text:
            raise SurveyDefinitionError(
                f"{path}.{name}: 中身が空。書き分けるつもりの設問が既定の system で聞かれてしまう"
            )
        systems[name] = text
    return systems


def _prompt_headings(source: Any, path: str) -> PromptHeadings:
    """見出しの上書き。省略したキーは既定のまま残す。"""
    defaults = PromptHeadings()
    if source is None:
        return defaults
    mapping = _as_mapping(source, path)
    keys = tuple(defaults.__dataclass_fields__)
    _reject_unknown(mapping, keys, path)
    return PromptHeadings(
        **{
            key: _opt_str(mapping.get(key), f"{path}.{key}") or getattr(defaults, key)
            for key in keys
        }
    )


def _prompt_rules(source: Any, path: str) -> PromptRules:
    """指示行の上書き。省略したキーは既定のまま残す。

    設問タイプの指示行で `{max_length}` を埋め込めるのは `open` だけ。`reasoning` は
    入れ子（`ReasoningRules`）なので `_reasoning_rules()` が受け持つ。他のキーに `{}` を
    書くと実行時まで気づけないので、ここで組み立ててみて弾く。
    """
    defaults = PromptRules()
    if source is None:
        return defaults
    mapping = _as_mapping(source, path)
    keys = tuple(defaults.__dataclass_fields__)
    _reject_unknown(mapping, keys, path)

    type_keys = tuple(key for key in keys if key != "reasoning")
    rules = PromptRules(
        **{
            key: _opt_str(mapping.get(key), f"{path}.{key}") or getattr(defaults, key)
            for key in type_keys
        },
        reasoning=_reasoning_rules(mapping.get("reasoning"), f"{path}.reasoning"),
    )
    for key in type_keys:
        _check_rule_placeholders(
            getattr(rules, key), f"{path}.{key}", takes_max_length=key == "open"
        )
    return rules


def _reasoning_rules(source: Any, path: str) -> ReasoningRules:
    """`reasoning: true` の設問の指示行。**設問タイプごと**に書く（§6.3）。

    1本の文を全タイプで使い回す形は受けない。設問タイプの指示行を置き換えたときに
    そのタイプ固有の指示が落ちる——`multi` の「すべて、カンマ区切りで」が消え、
    複数回答なのに1つだけ選ばせる問いになる。
    """
    defaults = ReasoningRules()
    if source is None:
        return defaults
    if isinstance(source, str):
        raise SurveyDefinitionError(
            f"{path}: 1本の文ではなく、single / scale / multi の入れ子で書くこと（§6.3）。"
        )
    mapping = _as_mapping(source, path)
    keys = tuple(defaults.__dataclass_fields__)
    _reject_unknown(mapping, keys, path)

    rules = ReasoningRules(
        **{
            key: _opt_str(mapping.get(key), f"{path}.{key}") or getattr(defaults, key)
            for key in keys
        }
    )
    for key in keys:
        _check_rule_placeholders(getattr(rules, key), f"{path}.{key}", takes_max_length=True)
    return rules


def _check_rule_placeholders(template: str, path: str, *, takes_max_length: bool) -> None:
    """指示行の差し込みが解決できるか、組み立ててみて確かめる。"""
    allowed = {"max_length": 0} if takes_max_length else {}
    try:
        template.format(**allowed)
    except (KeyError, IndexError) as exc:
        usable = "{max_length} のみ使える" if takes_max_length else "プレースホルダは使えない"
        raise SurveyDefinitionError(
            f"{path}: 差し込み {exc} は解決できない（{usable}）。"
            "波括弧をそのまま出したい場合は {{ }} と二重にすること"
        ) from exc


def _output(source: Any) -> OutputConfig:
    path = "output"
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, ("segments", "formats"), path)
    return OutputConfig(
        segments=_opt_str_tuple(mapping.get("segments"), f"{path}.segments") or ("total",),
        formats=_opt_str_tuple(mapping.get("formats"), f"{path}.formats") or ("delta",),
    )


# --------------------------------------------------------------------------- #
# 小さなヘルパ
# --------------------------------------------------------------------------- #


def _reject_removed_model_fields(mapping: Mapping[str, Any], path: str) -> None:
    """`model` / `infer.model` の廃止キーを、廃止した理由つきで止める。

    未知のキーとして一括で弾くと「打ち間違えたのか、廃止されたのか」が読み取れない。
    黙って無視すれば「指定したつもり」が残る（`AGENTS.md` 不変条件）。
    """
    for removed, hint in REMOVED_MODEL_FIELDS.items():
        if removed in mapping:
            raise SurveyDefinitionError(f"{path}.{removed}: {hint}")


def _reject_unknown(mapping: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        where = path or "ルート"
        raise SurveyDefinitionError(
            f"{where}: 未知のキー {', '.join(unknown)}。指定できるのは {', '.join(sorted(allowed))}"
        )


def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SurveyDefinitionError(f"{path}: マッピングである必要がある（実際: {type(value).__name__}）")
    return value


def _as_sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SurveyDefinitionError(f"{path}: リストである必要がある（実際: {type(value).__name__}）")
    return value


def _mapping(source: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    if key not in source:
        raise SurveyDefinitionError(f"{path} が無い")
    return _as_mapping(source[key], path)


def _sequence(source: Mapping[str, Any], key: str, path: str) -> Sequence[Any]:
    if key not in source:
        raise SurveyDefinitionError(f"{path} が無い")
    values = _as_sequence(source[key], path)
    if not values:
        raise SurveyDefinitionError(f"{path} が空")
    return values


def _str(source: Mapping[str, Any], key: str, path: str) -> str:
    if key not in source or source[key] is None:
        raise SurveyDefinitionError(f"{path} が無い")
    return _as_str(source[key], path)


def _int(source: Mapping[str, Any], key: str, path: str) -> int:
    if key not in source or source[key] is None:
        raise SurveyDefinitionError(f"{path} が無い")
    return _as_int(source[key], path)


def _as_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise SurveyDefinitionError(f"{path}: 文字列である必要がある（実際: {type(value).__name__}）")
    return value


def _as_int(value: Any, path: str) -> int:
    # bool は int のサブクラスなので明示的に弾く。
    if isinstance(value, bool) or not isinstance(value, int):
        raise SurveyDefinitionError(f"{path}: 整数である必要がある（実際: {type(value).__name__}）")
    return value


def _as_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SurveyDefinitionError(f"{path}: 数値である必要がある（実際: {type(value).__name__}）")
    return float(value)


def _opt_str(value: Any, path: str) -> str | None:
    return None if value is None else _as_str(value, path)


def _opt_int(value: Any, path: str) -> int | None:
    return None if value is None else _as_int(value, path)


def _opt_str_tuple(value: Any, path: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(_as_str(v, f"{path}[{i}]") for i, v in enumerate(_as_sequence(value, path)))


def _enum(enum_cls: EnumType, value: Any, path: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        raise SurveyDefinitionError(f"{path}: {value!r} は不正。指定できるのは {allowed}") from None
