"""実行メタデータ（`SPEC_PHASE1.md` §9）。

利用側が実査と突き合わせるために必要な情報を、`runs` テーブルと
`outputs/{survey_id}/run_metadata.json` の両方に残す。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from persona_sim import __version__, attribution_text
from persona_sim.config import StorageConfig
from persona_sim.panel.quotas import allocate_cell_sizes
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.run.memory import resolve_remember
from persona_sim.run.run import RunResult
from persona_sim.storage import delta
from persona_sim.storage.locator import PANELS, PERSONAS_BASE, RUNS, locator

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import SparkSession

#: `runs` のスキーマ（§2.4）。
#:
#: `survey_name` / `survey_type` は `metadata_json` にも入るが、**列としても持つ**。
#: 調査一覧を出すたびに全件の JSON を掘るのは無駄が大きく、種別で絞ることもできないため。
RUNS_SCHEMA = (
    "survey_id string, survey_name string, survey_type string, run_id string, "
    "started_at timestamp, finished_at timestamp, "
    "sessions_total int, sessions_ok int, sessions_failed int, sessions_skipped int, "
    "aborted_reason string, metadata_json string"
)


def run_id(survey: SurveyDefinition, result: RunResult) -> str:
    """実行の識別子。開始時刻から作るので乱数を使わない。"""
    return f"{survey.survey_id}-{result.started_at:%Y%m%dT%H%M%SZ}"


def build_metadata(
    spark: SparkSession,
    survey: SurveyDefinition,
    result: RunResult,
    storage: StorageConfig,
) -> dict[str, Any]:
    """§9 の構造を組み立てる。"""
    return {
        "survey_id": survey.survey_id,
        "survey_name": survey.name,
        # 調査定義に書かれていなくても既定値が入るので、実際に何の調査だったかはここで分かる。
        "survey_type": str(survey.survey_type),
        "run_id": run_id(survey, result),
        "persona_sim_version": __version__,
        "survey_definition": survey.raw,
        "model": {
            "endpoint": str(survey.model.endpoint),
            "deployment": survey.model.deployment,
            "client": result.client_description,
            "model_version": list(result.model_versions),
            "thinking": survey.model.thinking,
            # 理由を書かせた設問（§6.3）。思考モードとは別物で、書かせたなら
            # `responses.answer_reasoning` に残っている。何問で使ったかを残さないと、
            # 後から結果を比べるときに条件の違いが読み取れない。
            "reasoning_questions": [
                question.id for question in survey.questions if question.reasoning
            ],
            "max_tokens_reasoning": survey.model.max_tokens_reasoning,
            "structured_output": str(survey.model.structured_output),
            "structured_output_fell_back": result.structured_output_fell_back,
            # 出力予算の引き上げ（`llm/budget.py`）。広げて通ったぶんにはフラグが立たないので、
            # `max_tokens` が実態に足りているかはここでしか読み取れない。
            "output_limit": {
                "escalated": result.output_budget_escalated,
                "exhausted": result.output_budget_exhausted,
            },
            # 伝送層の再試行（§6.5）。`latency_ms` にはバックオフの待ち時間も含まれるので、
            # これが無いと「モデルが遅かった」のか「429 で眠っていた」のかを後から分けられない。
            "endpoint_retries": _retry_summary(result.retry_stats),
        },
        # 実行後に「実際に何を聞いたのか」を確認できるようにする。
        # 1名ぶんだけ残すが、その1名については全設問を残す。
        "prompt_sample": _prompt_sample(result),
        # 何を見せて聞いたか。`prompt_sample` は1名ぶんなので、設定そのものも残す。
        "persona_card": _persona_card_summary(survey.persona_card),
        "data_versions": {
            "personas_base": result.personas_version,
            "source_dataset": _source_dataset(spark, storage),
        },
        "panel": _panel_summary(spark, survey, storage),
        "design": {
            "sample_overlap": str(survey.design.sample_overlap),
            "presentation": str(survey.design.presentation),
            # 記憶は設問の性質なので、設問ごとに残す。何を持たせて聞いたのかが
            # 分からないと、モデルへの入力を再現できない（§9.1）。
            "questions_memory": {
                question_id: remember.describe()
                for question_id, remember in resolve_remember(survey).items()
            },
            "rotation": str(survey.design.rotation),
            "image_mode": sorted({str(s.image_mode) for s in survey.stimuli}),
        },
        "execution": {
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
            "sessions_total": result.sessions_total,
            "sessions_ok": result.sessions_ok,
            "sessions_failed": result.sessions_failed,
            "sessions_skipped": result.sessions_skipped,
            "records_written": result.records_written,
            "aborted_reason": result.aborted_reason,
            # フラグは調査全体（過去の実行ぶんを含む）の集計、トークンはこの実行ぶんだけ。
            # 集計対象が違うので明示する。
            "flags": result.flag_counts,
            "flags_scope": "survey",
            "flag_rates": {flag: round(rate, 6) for flag, rate in result.flag_rates.items()},
            "tokens": {"input": result.input_tokens, "output": result.output_tokens},
            "tokens_scope": "run",
            # 単価はワークスペースごとに異なり、システム側に持たせると必ず古くなる。
            # トークン数を残し、金額換算は利用側に委ねる。
            "estimated_cost": None,
            "warnings": result.warnings,
        },
        # 帰属表示（§15.3）と免責（§15.1）。実行の記録そのものにも残す。
        # メタデータだけを受け取った相手にも、これが何の数字なのかが伝わるように。
        "attribution": attribution_text(),
        "reproducibility": {
            "seed": survey.panel.seed,
            # §9.1: 保証するのは入力側。生成結果の一致は保証しない。
            "guaranteed": "inputs_only",
            "note": (
                "seed と各バージョンが揃えば、誰にどのコンセプトをどの順・どの選択肢順で"
                "提示したかまでが再現される。生成結果そのものの一致は保証しない。"
            ),
        },
    }


def write_metadata(
    spark: SparkSession,
    survey: SurveyDefinition,
    result: RunResult,
    storage: StorageConfig,
    output_dir: str,
) -> Path:
    """`run_metadata.json` を書き、`runs` テーブルにも1行追加する。"""
    metadata = build_metadata(spark, survey, result, storage)

    destination = Path(output_dir) / survey.survey_id
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "run_metadata.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    if metadata["prompt_sample"] is not None:
        (destination / "prompt_sample.md").write_text(
            render_prompt_sample(survey, metadata["prompt_sample"]), encoding="utf-8"
        )

    runs = spark.createDataFrame(
        [
            (
                survey.survey_id,
                survey.name,
                str(survey.survey_type),
                metadata["run_id"],
                result.started_at,
                result.finished_at,
                result.sessions_total,
                result.sessions_ok,
                result.sessions_failed,
                result.sessions_skipped,
                result.aborted_reason,
                json.dumps(metadata, ensure_ascii=False),
            )
        ],
        RUNS_SCHEMA,
    )
    delta.write_table(runs, locator(RUNS, storage), mode="append")
    return path


def _retry_summary(stats) -> dict[str, Any] | None:
    """伝送層の再試行の実績（§9）。統計を出せないクライアントでは None。"""
    if stats is None:
        return None
    return {
        "calls": stats.calls,
        "retried_calls": stats.retried_calls,
        "retries": stats.retries,
        "backoff_seconds": round(stats.backoff_seconds, 1),
        "reasons": dict(stats.reasons),
    }


def _prompt_sample(result: RunResult) -> dict[str, Any] | None:
    """記録に残す1名ぶんのプロンプト（§9）。その1名の全設問を、聞いた順に並べる。

    メッセージ列をそのまま持つ。記憶を持つ設問は [system] / [user] / [assistant] /
    [user] … という列で送られるので、`system` と `user` の2本には潰せない。
    """
    samples = result.prompt_samples
    if not samples:
        return None
    return {
        "persona_uuid": samples[0].persona_uuid,
        "questions": [
            {
                "stimulus_id": sample.stimulus_id,
                "question_id": sample.question_id,
                "sequence": sample.sequence,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in sample.messages
                ],
                # 空でなければ、その [assistant] は目印で埋まっている（実物と一致しない）。
                "missing_answers": list(sample.missing_answers),
            }
            for sample in samples
        ],
    }


def render_prompt_sample(survey: SurveyDefinition, sample: dict[str, Any]) -> str:
    """`prompt_sample.md` の本文を組み立てる。

    プロンプト本文は `■` 見出しや `【】` を含むので、コードブロックに入れて
    Markdown として解釈されないようにする。
    """
    questions = sample["questions"]
    lines = [
        "# 実際に送ったプロンプト（1ペルソナ分・全設問）",
        "",
        "| 項目 | 値 |",
        "|---|---|",
        f"| survey_id | `{survey.survey_id}` |",
        f"| persona_uuid | `{sample['persona_uuid']}` |",
        f"| 設問数 | {len(questions)} |",
        "",
        "同じ調査を何度実行しても、ここに出るペルソナは変わらない。",
        "設問は聞いた順に並ぶ。記憶を持つ設問には、その人が実際に返した回答を",
        "[assistant] として再生してある。",
        "",
    ]

    for index, entry in enumerate(questions, start=1):
        lines += [
            "---",
            "",
            f"## {index}. {entry['question_id']}",
            "",
            "| 項目 | 値 |",
            "|---|---|",
            f"| stimulus_id | `{entry['stimulus_id']}` |",
            f"| question_id | `{entry['question_id']}` |",
            f"| sequence | {entry['sequence']} |",
            "",
        ]
        missing = entry.get("missing_answers") or []
        if missing:
            ids = "・".join(f"`{question_id}`" for question_id in missing)
            lines += [
                f"> 記憶として再生した設問のうち {ids} の回答を `responses` から引けなかった。"
                "該当の [assistant] は目印で埋めてあり、実際に送った文面とは一致しない。",
                "",
            ]
        for message in entry["messages"]:
            lines += [
                f"### [{message['role']}]",
                "",
                "```text",
                _format_content_for_sample(message["content"]),
                "```",
                "",
            ]

    return "\n".join(lines)


def _format_content_for_sample(content: str | list[dict[str, Any]]) -> str:
    """実プロンプト記録用の可視化文字列を作成する。"""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text", "")
            if text:
                parts.append(text)
        elif part_type == "image_url":
            url = part.get("image_url", {}).get("url", "")
            parts.append(f"[Image: {url}]")
        else:
            parts.append(str(part))
    return "\n\n".join(parts)


def _source_dataset(spark: SparkSession, storage: StorageConfig) -> str | None:
    personas_locator = locator(PERSONAS_BASE, storage)
    if not delta.table_exists(spark, personas_locator):
        return None
    rows = delta.read_table(spark, personas_locator).select("source_version").limit(1).collect()
    return rows[0]["source_version"] if rows else None


def _panel_summary(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig
) -> dict[str, Any]:
    from pyspark.sql import functions as F

    from persona_sim.panel.sampling import ROLE_CANDIDATE, ROLE_MAIN
    from persona_sim.panel.screening import incidence_from_panels

    requested = allocate_cell_sizes(survey)
    achieved: dict[str, int] = {}
    incidence: dict[str, float] | None = None
    incidence_estimated: dict[str, float] | None = None
    oversample_actual: float | None = None

    panels_locator = locator(PANELS, storage)
    if delta.table_exists(spark, panels_locator):
        rows = [
            row.asDict()
            for row in delta.read_table(spark, panels_locator)
            .filter(F.col("survey_id") == F.lit(survey.survey_id))
            .select("cell_id", "role")
            .collect()
        ]
        achieved = {}
        for row in rows:
            if row["role"] == ROLE_MAIN:
                achieved[row["cell_id"]] = achieved.get(row["cell_id"], 0) + 1

        screener = survey.screening
        if screener is not None and screener.calls_llm:
            rates = incidence_from_panels(rows) or None
            if screener.asks:
                # 本人に聞いたので実測値（§4.2）。
                incidence = rates
            else:
                # infer は聞いていない。同じ欄に入れると実測値と取り違えられる。
                incidence_estimated = rates
            judged = sum(1 for row in rows if row["role"] != ROLE_CANDIDATE)
            if survey.panel.size:
                oversample_actual = round(judged / survey.panel.size, 3)

    return {
        "seed": survey.panel.seed,
        "size": survey.panel.size,
        "requested": requested,
        "achieved": achieved,
        "screener_method": str(survey.screening.mode) if survey.screening else None,
        # assume / infer では聞いていないので測れていない。1.0 と書くと「全員該当」と
        # 誤読される（§9）。実測できるのは ask だけ。
        "screener_incidence": incidence,
        # infer の通過率。聞いた結果ではなく判定の結果なので、実測値とは別枠にする。
        "screener_incidence_estimated": incidence_estimated,
        "oversample_actual": oversample_actual,
        # 誰をどう判定したかまで残さないと入力を再現できない（§9.1）。
        "screener_infer": _infer_summary(survey),
    }


def _persona_card_summary(card) -> dict[str, Any]:
    """ペルソナカードに何を載せたか（§9.1）。

    どの列をどの順で見せたかまで残さないと、モデルへの入力を再現できない。
    属性行が設定可能になったので、既定に依存せず実際の並びを書き出す。
    """
    return {
        "attributes": [
            {"field": a.field, "suffix": a.suffix} if a.suffix else {"field": a.field}
            for a in card.attributes
        ],
        "include_summary": card.include_summary,
        "persona_fields": [{"field": f.field, "label": f.label} for f in card.persona_fields],
    }


def _infer_summary(survey: SurveyDefinition) -> dict[str, Any] | None:
    """`infer` の判定条件（§9.1）。他の方式では None。"""
    from persona_sim.panel.infer import judge_model

    screening = survey.screening
    if screening is None or not screening.infers:
        return None

    model = judge_model(survey.model, screening)
    return {
        "batch_size": screening.batch_size,
        "model": {
            "endpoint": str(model.endpoint),
            "deployment": model.deployment,
            "max_tokens": model.max_tokens,
        },
        "persona_card": _persona_card_summary(screening.persona_card),
    }
