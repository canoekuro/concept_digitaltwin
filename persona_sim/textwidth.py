"""端末上の見た目の幅。

CLI の表（`cli.py`）と進捗表示（`run/progress.py`）の両方が桁を揃えるのに要る。
`len()` は文字数であって幅ではなく、日本語を混ぜた途端に列がずれる（進捗表示では
消し残りになる）ので、**幅を測る場所はここ1つに寄せる**。
"""

from __future__ import annotations

import unicodedata


def display_width(text: str) -> int:
    """端末上の見た目の幅。全角文字は2桁ぶん取る。

    `east_asian_width` が W（Wide）または F（Fullwidth）を返す文字を2桁と数える。
    A（Ambiguous）は端末の設定次第なので1桁のまま扱う——どちらに倒しても
    どこかの端末では外れるが、1桁側の外れ方（詰まる）のほうが害が小さい。
    """
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)
