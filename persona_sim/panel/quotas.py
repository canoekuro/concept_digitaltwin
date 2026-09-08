"""割り付けの解決（`SPEC.md` §4.1, §4.3）。

- 属性条件 → Spark の述語への変換
- `proportion` モードの整数配分（最大剰余法）
- セル別ウェイトの算出

整数配分とウェイトは pyspark を使わない純関数として置き、単体テストできるようにする。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_sim.panel.schema import PersonaFilter, QuotaMode, SurveyDefinition

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import Column


def allocate_cell_sizes(survey: SurveyDefinition) -> dict[str, int]:
    """各セルの必要人数を決める。

    `count` モードは指定値をそのまま使う。`proportion` モードは最大剰余法で
    整数に配分し、合計が `panel.size` にちょうど一致するようにする。
    端数の取り合いは「剰余の大きい順、同点は cell_id 昇順」で決めるため決定論的。
    """
    quotas = survey.panel.quotas
    if quotas.mode is QuotaMode.COUNT:
        return {cell.cell_id: int(cell.n or 0) for cell in quotas.cells}

    size = survey.panel.size
    exact = {cell.cell_id: (cell.proportion or 0.0) * size for cell in quotas.cells}
    allocated = {cell_id: int(value) for cell_id, value in exact.items()}
    # **負にしない。** 比率の合計が1を超えると `remaining` が負になり、
    # 下の `order[:remaining]` が「末尾 n 個を除く全部」を返して**全セルに +1** される
    # ——「余りを配る」処理が「余分に配る」処理に反転する。
    # 合計1.0 は `validate._check_quotas()` が検証するが、`warehouse.fetch_survey()`
    # （`runs.metadata_json` からの復元）は `validate_static()` を通さないので到達しうる。
    # 静かに誤った人数を出すより、配らないほうがまだ読み取れる。
    remaining = max(0, size - sum(allocated.values()))

    # 剰余の大きい順に1人ずつ配る。同点は cell_id 昇順にして実行ごとのぶれを無くす。
    order = sorted(exact, key=lambda cell_id: (-(exact[cell_id] - allocated[cell_id]), cell_id))
    for cell_id in order[:remaining]:
        allocated[cell_id] += 1
    return allocated


def cell_weights(survey: SurveyDefinition, achieved: dict[str, int]) -> dict[str, float]:
    """セル別ウェイト（§4.3）。

    割り付けどおりに抽出できていれば 1.0。構成が崩れた場合のみ
    `目標比率 / 実比率` を返す。実績が 0 のセルは 0.0（母数が無いので按分できない）。
    """
    requested = allocate_cell_sizes(survey)
    quotas = survey.panel.quotas
    total_achieved = sum(achieved.values())
    if total_achieved == 0:
        return {cell_id: 0.0 for cell_id in requested}

    weights: dict[str, float] = {}
    for cell in quotas.cells:
        cell_id = cell.cell_id
        got = achieved.get(cell_id, 0)
        if got == 0:
            weights[cell_id] = 0.0
            continue
        if quotas.mode is QuotaMode.PROPORTION:
            target_ratio = cell.proportion or 0.0
        else:
            target_ratio = requested[cell_id] / survey.panel.size
        actual_ratio = got / total_achieved
        weights[cell_id] = target_ratio / actual_ratio
    return weights


#: 抽出に必要な列（キーの `uuid` ＋ `filter_condition` が参照する属性列）。
#: `personas_base` は長文の生成列を多数持つので、抽出ではここまで絞ってから読む。
#: **`filter_condition` に列を足したらここにも足すこと。** 欠けると抽出時に落ちる
#: （`tests/test_panel_spark.py::test_selection_columns_cover_every_filter_field` で検出する）。
SELECTION_COLUMNS: tuple[str, ...] = (
    "uuid",
    "sex",
    "age",
    "prefecture",
    "region",
    "area",
    "marital_status",
    "education_level",
)


def filter_condition(persona_filter: PersonaFilter) -> Column | None:
    """属性条件を Spark の述語に変換する。条件が無ければ None。"""
    from pyspark.sql import functions as F

    conditions: list[Column] = []
    if persona_filter.sex is not None:
        conditions.append(F.col("sex") == F.lit(persona_filter.sex))
    if persona_filter.age_min is not None:
        conditions.append(F.col("age") >= F.lit(persona_filter.age_min))
    if persona_filter.age_max is not None:
        conditions.append(F.col("age") <= F.lit(persona_filter.age_max))
    for column, values in (
        ("prefecture", persona_filter.prefecture_in),
        ("region", persona_filter.region_in),
        ("area", persona_filter.area_in),
        ("marital_status", persona_filter.marital_status_in),
        ("education_level", persona_filter.education_level_in),
    ):
        if values:
            conditions.append(F.col(column).isin(list(values)))

    if not conditions:
        return None
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = combined & condition
    return combined


def combine_conditions(*conditions: Column | None) -> Column | None:
    """None を無視して AND で結合する。"""
    present = [c for c in conditions if c is not None]
    if not present:
        return None
    combined = present[0]
    for condition in present[1:]:
        combined = combined & condition
    return combined
