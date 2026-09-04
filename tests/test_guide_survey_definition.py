"""書き方ガイド（`docs/GUIDE_SURVEY_DEFINITION.md`）の実例が、実際に読めて通ること。

**ガイドは腐る。** 仕様を変えたときに本文だけ古いまま残ると、書き写した人が最初に踏む。
`examples/survey_sample.yaml` が「読めることしか見ていなかったせいで、割り付けの合計が
`panel.size` と一致しないまま放置されていた」のと同じ轍を踏まないための網。

対象は完全な調査定義（トップレベルに `survey:` を持つブロック）と、`screening` /
`design` だけの断片。断片は最小の定義に差し込んで検証する——**方式ごとに書けるものが
違う**箇所（`ask` に `conditions` を書くと止まる 等）がガイドで間違っていても、
完全な定義しか見ていないと気づけないため。
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.validate import validate_static

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "GUIDE_SURVEY_DEFINITION.md"

#: ```yaml … ``` で囲まれたブロック。
_FENCE = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)


#: 断片として書いてよいトップレベルの節。最小の定義に差し込んで検証する。
#:
#: 完全な定義しか見ないと、`screening` のように**方式ごとに書けるものが違う**箇所
#: （`ask` に `conditions` を書くと止まる 等）がガイドで間違っていても気づけない。
_MERGEABLE = ("screening", "design")


def _blocks() -> list[tuple[int, dict]]:
    """ガイド中の YAML ブロックを、行番号つきで拾う。

    行番号を持たせているのは、落ちたときにガイドのどこを直せばよいかを出すため。
    """
    text = GUIDE.read_text(encoding="utf-8")
    found = []
    for match in _FENCE.finditer(text):
        parsed = yaml.safe_load(match.group(1))
        if isinstance(parsed, dict):
            found.append((text[: match.start()].count("\n") + 1, parsed))
    return found


def _cases() -> list[tuple[int, dict]]:
    """検証にかける調査定義。完全なものはそのまま、断片は最小の定義に差し込む。"""
    blocks = _blocks()
    minimal = next(block for _, block in blocks if "survey" in block)

    cases = []
    for line, block in blocks:
        if "survey" in block:
            cases.append((line, block))
        elif set(block) <= set(_MERGEABLE):
            cases.append((line, {**copy.deepcopy(minimal), **block}))
    return cases


DEFINITIONS = _cases()


def test_the_guide_yields_cases_to_check():
    """拾えていること自体を確かめる。

    フェンスの書式を変えて1件も拾えなくなると、下のテストが**空振りして緑になる**。
    完全な定義と断片の両方が拾えている数を下限にしている。
    """
    assert len(DEFINITIONS) >= 5


@pytest.mark.parametrize(
    ("line", "definition"), DEFINITIONS, ids=[f"L{line}" for line, _ in DEFINITIONS]
)
def test_every_definition_in_the_guide_is_valid(line, definition):
    """ガイドに載っている調査定義は、そのまま書き写して `validate` を通ること。"""
    report = validate_static(survey_from_dict(definition))
    assert report.ok, (
        f"{GUIDE.name} の {line} 行目から始まる例が検証を通らない: "
        f"{[str(issue) for issue in report.errors]}"
    )


def test_the_guide_does_not_mention_the_discontinued_memory_setting():
    """廃止した `design.memory` をガイドが勧めていないこと。

    移行の案内として名前を出すのは可（`remember` への移行に必ず触れる）。
    """
    text = GUIDE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "design.memory" in line:
            assert "remember" in line, f"移行先に触れずに design.memory を書いている: {line}"
