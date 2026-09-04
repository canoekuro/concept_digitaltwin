"""コンセプト調査 Web UI のエントリ（`docs/SPEC_UI.md`）。

ページは `st.navigation` で登録する。`pages/` の自動検出は使わない
（両者は排他で、混ぜるとアプリを再起動するまで `pages/` が無効になる）。

自動検出だとサイドバーの表示名がファイル名になってしまう。ファイル名は英語、
画面の文言は日本語という規約（`.agents/rules/language-strategies.md`）を両立させるため、
表示名を明示できる `st.Page` を使う。
"""

from __future__ import annotations

import streamlit as st

from lib.layout import inject_css

st.set_page_config(page_title="コンセプト調査", page_icon="📋", layout="wide")
inject_css()


navigation = st.navigation(
    [
        st.Page("views/survey_design.py", title="調査設計", default=True),
        st.Page("views/results.py", title="結果閲覧"),
    ]
)
navigation.run()
