"""調査定義から「誰に聞くのか」を1行で表す（`docs/SPEC_UI.md` §3.4, §4）。

画面に出す文字列だが、**組み立てを Streamlit 側に置かない。** 調査設計ページと
結果閲覧ページの両方が同じ文言を出す必要があり、片方だけ直すと「開始前に見た対象」と
「結果を見たときの対象」が食い違って読めてしまう。純関数にして単体テストで押さえる。

対象は `panel.filters` ではなく **`panel.quotas.cells` から読む**。複数性別を
選んだときの `filters` は年齢の envelope だけになり性別条件を持たない（§3.3）ので、
`filters` を見ると「性別の指定なし」に見えてしまう。厳密な対象はセル条件が持っている。
"""

from __future__ import annotations

from persona_sim.panel.schema import SurveyDefinition

#: 表示用の性別名。`personas_base.sex` の値（`男` / `女`）を画面の言い方に直す。
SEX_LABELS = {"男": "男性", "女": "女性"}

#: 性別の指定が無いセルの表示。
ANY_SEX_LABEL = "性別指定なし"

#: 年齢の指定が無いセルの表示。
ANY_AGE_LABEL = "年齢指定なし"


def target_summary(survey: SurveyDefinition) -> str:
    """割り付けセルから対象者を1行にする。

    例: `男性 20〜69歳（5セル・計 500名）`
        `男性 20〜39歳 / 女性 40〜69歳（6セル・計 500名）`
    """
    parts = [_range_label(sex, low, high) for sex, (low, high) in _ranges(survey).items()]
    target = " / ".join(parts) if parts else "（割り付けセルなし）"
    cells = len(survey.panel.quotas.cells)
    return f"{target}（{cells}セル・計 {survey.panel.size:,}名）"


def _ranges(survey: SurveyDefinition) -> dict[str, tuple[int | None, int | None]]:
    """性別ごとの年齢範囲。セルの定義順を保つ。

    同じ性別に複数のセルがあるので、下限の最小と上限の最大にまとめる。
    どちらかが未指定のセルがあれば、その側は未指定として扱う（狭く見せない）。
    """
    ranges: dict[str, tuple[int | None, int | None]] = {}
    for cell in survey.panel.quotas.cells:
        sex = cell.conditions.sex or ""
        low, high = cell.conditions.age_min, cell.conditions.age_max
        if sex not in ranges:
            ranges[sex] = (low, high)
            continue
        known_low, known_high = ranges[sex]
        ranges[sex] = (
            None if known_low is None or low is None else min(known_low, low),
            None if known_high is None or high is None else max(known_high, high),
        )
    return ranges


def _range_label(sex: str, low: int | None, high: int | None) -> str:
    name = SEX_LABELS.get(sex, sex) if sex else ANY_SEX_LABEL
    if low is None and high is None:
        return f"{name} {ANY_AGE_LABEL}"
    if low is None:
        return f"{name} 〜{high}歳"
    if high is None:
        return f"{name} {low}歳〜"
    return f"{name} {low}〜{high}歳"
