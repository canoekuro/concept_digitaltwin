"""集計軸の解決（`SPEC_PHASE1.md` §7.1 の `output.segments`）。

`output.segments` に書けるのは次の3種類。

- `total` … 全体。1行だけの軸
- 属性列そのもの … `sex` / `age_band_10` / `cell_id` など
- 合成軸 … `sex_x_age_band_10` のように `_x_` で属性列をつなぐ

このモジュールは **pyspark に依存しない**。軸の妥当性検証を Spark 抜きでテストし、
`validate` からも同じ関数で検査できるようにするため。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from persona_sim.errors import SurveyDefinitionError

#: 全体を表す軸。
TOTAL = "total"

#: 合成軸の区切り。
COMPOSITE_SEPARATOR = "_x_"

#: `personas_base`（§2.1）のうち集計軸に使える列。ナラティブ列は軸にならない。
PERSONA_AXES: tuple[str, ...] = (
    "sex",
    "age_band_5",
    "age_band_10",
    "prefecture",
    "region",
    "area",
    "marital_status",
    "education_level",
    "occupation_industry",
    "occupation_scale",
    "occupation_role",
    "employment_status",
)

#: `panels`（§2.2）側の列。
PANEL_AXES: tuple[str, ...] = ("cell_id",)

#: 軸に使える属性列すべて。
ATTRIBUTE_AXES: tuple[str, ...] = (*PERSONA_AXES, *PANEL_AXES)

#: 合成軸の値をつなぐ区切り（表示用）。
VALUE_SEPARATOR = " × "

#: 値が欠けているときの表示。
MISSING_VALUE = "(不明)"

#: 全体行の表示名。
TOTAL_LABEL = "全体"


def parts(segment: str) -> tuple[str, ...]:
    """軸を構成する属性列に分解する。`total` は空タプル。"""
    if segment == TOTAL:
        return ()
    return tuple(segment.split(COMPOSITE_SEPARATOR))


def unknown_axes(segment: str) -> tuple[str, ...]:
    """その軸に含まれる、集計に使えない属性列。妥当なら空タプル。"""
    return tuple(part for part in parts(segment) if part not in ATTRIBUTE_AXES)


def validate_segments(segments: Sequence[str]) -> None:
    """`output.segments` を検証する。使えない軸があれば停止する。

    黙って無視すると、綴り違いの軸がそのまま消えて「指定したのに出ていない」ことに
    実行後まで気づけない。
    """
    for segment in segments:
        unknown = unknown_axes(segment)
        if unknown:
            available = ", ".join([TOTAL, *ATTRIBUTE_AXES])
            raise SurveyDefinitionError(
                f"output.segments の '{segment}' に使えない軸がある: {', '.join(unknown)}。"
                f" 指定できるのは {available}、および `_x_` でつないだ組み合わせ"
                f"（例: sex{COMPOSITE_SEPARATOR}age_band_10）"
            )


def required_columns(segments: Iterable[str]) -> tuple[str, ...]:
    """指定された軸を作るのに要る属性列（重複を除き、定義順を保つ）。"""
    columns: dict[str, None] = {}
    for segment in segments:
        for part in parts(segment):
            columns[part] = None
    return tuple(columns)


def value_of(segment: str, attributes: Mapping[str, object]) -> str:
    """1レコードの属性から、その軸での値（表示名）を作る。"""
    if segment == TOTAL:
        return TOTAL_LABEL
    values = []
    for part in parts(segment):
        value = attributes.get(part)
        values.append(MISSING_VALUE if value is None or value == "" else str(value))
    return VALUE_SEPARATOR.join(values)


def label(segment: str) -> str:
    """軸そのものの表示名。"""
    return TOTAL_LABEL if segment == TOTAL else segment
