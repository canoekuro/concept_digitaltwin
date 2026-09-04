"""画面の共有部品。

**ロジックは置かない。** 調査定義の組み立ても集計も `persona_sim` 側にあり、
テストできる場所に置いてある。ここにあるのは環境変数の解決・接続のキャッシュ・
表示の組み立てだけ。

Streamlit は起動スクリプトのディレクトリ（`app/`）を `sys.path` に入れるので、
各ページからは `from lib import ...` で届く。`persona_sim` はその外にあるので、
ここでリポジトリ直下も通す。Databricks Apps でもソースコードパスがリポジトリ直下
（`app.yaml` の場所）なので、同じ経路でリポジトリ内の `persona_sim` を直接 import する。
インストール済みの `persona_sim` があればそちらが優先される。
"""

from __future__ import annotations

import sys
from importlib.util import find_spec
from pathlib import Path

if find_spec("persona_sim") is None:  # pragma: no cover - ローカル起動時のみ
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
