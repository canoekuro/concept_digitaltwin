"""画面の共通部品。

CC BY 4.0 の帰属表示は義務なので（`SPEC.md` §15.3）、全ページのフッタに出す。
文言は `persona_sim/__init__.py` の定数を使い、ここで書き直さない。
"""

from __future__ import annotations

import streamlit as st

from persona_sim import DATASET_ATTRIBUTION, OUTPUT_DISCLAIMER

#: 入力欄・ボタンの見た目だけを軽く整える（docs/issues/UIの修正.md）。
#: 色は `.streamlit/config.toml` の [theme] 側に任せ、ここでは枠線・角丸のみ扱う。
#: 複雑な指定はしない。
_CSS = """
<style>
input, textarea, select {
    border-radius: 8px !important;
    border: 1px solid #d2d2d7 !important;
}
.stButton > button {
    border-radius: 8px !important;
}
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def footer() -> None:
    st.divider()
    st.caption(OUTPUT_DISCLAIMER)
    st.caption(DATASET_ATTRIBUTION.replace("\n", " "))


def page_header(title: str, description: str) -> None:
    st.title(title)
    st.caption(description)
