"""結果閲覧ページ（`docs/SPEC_UI.md` §4）。

完了した調査を選び、「データを取得」で `responses ⋈ panels ⋈ personas_base` と
ローデータを取ってくる。**画面に出すのはクロス集計表だけ**で、集計表もダウンロードも
その取得結果から作る。

集計の算術は `persona_sim.aggregate` の純関数を通す。CLI（Spark 経由）と同じ関数なので、
読み方が違っても数値は一致する。

グラフとサマリーカードは置かない。コンセプト間の比較は表の T2B・平均で読む。
"""

from __future__ import annotations

import streamlit as st

from lib import context
from lib.layout import footer, page_header
from persona_sim.aggregate import segments as segment_axes
from persona_sim.aggregate.crosstab import tabulated_measures
from persona_sim.aggregate.tables import concept_axis_table, stacked_crosstab_table
from persona_sim.storage import warehouse
from persona_sim.uiconfig import target_summary

#: 属性別クロス集計の選択肢に出す表。コンセプト × 設問のものだけに絞る。
#: コンセプト比較表とパネル構成表は選ばせない（`docs/issues/20260805002.md`）。
CROSSTAB_PREFIX = "crosstab_"

ui = context.ui_config()

page_header("結果閲覧", "完了した調査の集計結果を見て、データをダウンロードします。")

missing = context.missing_for_results()
if missing:
    context.show_missing(missing)
    footer()
    st.stop()

storage = context.storage()

# --------------------------------------------------------------------------- #
# 1. 調査を選ぶ
# --------------------------------------------------------------------------- #

try:
    runs = context.runs(storage, ui.survey_type)
except Exception as exc:  # noqa: BLE001
    st.error(f"調査一覧を取得できませんでした: {exc}")
    footer()
    st.stop()

if not runs:
    st.info(
        "完了した調査がまだありません。"
        "調査設計ページで調査を開始すると、完了後にここへ出てきます。"
    )
    footer()
    st.stop()

def _run_label(run: dict) -> str:
    """一覧の表示名。**`survey_id` を必ず添える。**

    調査名は重複しうるので、名前と完了時刻だけでは同名の別実行を見分けられない。
    調査設計ページが投入時に出す ID と突き合わせられるようにする。
    """
    name = run["survey_name"] or run["survey_id"]
    finished = f"{run['finished_at']:%Y-%m-%d %H:%M}" if run.get("finished_at") else "完了時刻不明"
    return f"{name}（{finished} / {run['survey_id']}）"


labels = {run["survey_id"]: _run_label(run) for run in runs}
survey_id = st.selectbox(
    "調査", options=list(labels), format_func=lambda sid: labels[sid]
)
selected = next(run for run in runs if run["survey_id"] == survey_id)

# 実行セッション数・成功・失敗・完了日時は出さない。読み手にとって何の数字か
# 分からないまま場所を取っていた（`docs/issues/20260805004.md`）。この時点で見えるのは
# 調査の選択と「データを取得」だけにする。
#
# **中断の警告だけは残す。** データが欠けているサインなので、黙って消すと
# 不完全な集計を完全なものとして読んでしまう。
if selected.get("aborted_reason"):
    st.warning(f"実行は途中で停止しています: {selected['aborted_reason']}")

# --------------------------------------------------------------------------- #
# 2. データを取得する
# --------------------------------------------------------------------------- #

# ローデータも同じ押下で取る。ダウンロードをボタン1つにするため、押した時点で
# 中身が揃っている必要がある（`st.download_button` は1ファイルしか返せない）。
if st.button("データを取得", type="primary"):
    with st.spinner("SQL Warehouse から取得しています…"):
        # 取得は必ず `context.query()` を通す。接続は全利用者で1本を共有しており
        # （`@st.cache_resource`）、同時に使うとリクエストが混ざる。
        survey = context.query(warehouse.fetch_survey, storage, survey_id)
        st.session_state["result"] = context.query(warehouse.build_result, storage, survey)
        st.session_state["raw"] = context.query(
            warehouse.fetch_responses_raw, storage, survey
        )
        st.session_state["survey"] = survey
        st.session_state["result_for"] = survey_id

if st.session_state.get("result_for") != survey_id:
    st.info("「データを取得」を押すと、この調査の集計結果を表示します。")
    footer()
    st.stop()

survey = st.session_state["survey"]
result = st.session_state["result"]
raw_columns, raw_rows = st.session_state["raw"]

# 今どの調査を見ているのかと、その調査が誰を対象にしたのかを必ず出す。
# これが無いと、別の調査の集計表を自分の調査だと思って読んでしまう。
st.caption(f"survey_id: `{survey.survey_id}` / 対象: {target_summary(survey)}")

for note in result.notes:
    st.warning(note)


def _show(table) -> None:
    st.dataframe(
        [dict(zip(table.columns, row, strict=True)) for row in table.rows],
        use_container_width=True,
        hide_index=True,
    )
    for note in table.notes:
        st.caption(note)


# --------------------------------------------------------------------------- #
# 3. 設問ごとのクロス集計表（軸＝コンセプト、セグメント＝全体）
# --------------------------------------------------------------------------- #

# 設問の並びは調査定義のまま。既定では購入意向 → 新規性の順に出る。
# 同じ問いは slot ごとに別の設問へ展開されているので、measure で畳んでから並べる
# （畳まないと「購入意向」の表がコンセプトの数だけ並ぶ）。
for question in tabulated_measures(survey):
    st.subheader(question.text)
    _show(
        concept_axis_table(
            survey,
            result.crosstabs,
            question.measure_key,
            key=f"concept_axis_{question.measure_key}",
            title=question.text,
            segments=(segment_axes.TOTAL,),
        )
    )

# --------------------------------------------------------------------------- #
# 4. 属性別のクロス集計表
# --------------------------------------------------------------------------- #

st.subheader("属性別のクロス集計表")
st.caption("コンセプト間の相対比較として読んでください。")

crosstabs = [table for table in result.tables if table.key.startswith(CROSSTAB_PREFIX)]
if not crosstabs:
    st.info("集計対象の回答がありません。")
else:
    names = {table.key: table.title for table in crosstabs}
    chosen = st.selectbox("表", options=list(names), format_func=lambda key: names[key])
    _show(next(table for table in crosstabs if table.key == chosen))

# --------------------------------------------------------------------------- #
# 5. ダウンロード
# --------------------------------------------------------------------------- #

st.subheader("ダウンロード")

workbook_table = stacked_crosstab_table(
    survey,
    result.crosstabs,
    key="crosstab",
    title="クロス集計表（コンセプト × セグメント）",
)

st.download_button(
    "集計表（Excel）とローデータ",
    data=context.workbook_bytes(
        survey_id,
        survey,
        workbook_table,
        _notes=result.notes,
        _raw_columns=raw_columns,
        _raw_rows=raw_rows,
    ),
    file_name=f"{survey_id}_report.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
st.caption("概要・集計表・ローデータを1つの Excel ファイル（シート3枚）にまとめています。")

footer()
