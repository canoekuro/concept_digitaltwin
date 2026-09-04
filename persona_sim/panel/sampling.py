"""層化抽出（`SPEC_PHASE1.md` §4.1）。

**乱数によるサンプリングを使わない。** `DataFrame.sample(seed=…)` は
パーティション数や並列度で結果が変わり、「seed と各バージョンが揃えば同一結果が
再現できること」という不変条件を満たせない。代わりに

```
rank_key = sha2(uuid | seed | cell_id, 256)
```

の昇順で先頭から必要数を取る。実行環境に依存せず同じ集合が得られる。

セル条件が重なると同一ペルソナが複数セルに入りうるため、調査定義の記述順に処理し、
確定済みの uuid を anti-join で除外する。これは §5.0（同一ペルソナは同一コンセプトに
1回しか回答しない）を守るための担保であり、設定で無効化できない。

**抽出コストはセル数に比例させる。** 各セルの確定行は driver 側に取り出し、
除外集合も DataFrame の連鎖ではなくそこから作り直す。前セルの DataFrame を参照すると、
その計算木が後続セルに入れ子で積み上がり、コストがセル数に対して指数的に増える
（実測でセル数 2/4/6/8 に対し 12/32/106/396 ステージ。`性別 × 10歳刻み` の10セルで顕在化した）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from persona_sim.errors import InsufficientCandidatesError
from persona_sim.panel.quotas import (
    SELECTION_COLUMNS,
    allocate_cell_sizes,
    cell_weights,
    combine_conditions,
    filter_condition,
)
from persona_sim.panel.schema import SurveyDefinition

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

#: パネルの役割（`SPEC_PHASE1.md` §2.2）。
ROLE_CANDIDATE = "candidate"
ROLE_MAIN = "main"
ROLE_RESERVE = "reserve"
ROLE_SCREENED_OUT = "screened_out"

#: `select_members` が返す DataFrame のスキーマ（`role` は後から一律に足す）。
MEMBER_SCHEMA = "persona_uuid string, cell_id string, cell_rank int"


@dataclass(frozen=True)
class CellSpec:
    """抽出する1セル。`select_by_cells` への入力。

    調査定義の型を持ち込まないのは、**セルの決め方（割り付け・スクリーニングの
    オーバーサンプル）は呼ぶ側ごとに違うが、選び方は同じ**でなければ「同じ seed なら
    同じ人が出る」という再現性の保証が経路ごとに分かれてしまうため。
    """

    cell_id: str
    #: このセルの候補を絞る述語。全件が候補なら None。
    condition: object | None
    n: int


@dataclass
class SelectionReport:
    """抽出の実績。`runs` への記録と `validate` の警告に使う（§9）。"""

    requested: dict[str, int] = field(default_factory=dict)
    achieved: dict[str, int] = field(default_factory=dict)
    raw_candidates: dict[str, int] = field(default_factory=dict)
    overlap_excluded: dict[str, int] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def total_achieved(self) -> int:
        return sum(self.achieved.values())


def select_members(
    spark: SparkSession,
    personas: DataFrame,
    survey: SurveyDefinition,
    *,
    cell_sizes: dict[str, int] | None = None,
    role: str = ROLE_MAIN,
) -> tuple[DataFrame, SelectionReport]:
    """割り付けどおりにペルソナを選抜する。

    返す DataFrame は `persona_uuid` / `cell_id` / `cell_rank` / `role` を持つ。
    `cell_rank` はセル内の抽出順（0 始まり）で、コンセプト割り当ての起点になる。

    `cell_sizes` を渡すと割り付け数の代わりに使う（スクリーニングのオーバーサンプル用）。
    抽出はハッシュ順なので、**数を増やすことは同じ並びの先を伸ばすこと**と同じになり、
    既に選ばれていた人は同じ `cell_rank` のまま残る（§4.2 の再試行がこれを使う）。

    候補が足りないセルがあれば `InsufficientCandidatesError`（E1）を送出する。
    1つ目で止めず、全セルを調べてからまとめて報告する。
    """
    from pyspark.sql import functions as F

    requested = cell_sizes or allocate_cell_sizes(survey)
    global_condition = filter_condition(survey.panel.filters)

    cells = [
        CellSpec(
            cell_id=cell.cell_id,
            condition=combine_conditions(global_condition, filter_condition(cell.conditions)),
            n=requested[cell.cell_id],
        )
        for cell in survey.panel.quotas.cells
    ]

    rows, report = select_by_cells(spark, personas, seed=survey.panel.seed, cells=cells)

    # 確定行は driver 側にあるので、union を重ねずに1回で組み直す。
    # パネルは高々 `panel.size × oversample_factor`（数百行）で、`finalize_panel` も同じ規模を
    # driver に載せている。
    members = spark.createDataFrame(rows, MEMBER_SCHEMA).withColumn("role", F.lit(role))

    report.weights = cell_weights(survey, report.achieved)
    return members, report


def select_by_cells(
    spark: SparkSession,
    personas: DataFrame,
    *,
    seed: int,
    cells: Sequence[CellSpec],
) -> tuple[list[tuple[str, str, int]], SelectionReport]:
    """ハッシュ順抽出の中核。`(persona_uuid, cell_id, cell_rank)` の並びと実績を返す。

    調査定義の型に依存しないので、本調査のパネル構築（`select_members`）と
    スクリーニングのオーバーサンプル抽出が同じ経路を通る。

    `cells` の**並び順に意味がある**。セル条件が重なると同一ペルソナが複数セルに
    入りうるため、先に処理したセルの確定分を anti-join で除外していく
    （モジュール冒頭の説明を参照）。呼ぶ側は調査定義の記述順で渡すこと。

    候補が足りないセルがあれば `InsufficientCandidatesError`（E1）を送出する。
    """
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    conditions = {cell.cell_id: cell.condition for cell in cells}
    report = SelectionReport(requested={cell.cell_id: cell.n for cell in cells})
    # 全セルが同じ母集団を舐めるので、条件に使う列だけに絞って1回だけ読む。
    pool = personas.select(*SELECTION_COLUMNS)
    _pool_cached = False
    try:
        pool.cache()
        _pool_cached = True
    except Exception:
        pass  # serverless ではキャッシュ不可
    rows: list[tuple[str, str, int]] = []

    try:
        report.raw_candidates = _count_candidates(pool, conditions)
        _warn_on_overlap(pool, conditions, report)

        taken: list[str] = []  # 確定済み uuid。セル間の重複除外に使う
        for cell in cells:
            cell_id = cell.cell_id
            needed = cell.n
            condition = cell.condition

            candidates = pool if condition is None else pool.filter(condition)
            candidates = candidates.select(F.col("uuid").alias("persona_uuid"))
            if taken:
                # 除外集合は確定済み uuid から作り直す。前セルの DataFrame をそのまま繋ぐと、
                # その計算木が後続セルに入れ子で積み上がり、コストがセル数に対して指数的に増える。
                excluded = spark.createDataFrame(
                    [(persona_uuid,) for persona_uuid in taken], "persona_uuid string"
                )
                candidates = candidates.join(
                    F.broadcast(excluded), on="persona_uuid", how="left_anti"
                )

            ranked = candidates.withColumn(
                "_rank_key",
                F.sha2(
                    F.concat_ws("|", F.col("persona_uuid"), F.lit(str(seed)), F.lit(cell_id)), 256
                ),
            )
            # limit で TakeOrderedAndProject になるため、全件ソートを避けられる。
            # 同点は uuid で決着させて実行ごとのぶれを無くす（ハッシュ衝突は事実上起きないが念のため）。
            picked = ranked.orderBy("_rank_key", "persona_uuid").limit(needed)
            picked = picked.withColumn(
                "cell_rank",
                F.row_number().over(Window.orderBy("_rank_key", "persona_uuid")) - 1,
            ).select("persona_uuid", "cell_rank")

            # 順位は Spark 側で全順序から確定しているので、取り出す行の並びには依存しない。
            cell_rows = [
                (row["persona_uuid"], cell_id, int(row["cell_rank"])) for row in picked.collect()
            ]
            report.achieved[cell_id] = len(cell_rows)
            rows.extend(cell_rows)
            taken.extend(persona_uuid for persona_uuid, _, _ in cell_rows)

        _raise_if_short(report)
    finally:
        if _pool_cached:
            try:
                pool.unpersist()
            except Exception:
                pass

    return rows, report


def _count_candidates(personas: DataFrame, conditions: dict[str, object]) -> dict[str, int]:
    """全セルの候補数を1回のスキャンで数える。"""
    from pyspark.sql import functions as F

    if not conditions:
        return {}
    aggregations = [
        F.count(F.when(condition, F.lit(1))).alias(f"cell_{index}") if condition is not None
        else F.count(F.lit(1)).alias(f"cell_{index}")
        for index, condition in enumerate(conditions.values())
    ]
    row = personas.agg(*aggregations).collect()[0]
    return {cell_id: int(row[f"cell_{index}"]) for index, cell_id in enumerate(conditions)}


def _warn_on_overlap(
    personas: DataFrame, conditions: dict[str, object], report: SelectionReport
) -> None:
    """複数セルの条件を同時に満たすペルソナ数を数える（セル定義の重なりの検知）。"""
    from pyspark.sql import functions as F

    if len(conditions) < 2:
        return
    matched = None
    for condition in conditions.values():
        term = F.lit(1) if condition is None else F.when(condition, F.lit(1)).otherwise(F.lit(0))
        matched = term if matched is None else matched + term
    overlapping = int(personas.agg(F.count(F.when(matched > 1, F.lit(1)))).collect()[0][0])
    if overlapping:
        # どのセルで何件除外されたかまでは数えない（セル数だけスキャンが増えるため）。
        # 重なりがあること自体が定義ミスの兆候なので、総数だけ表面化させる。
        report.overlap_excluded["(複数セルに該当)"] = overlapping


def _raise_if_short(report: SelectionReport) -> None:
    shortfalls = [
        (cell_id, needed, report.achieved.get(cell_id, 0), report.raw_candidates.get(cell_id, 0))
        for cell_id, needed in report.requested.items()
        if report.achieved.get(cell_id, 0) < needed
    ]
    if not shortfalls:
        return
    lines = [
        f"  {cell_id}: 必要 {needed} / 確保 {achieved}（条件に合うペルソナ {raw}）"
        for cell_id, needed, achieved, raw in shortfalls
    ]
    raise InsufficientCandidatesError(
        "割り付けを満たすペルソナが足りない（セル間の重複除外を適用した後の数）:\n"
        + "\n".join(lines)
    )
