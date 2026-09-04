"""集計に使うデータの読み込み（`SPEC_PHASE1.md` §7）。

`responses` × `panels` × `personas_base` を結合し、算術の入力になる `Answer` を組み立てる。
**Spark を使うのはここだけ**で、集計そのものは `crosstab` の純関数が行う。

ドライバへ集める前提で書いている。フェーズ1の想定規模（1,000人 × 10コンセプト × 数問 ＝
数万行、§12 M7）では問題にならない。全件をそのまま出す `responses_raw.csv` だけは
件数が伸びうるので、`stream_responses_raw` が1行ずつ流す。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from persona_sim.aggregate import segments as segment_axes
from persona_sim.aggregate.crosstab import Answer, to_defined_codes
from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.panel.sampling import ROLE_MAIN
from persona_sim.panel.schema import QuestionType, SurveyDefinition
from persona_sim.run import flags as flag_names
from persona_sim.run.run import RESPONSES_SCHEMA_COLUMNS
from persona_sim.storage import delta
from persona_sim.storage.locator import PANELS, PERSONAS_BASE, RESPONSES, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

#: 自由回答テーブル（§7.3）に添えるペルソナの主要属性。
OPEN_END_ATTRIBUTES: tuple[str, ...] = ("sex", "age", "age_band_10", "prefecture", "cell_id")


def answers_from_rows(
    rows: Iterable[Mapping[str, Any]], survey: SurveyDefinition
) -> list[Answer]:
    """結合済みの行を集計の入力（`Answer`）に変換する。**Spark を使わない。**

    Spark で読んだ場合（`load_answers`）と SQL Warehouse 経由で読んだ場合
    （`persona_sim.storage.warehouse`）で、ここを共有する。読み方が違っても
    同じ数字になることを保証するのが目的で、変換を二重に実装しない。

    期待する行のキー: `persona_uuid` / `stimulus_id` / `question_id` / `answer_codes` /
    `options_order` / `weight` / `flags` / `answer_text` ＋ 集計軸に使う属性列。
    """
    attribute_columns = _attribute_columns(survey)
    questions = {question.id: question for question in survey.questions}

    answers: list[Answer] = []
    for row in rows:
        question = questions.get(row["question_id"])
        if question is None:
            # 調査定義から消えた設問の過去レコード。集計対象にしない。
            continue
        answers.append(
            Answer(
                persona_uuid=row["persona_uuid"],
                stimulus_id=row["stimulus_id"],
                question_id=row["question_id"],
                # 提示順の番号を定義順に戻す。飛ばすと、選択肢をシャッフルした設問で
                # 結果の意味が変わる（§2.3）。
                codes=to_defined_codes(row["answer_codes"], row["options_order"]),
                weight=float(row["weight"] if row["weight"] is not None else 1.0),
                flagged=_is_flagged(row["flags"]),
                attributes={name: row[name] for name in attribute_columns},
                number=_to_number(row["answer_text"])
                if question.type is QuestionType.NUMERIC
                else None,
                text=row["answer_text"],
            )
        )
    return answers


def load_answers(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> list[Answer]:
    """集計対象の回答を読み込む。

    本調査に進んだ `role=main` のペルソナだけを対象にする（`reserve` / `screened_out` /
    `candidate` は本調査を実行していない）。
    """
    joined = _joined(spark, survey, storage)
    return answers_from_rows((row.asDict() for row in joined.collect()), survey)


def load_open_ends(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> tuple[tuple[str, ...], list[list[Any]]]:
    """自由回答の長持ちテーブル（§7.3）。見出しと行を返す。"""
    from pyspark.sql import functions as F

    open_question_ids = [
        question.id for question in survey.questions if question.type is QuestionType.OPEN
    ]
    columns = (
        "survey_id",
        "persona_uuid",
        "stimulus_id",
        "question_id",
        "answer_text",
        *OPEN_END_ATTRIBUTES,
    )
    if not open_question_ids:
        return columns, []

    joined = _joined(spark, survey, storage, extra_attributes=OPEN_END_ATTRIBUTES).filter(
        F.col("question_id").isin(open_question_ids)
    )
    rows = [
        [row[name] if name != "survey_id" else survey.survey_id for name in columns]
        for row in joined.orderBy("persona_uuid", "stimulus_id", "question_id").collect()
    ]
    return columns, rows


def stream_responses_raw(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> tuple[tuple[str, ...], Iterator[list[Any]]]:
    """生データ全件（§7.4 `responses_raw.csv`）。件数が伸びうるので流しながら返す。"""
    from pyspark.sql import functions as F

    responses = (
        delta.read_table(spark, locator(RESPONSES, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .select(*RESPONSES_SCHEMA_COLUMNS)
        .orderBy("persona_uuid", "stimulus_id", "question_id")
    )

    def rows() -> Iterator[list[Any]]:
        for row in responses.toLocalIterator():
            yield [_scalar(row[name]) for name in RESPONSES_SCHEMA_COLUMNS]

    return RESPONSES_SCHEMA_COLUMNS, rows()


def load_panel_rows(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> list[dict[str, Any]]:
    """`panels` のうち、この調査の全ロールの行（構成表とインシデンス用）。"""
    from pyspark.sql import functions as F

    panels_locator = locator(PANELS, storage)
    if not delta.table_exists(spark, panels_locator):
        return []
    return [
        row.asDict()
        for row in delta.read_table(spark, panels_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .select("cell_id", "role", "weight")
        .collect()
    ]


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _joined(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    *,
    extra_attributes: Sequence[str] = (),
) -> DataFrame:
    """`responses` に `panels` のセル・ウェイトと `personas_base` の属性を付ける。"""
    from pyspark.sql import functions as F

    responses_locator = locator(RESPONSES, storage)
    if not delta.table_exists(spark, responses_locator):
        raise PersonaSimError(
            f"{responses_locator.describe()} が無い。先に `persona-sim run` を実行すること"
        )

    responses = delta.read_table(spark, responses_locator).filter(
        F.col("survey_id") == F.lit(survey.survey_id)
    )
    if not responses.take(1):
        raise PersonaSimError(
            f"survey_id={survey.survey_id} の回答が1件も無い。先に `persona-sim run` を実行すること"
        )

    panels = (
        delta.read_table(spark, locator(PANELS, storage))
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .filter(F.col("role") == F.lit(ROLE_MAIN))
        .select("persona_uuid", "cell_id", "weight")
    )

    wanted = {*_attribute_columns(survey), *extra_attributes}
    persona_columns = [name for name in wanted if name not in ("cell_id",)]
    personas = delta.read_table(spark, locator(PERSONAS_BASE, storage)).select(
        "uuid", *persona_columns
    )

    return (
        responses.join(panels, on="persona_uuid", how="inner")
        .join(personas, responses["persona_uuid"] == personas["uuid"], how="left")
        .drop("uuid")
    )


def _attribute_columns(survey: SurveyDefinition) -> tuple[str, ...]:
    """セグメント軸に要る属性列（`cell_id` は `panels` 側にある）。"""
    return segment_axes.required_columns(survey.output.segments)


def _is_flagged(flags: Sequence[str] | None) -> bool:
    return any(flag in flag_names.QUALITY_FLAGS for flag in (flags or ()))


def _to_number(text: str | None) -> float | None:
    """`numeric` 設問の値。カンマ区切りを外して読む。読めなければ None。"""
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _scalar(value: Any) -> Any:
    """CSV に書ける形へ。配列は `|` 区切りに潰す。"""
    if isinstance(value, (list, tuple)):
        return "|".join(str(item) for item in value)
    return value
