"""国勢調査の人口構成比（`docs/SPEC_UI.md` §5）。

性別・5歳階級の人口を持ち、指定された年齢範囲で**再標準化**した比率を返す。
整数人数への変換はここでは行わない。比率を `quotas.mode: proportion` に載せ、
`persona_sim.panel.quotas.allocate_cell_sizes()` の最大剰余法に委ねる（§4.1）。

参照テーブルはパッケージ同梱の CSV。**出典・年次は `persona_sim/data/README.md`**。
リポジトリに CSV が無い場合は `None` を返し、UI 側で国勢調査パターンを選べなくする。
数値を持たないまま「人口構成比で割り付けた」と表示する方が害が大きい。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from persona_sim.uiconfig.schema import UIConfigError

#: 同梱の参照テーブル。列は sex / age_min / age_max / population。
CENSUS_PATH = Path(__file__).resolve().parent.parent / "data" / "census_population_by_sex_age5.csv"

#: 参照テーブルの階級幅（歳）。10歳刻みはこれを2つずつ束ねて作る。
SOURCE_BAND = 5


@dataclass(frozen=True)
class CensusRow:
    sex: str
    age_min: int
    age_max: int
    population: int


def load_census(path: Path | None = None) -> tuple[CensusRow, ...] | None:
    """参照テーブルを読む。無ければ `None`（＝国勢調査割り付けは使えない）。"""
    target = path or CENSUS_PATH
    if not target.exists():
        return None

    rows = []
    with target.open(encoding="utf-8-sig", newline="") as handle:
        for line_no, record in enumerate(csv.DictReader(handle), start=2):
            missing = [k for k in ("sex", "age_min", "age_max", "population") if not record.get(k)]
            if missing:
                raise UIConfigError(f"{target}:{line_no}: {', '.join(missing)} が空")
            rows.append(
                CensusRow(
                    sex=record["sex"].strip(),
                    age_min=int(record["age_min"]),
                    age_max=int(record["age_max"]),
                    population=int(record["population"]),
                )
            )
    if not rows:
        return None
    return tuple(rows)


def ratios(
    rows: Sequence[CensusRow], sex_ranges: Mapping[str, tuple[int, int]], band: int
) -> dict[tuple[str, int, int], float]:
    """指定範囲・指定刻みでの人口構成比。合計が 1.0 になるよう再標準化する。

    `sex_ranges` は対象にする性別だけをキーに持つ（値は `(age_min, age_max)`）。
    キーは `(性別, 下限年齢, 上限年齢)`。範囲外の階級は落としてから割り直すので、
    「20〜69歳の中での構成比」になる（全年齢に対する比率ではない）。性別ごとに
    範囲が違っても、全性別・全セルを合わせた合計で再標準化する。
    """
    if band % SOURCE_BAND:
        raise UIConfigError(
            f"刻み幅 {band} 歳は参照テーブル（{SOURCE_BAND}歳階級）から作れない"
        )

    populations: dict[tuple[str, int, int], int] = {}
    for sex, (age_min, age_max) in sex_ranges.items():
        for lower in range(age_min, age_max + 1, band):
            upper = lower + band - 1
            total = sum(
                row.population
                for row in rows
                if row.sex == sex and lower <= row.age_min and row.age_max <= upper
            )
            if total <= 0:
                raise UIConfigError(
                    f"国勢調査データに {sex} {lower}〜{upper}歳 の人口が無い。"
                    "対象年齢の範囲が参照テーブルの範囲を超えていないか確認すること"
                )
            populations[(sex, lower, upper)] = total

    grand_total = sum(populations.values())
    return {key: value / grand_total for key, value in populations.items()}
