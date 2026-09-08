"""回答生成の実行（`SPEC.md` §6）。

`panels` と `personas_base` を読み、セッションを組み立てて実行し、`responses` に
冪等に書き出す。中断しても同じコマンドで続きから再開できる（§6.4）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.llm.budget import output_budget_warning
from persona_sim.llm.client import LLMClient, RetryStats, retry_stats_of, retry_warning
from persona_sim.llm.registry import build_client
from persona_sim.panel.sampling import ROLE_MAIN
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.panel.screening import load_premises
from persona_sim.personas.load import load_personas
from persona_sim.run import flags as flag_names
from persona_sim.run.executor import ContinuousFailureDetector, ExecutionResult, execute
from persona_sim.run.memory import ask_order
from persona_sim.run.session import (
    OutputBudgetState,
    PromptSample,
    ResponseRecord,
    Session,
    SessionContext,
    StructuredOutputState,
    build_sessions,
    sample_prompts,
)
from persona_sim.storage import delta
from persona_sim.storage.locator import PANELS, PERSONAS_BASE, RESPONSES, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

#: `responses` の主キー（§2.3）。
RESPONSE_KEYS = ("survey_id", "persona_uuid", "stimulus_id", "question_id")

#: `responses` のスキーマ。型を固定して書き出しごとのぶれを無くす。
#:
#: `answer_codes` は**提示順**の番号空間（§2.3）。multi で選ばれた番号すべてを持ち、
#: single / scale では1要素、open / numeric では空になる。
RESPONSES_SCHEMA = (
    "survey_id string, persona_uuid string, stimulus_id string, question_id string, "
    "sequence int, answer_raw string, answer_codes array<int>, "
    "answer_text string, answer_reasoning string, options_order array<int>, "
    "flags array<string>, latency_ms int, "
    "input_tokens int, output_tokens int, attempt int, ts timestamp"
)

#: E3・E4 の閾値（§11）。
FLAG_WARNING_THRESHOLD = 0.05


@dataclass
class RunResult:
    survey_id: str
    started_at: datetime
    finished_at: datetime
    sessions_total: int = 0
    sessions_ok: int = 0
    sessions_failed: int = 0
    sessions_skipped: int = 0
    records_written: int = 0
    flag_counts: dict[str, int] = field(default_factory=dict)
    flag_rates: dict[str, float] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    warnings: list[str] = field(default_factory=list)
    aborted_reason: str | None = None
    structured_output_fell_back: bool = False
    #: 出力予算を引き上げた／使い切った回数（`model.max_tokens` を見直す材料）。
    output_budget_escalated: int = 0
    output_budget_exhausted: int = 0
    personas_version: int | None = None
    model_versions: tuple[str, ...] = ()
    client_description: str = ""
    #: 伝送層の再試行の実績（§6.5）。統計を出せないクライアントでは None。
    retry_stats: RetryStats | None = None
    #: 実際に送ったプロンプト1名ぶん（§9）。**その1名に聞いた全設問**を、聞いた順に持つ。
    #: セッションが1つも無ければ空。
    prompt_samples: tuple[PromptSample, ...] = ()

    @property
    def aborted(self) -> bool:
        return self.aborted_reason is not None


def run_survey(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    *,
    client: LLMClient | None = None,
    detector: ContinuousFailureDetector | None = None,
    progress=None,
) -> RunResult:
    """調査を実行して `responses` を埋める。

    既に完了しているセッションはスキップする。完了判定はセッション単位で行うので、
    途中まで進んだセッションは丸ごとやり直す（§6.4 の理由は `session` モジュール参照）。
    """
    started_at = datetime.now(UTC)

    panel_rows = _load_panel(spark, survey, storage)
    personas = load_personas(spark, survey, storage, [row["persona_uuid"] for row in panel_rows])

    sessions = build_sessions(survey, panel_rows)
    completed = _completed_keys(spark, survey, storage)
    pending = [s for s in sessions if not _session_completed(s, completed)]

    client = client or build_client(survey.model)
    structured_output = StructuredOutputState(mode=survey.model.structured_output)
    output_budget = OutputBudgetState()
    ctx = SessionContext(
        survey=survey,
        client=client,
        personas=personas,
        stimuli={stimulus.id: stimulus for stimulus in survey.stimuli},
        structured_output=structured_output,
        output_budget=output_budget,
        # スクリーナーの前提はペルソナカードに載せる。memory の設定によらず効かせるため（§4.2）。
        premises=load_premises(spark, survey, storage, list(personas)),
    )

    execution = execute(
        pending,
        ctx,
        concurrency=survey.model.concurrency,
        detector=detector,
        progress=progress,
    )

    responses_locator = locator(RESPONSES, storage)
    if execution.records:
        delta.merge_upsert(
            spark,
            _to_dataframe(spark, execution.records),
            responses_locator,
            RESPONSE_KEYS,
            # 列を増やしたリリースを、既にある `responses` の上に載せられるようにする
            # （`answer_reasoning` の追加）。付けないと、既存テーブルへの書き込みと
            # `_apply_straightline()` の select が列の欠落で落ちる。
            evolve_schema=True,
        )

    # straightline はペルソナが答えた設問全体を見ないと判定できないので、
    # 書き出しが済んでから、その調査の全レコードに対して付け直す。
    _apply_straightline(spark, survey, responses_locator)

    finished_at = datetime.now(UTC)
    result = RunResult(
        survey_id=survey.survey_id,
        started_at=started_at,
        finished_at=finished_at,
        sessions_total=len(sessions),
        sessions_ok=execution.sessions_ok,
        sessions_failed=execution.sessions_failed,
        sessions_skipped=len(sessions) - len(pending),
        records_written=len(execution.records),
        aborted_reason=execution.aborted_reason,
        structured_output_fell_back=structured_output.fell_back,
        output_budget_escalated=output_budget.escalated,
        output_budget_exhausted=output_budget.exhausted,
        personas_version=delta.table_version(spark, locator(PERSONAS_BASE, storage)),
        client_description=client.describe(),
        retry_stats=retry_stats_of(client),
    )
    # 回答を再生するので、`responses` への書き出しが済んでから組み直す。
    result.prompt_samples = _pick_prompt_samples(spark, survey, storage, sessions, ctx)
    _summarize(spark, survey, responses_locator, execution, result)
    return result


def _pick_prompt_samples(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    sessions: Sequence[Session],
    ctx: SessionContext,
) -> tuple[PromptSample, ...]:
    """記録に残す1名を決め、その人に聞いた**全設問**を組み直す（§9）。

    並び順に依存すると実行のたびに別のペルソナが出てしまうので、キーで整列して
    先頭を採る。同じ調査を何度流しても同じ1名が残る。

    そのペルソナのセッションは ask order（`slot` 昇順、同 slot 内は定義順）に並べる。
    セッションのキー順だと `stimulus_id` の辞書順になり、実際に聞いた順と食い違う。

    実行対象の有無によらず記録したいので、pending ではなく全セッションから選ぶ。
    """
    persona_uuid = _sample_persona(sessions, ctx)
    if persona_uuid is None:
        return ()

    position = {question.id: index for index, question in enumerate(ask_order(survey))}
    mine = sorted(
        (s for s in sessions if s.persona_uuid == persona_uuid and s.units),
        key=lambda session: position[session.units[0].question_id],
    )
    answers = _recorded_answers(spark, survey, storage, persona_uuid)
    return tuple(
        sample for session in mine for sample in sample_prompts(session, ctx, answers)
    )


def _sample_persona(sessions: Sequence[Session], ctx: SessionContext) -> str | None:
    """記録に残す1名。ペルソナが読み込まれていなければ次の候補へ送る。"""
    for session in sorted(sessions, key=lambda session: session.key):
        if session.units and session.persona_uuid in ctx.personas:
            return session.persona_uuid
    return None


def _recorded_answers(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig, persona_uuid: str
) -> dict[str, str]:
    """記録に残す1名の回答本文。設問IDごとに1件。

    記憶を持つ設問のプロンプトには先行設問の回答が入る。それはエンドポイントの
    出力なので純関数では組み直せず、書き出し済みの `responses` から引くしかない。
    再開実行（全セッションがスキップされる場合）でも前回の回答が読める。

    設問IDで引けるのは、**設問の `slot` が固定**で、同じ設問を別のコンセプトで
    2度聞くことがないため（`stimulus_for()`）。1ペルソナ1設問につき `responses` は1行になる。
    """
    from pyspark.sql import functions as F

    responses_locator = locator(RESPONSES, storage)
    if not delta.table_exists(spark, responses_locator):
        return {}

    rows = (
        delta.read_table(spark, responses_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .filter(F.col("persona_uuid") == F.lit(persona_uuid))
        .select("question_id", "answer_raw")
        .collect()
    )
    return {
        row["question_id"]: row["answer_raw"]
        for row in rows
        if row["answer_raw"] is not None
    }


# --------------------------------------------------------------------------- #
# 読み込み
# --------------------------------------------------------------------------- #


def _load_panel(spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig) -> list[dict]:
    from pyspark.sql import functions as F

    panels_locator = locator(PANELS, storage)
    if not delta.table_exists(spark, panels_locator):
        raise PersonaSimError(
            f"{panels_locator.describe()} が無い。先に `panel.build.build_panel()` を実行すること"
        )

    # 本調査に進むのは main だけ。reserve / screened_out / candidate は対象外。
    # ここで絞らないと、非通過者にまで本調査を実行してしまう。
    rows = (
        delta.read_table(spark, panels_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .filter(F.col("role") == F.lit(ROLE_MAIN))
        .select("persona_uuid", "assigned_stimuli")
        .orderBy("persona_uuid")
        .collect()
    )
    if not rows:
        hint = (
            "先に `panel.screening.screen_survey()` を実行すること（候補は抽出済みだが未判定）"
            if survey.screening is not None
            else "先に `panel.build.build_panel()` を実行すること"
        )
        raise PersonaSimError(f"survey_id={survey.survey_id} に role=main のパネルが1件も無い。{hint}")
    return [
        {"persona_uuid": row["persona_uuid"], "assigned_stimuli": list(row["assigned_stimuli"])}
        for row in rows
    ]


def _completed_keys(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> set[tuple[str, str, str]]:
    """完了済みの (persona_uuid, stimulus_id, question_id)。

    「完了」は、行が存在し、かつ `parse_error` が立っていないこと。パース失敗のまま
    残っている設問は、再実行の機会があるならやり直す。
    """
    from pyspark.sql import functions as F

    responses_locator = locator(RESPONSES, storage)
    if not delta.table_exists(spark, responses_locator):
        return set()

    rows = (
        delta.read_table(spark, responses_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .filter(~F.array_contains(F.col("flags"), F.lit(flag_names.PARSE_ERROR)))
        .select("persona_uuid", "stimulus_id", "question_id")
        .collect()
    )
    return {(row["persona_uuid"], row["stimulus_id"], row["question_id"]) for row in rows}


def _session_completed(session, completed: set[tuple[str, str, str]]) -> bool:
    return all(
        (unit.persona_uuid, unit.stimulus_id, unit.question_id) in completed
        for unit in session.units
    )


# --------------------------------------------------------------------------- #
# 書き出しと後処理
# --------------------------------------------------------------------------- #


def _to_dataframe(spark: SparkSession, records: list[ResponseRecord]) -> DataFrame:
    rows = [
        (
            record.survey_id,
            record.persona_uuid,
            record.stimulus_id,
            record.question_id,
            record.sequence,
            record.answer_raw,
            record.answer_codes,
            record.answer_text,
            record.answer_reasoning,
            record.options_order,
            record.flags,
            record.latency_ms,
            record.input_tokens,
            record.output_tokens,
            record.attempt,
            datetime.fromtimestamp(record.ts, UTC),
        )
        for record in records
    ]
    return spark.createDataFrame(rows, RESPONSES_SCHEMA)


def _apply_straightline(
    spark: SparkSession, survey: SurveyDefinition, responses_locator
) -> None:
    """`straightline` を、調査の全レコードを見て付け直す（§8）。

    答えが増えると判定が変わりうる（3問一致で立ったフラグが、4問目で外れる）。
    そのため付けるだけでなく、条件を満たさなくなった行からは外す。
    """
    from pyspark.sql import functions as F

    if not delta.table_exists(spark, responses_locator):
        return

    # 対象は single / scale のみ。multi を混ぜると `answer_codes[0]` の比較が
    # 「先頭が同じだけ」の別回答を同一視する（`flags.straightline_question_ids`）。
    closed_question_ids = flag_names.straightline_question_ids(survey.questions)
    if not closed_question_ids:
        return

    responses = delta.read_table(spark, responses_locator).filter(
        F.col("survey_id") == F.lit(survey.survey_id)
    )

    judged = (
        responses.filter(F.col("question_id").isin(closed_question_ids))
        # 選択式で番号が1つも取れていない行（パース失敗）は判定に使わない。
        .filter(F.size(F.col("answer_codes")) > 0)
        .groupBy("persona_uuid")
        .agg(
            F.count("*").alias("answered"),
            # 対象は single / scale だけなので `answer_codes` は1要素に定まる。
            # 先頭要素を見れば「毎回同じ位置を選んだか」が判定できる。
            F.countDistinct(F.col("answer_codes")[0]).alias("distinct_codes"),
        )
        .withColumn(
            "is_straightline",
            (F.col("answered") >= F.lit(flag_names.STRAIGHTLINE_MIN_ANSWERS))
            & (F.col("distinct_codes") == F.lit(1)),
        )
        .select("persona_uuid", "is_straightline")
    )

    flag = F.lit(flag_names.STRAIGHTLINE)
    without = F.array_except(F.col("flags"), F.array(flag))
    corrected = (
        responses.join(judged, on="persona_uuid", how="left")
        .withColumn(
            "new_flags",
            F.when(F.coalesce(F.col("is_straightline"), F.lit(False)), F.array_union(without, F.array(flag)))
            .otherwise(without),
        )
        .filter(F.col("new_flags") != F.col("flags"))
        .drop("flags", "is_straightline")
        .withColumnRenamed("new_flags", "flags")
        # 既にあるテーブルには新しい列（`answer_reasoning`）が無いことがある。
        # 定義どおりの並びに戻すのが目的なので、テーブルが持っている列だけを取る。
        .select(*(name for name in RESPONSES_SCHEMA_COLUMNS if name in responses.columns))
    )

    if corrected.take(1):
        delta.merge_upsert(spark, corrected, responses_locator, RESPONSE_KEYS)


RESPONSES_SCHEMA_COLUMNS = tuple(
    part.strip().split(" ")[0] for part in RESPONSES_SCHEMA.split(",")
)


def _summarize(
    spark: SparkSession,
    survey: SurveyDefinition,
    responses_locator,
    execution: ExecutionResult,
    result: RunResult,
) -> None:
    """フラグ集計と E3・E4 の警告（§11）、および伝送層の再試行の警告。"""
    from pyspark.sql import functions as F

    result.input_tokens = execution.input_tokens
    result.output_tokens = execution.output_tokens
    result.warnings.extend(execution.errors[:3])

    # 下の早期 return より前に出す。1件も書き出せなかった実行こそ再試行が多いはずで、
    # そこで警告が消えると「なぜ遅かったのか」が最も知りたい場面で何も残らない。
    warning = retry_warning(result.retry_stats)
    if warning:
        result.warnings.append(warning)

    budget_warning = output_budget_warning(
        escalated=result.output_budget_escalated, exhausted=result.output_budget_exhausted
    )
    if budget_warning:
        result.warnings.append(budget_warning)

    if not delta.table_exists(spark, responses_locator):
        return

    responses = delta.read_table(spark, responses_locator).filter(
        F.col("survey_id") == F.lit(survey.survey_id)
    )
    total = responses.count()
    if total == 0:
        return

    counts = responses.select(
        *[
            F.sum(F.array_contains(F.col("flags"), F.lit(flag)).cast("int")).alias(flag)
            for flag in flag_names.ALL_FLAGS
        ]
    ).collect()[0]
    result.flag_counts = {flag: int(counts[flag] or 0) for flag in flag_names.ALL_FLAGS}
    result.flag_rates = {flag: count / total for flag, count in result.flag_counts.items()}

    for flag, code in ((flag_names.PARSE_ERROR, "E3"), (flag_names.REFUSAL, "E4")):
        rate = result.flag_rates.get(flag, 0.0)
        if rate > FLAG_WARNING_THRESHOLD:
            result.warnings.append(
                f"[{code}] {flag} が全体の {rate:.1%}（{result.flag_counts[flag]} 件）で"
                f" 閾値 {FLAG_WARNING_THRESHOLD:.0%} を超えている。集計時に注記すること"
            )

    if result.structured_output_fell_back:
        result.warnings.append(
            "[W_STRUCTURED_OUTPUT] エンドポイントが構造化出力を受け付けなかったため、"
            "正規表現パースに切り替えて実行した（§6.3 の auto）"
        )

    result.model_versions = tuple(
        sorted({r.model_version for r in execution.records if r.model_version})
    )
