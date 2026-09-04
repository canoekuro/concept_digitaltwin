"""調査設計ページ（`docs/SPEC_UI.md` §3）。

入力を `SurveyForm` に集め、`build_survey_pair()` で調査定義にし、`validate_static()` で
検証してから見積もりを出す。**ここにロジックを置かない。** 集計もセッション数の計算も
`persona_sim` 側にあり、CLI と同じ経路を通る。

`survey_id` は投入1回につき1つ。画面が表示する ID とジョブが結果を書き込む ID が
食い違うと、結果閲覧で自分の調査を特定できなくなる（`docs/SPEC_UI.md` §3.1）。
"""

from __future__ import annotations

import streamlit as st

from lib import context
from lib.layout import footer, page_header
from persona_sim.panel.validate import validate_static
from persona_sim.uiconfig import (
    SEXES,
    Concept,
    SurveyForm,
    UIConfigError,
    build_survey_pair,
    estimate_cost,
    survey_id,
    target_summary,
)
from persona_sim.uiconfig.jobs import submit_survey

ui = context.ui_config()
census = context.census()

page_header(
    "調査設計",
    "対象者・サンプル数・コンセプト・設問を決めて、調査を開始します。",
)

# --------------------------------------------------------------------------- #
# 1. 基本情報・ターゲット
# --------------------------------------------------------------------------- #

st.subheader("1. 基本情報・ターゲット")

name = st.text_input("調査名", placeholder="新ビールコンセプト評価調査")

st.caption("対象者。チェックした性別だけが調査対象になる（年齢範囲は性別ごとに指定できる）。")
sex_ranges: dict[str, tuple[int, int]] = {}
for column, sex in zip(st.columns(len(SEXES)), SEXES, strict=True):
    with column:
        if st.checkbox(f"{sex}性を対象にする", value=True, key=f"sex_enabled_{sex}"):
            age_from = st.number_input(
                f"{sex}性 対象年齢（from）",
                min_value=15,
                max_value=79,
                value=20,
                key=f"sex_age_from_{sex}",
            )
            age_to = st.number_input(
                f"{sex}性 対象年齢（to）",
                min_value=15,
                max_value=79,
                value=69,
                key=f"sex_age_to_{sex}",
            )
            sex_ranges[sex] = (int(age_from), int(age_to))

condition = st.text_area(
    "対象者条件（任意）",
    placeholder="週1回以上ビールまたは発泡酒を飲む人",
    help=(
        "自然言語で書いてください。空欄にすると対象者条件を設けず、"
        "割り付けどおりのサンプルで調査します。"
    ),
)

# --------------------------------------------------------------------------- #
# 2. サンプル・割り付け
# --------------------------------------------------------------------------- #

st.subheader("2. サンプル・割り付け")

panel_size = st.number_input("総有効サンプル数（N数）", min_value=1, value=500, step=50)

available = [p for p in ui.allocation_patterns if census is not None or not p.census]
unavailable = [p for p in ui.allocation_patterns if p not in available]
pattern = st.radio(
    "割り付けパターン",
    options=[p.id for p in available],
    format_func=lambda pid: next(p.label for p in available if p.id == pid),
    horizontal=True,
)
for missing in unavailable:
    st.caption(f"「{missing.label}」は国勢調査データが未投入のため選べません。")

# --------------------------------------------------------------------------- #
# 3. コンセプト
# --------------------------------------------------------------------------- #

st.subheader("3. コンセプト")

if "concept_ids" not in st.session_state:
    st.session_state.concept_ids = [0, 1]
    st.session_state.next_concept_id = 2

concepts: list[Concept] = []
for position, concept_id in enumerate(st.session_state.concept_ids):
    with st.expander(f"コンセプト {position + 1}", expanded=position < 2):
        concept_name = st.text_input("名称", key=f"concept_name_{concept_id}")
        concept_text = st.text_area(
            "説明文章",
            key=f"concept_text_{concept_id}",
            placeholder="【商品名】…\n【特徴】…\n【価格】…",
            height=140,
        )
        if st.button("🗑 このコンセプトを削除", key=f"concept_delete_{concept_id}"):
            st.session_state.concept_ids.remove(concept_id)
            st.rerun()
    if concept_name.strip() or concept_text.strip():
        concepts.append(Concept(name=concept_name.strip(), text=concept_text.strip()))

if st.button("＋ コンセプトを追加"):
    st.session_state.concept_ids.append(st.session_state.next_concept_id)
    st.session_state.next_concept_id += 1
    st.rerun()

