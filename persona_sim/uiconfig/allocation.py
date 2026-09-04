"""割り付けセルの生成（`docs/SPEC_UI.md` §5）。

画面の「対象年齢」「N数」「割り付けパターン」から、調査定義の `panel.quotas` を作る。

**整数配分をここに書かない。** 3パターンとも `mode: proportion` で比率を渡し、
`persona_sim.panel.quotas.allocate_cell_sizes()` の最大剰余法に委ねる（§4.1）。
UI 側で丸めると、CLI 経由と UI 経由で人数が食い違いうる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from persona_sim.uiconfig.census import CensusRow, ratios
from persona_sim.uiconfig.schema import AllocationPattern, UIConfigError

#: `cell_id` に使う性別の記号。
_SEX_CODE = {"男": "M", "女": "F"}


def age_bands(age_min: int, age_max: int, band: int) -> tuple[tuple[int, int], ...]:
    """年齢範囲を刻み幅で区切る。割り切れなければ停止する。

    端数のセル（例: 20〜69歳を10歳刻みにしたときの最後の1年）を黙って作ると、
    そのセルだけ母集団が薄くなり、集計時に他セルと比べられなくなる。
    """
    if age_min < 0 or age_max < age_min:
        raise UIConfigError(f"対象年齢の範囲が不正: {age_min}〜{age_max}")

    span = age_max - age_min + 1
    if span % band:
        raise UIConfigError(
            f"対象年齢 {age_min}〜{age_max}歳（{span}年分）は {band} 歳刻みで割り切れない。"
            f"上限を {age_max - span % band} 歳にするか、刻み幅を変えること"
        )
    return tuple(
        (lower, lower + band - 1) for lower in range(age_min, age_max + 1, band)
    )


def build_quotas(
    pattern: AllocationPattern,
    sex_ranges: Mapping[str, tuple[int, int]],
    census: Sequence[CensusRow] | None = None,
) -> dict[str, Any]:
    """調査定義の `panel.quotas`（`mode: proportion`）を作る。

    `sex_ranges` は対象にする性別だけをキーに持つ（値は `(age_min, age_max)`）。
    性別ごとに違う年齢範囲を指定でき、キーに無い性別は調査対象から除外される。

    均等パターンは全セルに等しい比率を、国勢調査パターンは再標準化した人口構成比を割り当てる。
    どちらも合計が 1.0 になる。
    """
    if not sex_ranges:
        raise UIConfigError("対象の性別を1つ以上選ぶこと")

    bands_by_sex = {
        sex: age_bands(age_min, age_max, pattern.band)
        for sex, (age_min, age_max) in sex_ranges.items()
    }

    if pattern.census:
        if not census:
            raise UIConfigError(
                f"割り付けパターン {pattern.id!r} は国勢調査データを要求するが、"
                "参照テーブルが無い（persona_sim/data/README.md を参照）"
            )
        proportions = ratios(census, sex_ranges, pattern.band)
    else:
        total_cells = sum(len(bands) for bands in bands_by_sex.values())
        share = 1.0 / total_cells
        proportions = {
            (sex, lower, upper): share
            for sex, bands in bands_by_sex.items()
            for lower, upper in bands
        }

    cells = [
        {
            "cell_id": cell_id(sex, lower, upper),
            "sex": sex,
            "age_min": lower,
            "age_max": upper,
            "proportion": proportions[(sex, lower, upper)],
        }
        for sex, bands in bands_by_sex.items()
        for lower, upper in bands
    ]
    return {"mode": "proportion", "cells": _normalized(cells)}


def cell_id(sex: str, age_min: int, age_max: int) -> str:
    return f"{_SEX_CODE.get(sex, sex)}_{age_min}_{age_max}"


def _normalized(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """比率の合計を 1.0 ちょうどに寄せる。

    浮動小数の足し算で 1.0 から僅かにずれると `validate_static` が停止する。
    ずれを最後のセルに寄せるだけで、割り付けの意味は変わらない（差は 1e-15 規模）。
    """
    total = sum(cell["proportion"] for cell in cells)
    if cells and total != 1.0:
        cells[-1]["proportion"] += 1.0 - total
    return cells