# --------------------------------------------------------------------------- #
# 4. 設問
# --------------------------------------------------------------------------- #

st.subheader("4. 設問")
st.caption("既定の2問です。文言と選択肢を編集できます。選択肢の順序は変えられません（順序尺度のため）。")

questions = []
for index, default in enumerate(ui.default_questions):
    with st.expander(f"設問 {index + 1}", expanded=False):
        text = st.text_input("設問文", value=default["text"], key=f"q_text_{index}")
        options = []
        for option_index, option in enumerate(default["options"]):
            options.append(
                st.text_input(
                    f"選択肢 {option_index + 1}",
                    value=option,
                    key=f"q_{index}_opt_{option_index}",
                )
            )
    questions.append({**default, "text": text, "options": options})

# --------------------------------------------------------------------------- #
# 5. 事前チェックと見積もり
# --------------------------------------------------------------------------- #

st.subheader("5. 確認して開始")

# `survey_id` は投入1回につき1つに固定する。未指定のまま `build_survey_dict()` を
# 呼ぶと現在時刻から作り直されるため、Streamlit の再実行ごとに ID が変わり、
# 画面が「開始しました」と表示した ID と、ジョブが結果を書き込む ID が食い違う。
# 投入が済んだら捨てて、次の調査には新しい ID を振る（下の投入成功時）。
survey_name = name.strip()
if st.session_state.get("survey_id_for") != survey_name:
    st.session_state.survey_id_for = survey_name
    st.session_state.survey_id = survey_id(survey_name)

form = SurveyForm(
    name=survey_name,
    panel_size=int(panel_size),
    sex_ranges=sex_ranges,
    allocation_pattern=pattern,
    concepts=tuple(concepts),
    condition=condition,
    questions=tuple(questions),
    survey_id=st.session_state.survey_id,
)

survey = None
survey_dict = None
if not form.name:
    st.info("調査名を入力してください。")
elif not sex_ranges:
    st.info("対象の性別を1つ以上選んでください。")
elif not concepts:
    st.info("コンセプトを1件以上入力してください。")
else:
    try:
        # 画面表示・検証・投入で**同じ dict** を使う。組み直すと survey_id がずれる。
        survey_dict, survey = build_survey_pair(ui, form, census)
    except UIConfigError as exc:
        st.error(str(exc))

if survey is not None:
    report = validate_static(survey)
    for issue in report.errors:
        st.error(str(issue))
    for issue in report.warnings:
        st.warning(str(issue))

    if report.ok and report.estimate is not None:
        cost = estimate_cost(
            ui.benchmarks,
            report.estimate,
            oversample_factor=ui.screening.oversample_factor,
        )
        metrics = st.columns(5)
        metrics[0].metric("総セッション数", f"{cost.total_sessions:,}")
        metrics[1].metric("有効サンプル数／案", f"{report.estimate.effective_n_per_stimulus:,}")
        metrics[2].metric("入力トークン（概算）", f"{cost.input_tokens:,}")
        metrics[3].metric("予算目安", f"約 {cost.budget_jpy:,.0f} 円")
        metrics[4].metric("想定所要時間", f"約 {cost.duration_sec / 60:,.0f} 分")
        for note in cost.notes:
            st.caption(note)

        # 誰に聞くのかを開始前に必ず出す。チェックの外し忘れや年齢範囲の取り違えは、
        # 出さないと結果を見るまで気づけない。
        st.info(f"対象: {target_summary(survey)}")

        missing = context.missing_for_submit()
        if missing:
            context.show_missing(missing)
        elif st.button("調査を開始する", type="primary"):
            try:
                path, run_id = submit_survey(
                    context.workspace_client(),
                    context.job_id(),
                    context.survey_volume(),
                    survey_dict,
                )
            except Exception as exc:  # noqa: BLE001 - 画面に理由を出して落とさない
                st.error(f"調査を開始できませんでした: {exc}")
            else:
                st.success(
                    f"調査を開始しました（`{survey.survey_id}`）。"
                    "完了すると結果閲覧ページの一覧に、この ID で出てきます。"
                )
                st.caption(f"調査定義: `{path}` / 実行 ID: `{run_id}`")
                # 投入済みの ID を使い回さない。同じ ID で投げ直すと、ジョブ側が
                # 先行実行の panels / responses を消して書き直してしまう。
                st.session_state.pop("survey_id", None)
                st.session_state.pop("survey_id_for", None)

    with st.expander("組み立てた調査定義を見る"):
        st.json(survey_dict)

footer()
