"""スクリーニング（`SPEC_PHASE1.md` §4.2）。

対象者条件の満たし方は3通りある。**費用と、得られるものが違う。**

| | `ask` | `assume` | `infer` |
|---|---|---|---|
| やること | 設問に答えさせ通過者だけを残す | 聞かずに条件を前提として与える | 別の LLM に候補をまとめて判定させる |
| 費用 | `size × oversample_factor × 設問数` | 0 | `候補数 ÷ batch_size` |
| 属性付与 | 本人の回答 | 全候補に強制 | 蓋然性が高い候補のみ |
| インシデンス | 実測できる | **測れない**。1.0 と記録してはいけない | **推定値**。実測とは別枠に記録する |

`infer` の判定そのものは `persona_sim.panel.infer` にある。

どの方式でも、確定したペルソナのカードに前提ブロックを足す。`memory: none` では
設問ごとに独立したセッションになるため、**会話履歴では前提を引き継げない**からである。
前提ブロックの見出しは方式ごとに分ける。プロンプトを読んだときに由来を取り違えないため。

判定結果の唯一の置き場所は `panels.role`。`screener_responses` には生の回答だけを残す。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from persona_sim.llm.budget import output_budget_warning
from persona_sim.llm.client import RetryStats, retry_stats_of, retry_warning
from persona_sim.panel.schema import (
    ModelConfig,
    PersonaCardConfig,
    PromptHeadings,
    ScreeningConfig,
    SurveyDefinition,
)
from persona_sim.run import flags as flag_names
from persona_sim.run.prompt import PremiseBlock

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

#: `screener_responses` の主キー（§2.6）。
SCREENER_KEYS = ("survey_id", "persona_uuid", "question_id")

#: `mode: infer` の判定結果を入れる予約 `question_id`（§2.6）。
#: 判定は条件ごとではなく候補1人につき1回なので、対応する設問IDが存在しない。
#: 通過なら `answer_codes: [1]`、非通過なら空。`ask` の実回答とは `inferred` フラグで区別する。
#: 方式は1つだけ。`runs.screener_method` と `ScreeningResult.method` に入る。
SCREENER_METHOD = "infer"

INFER_QUESTION_ID = "_infer"

#: `INFER_QUESTION_ID` の通過を表す選択肢番号。
INFER_PASSED_CODE = 1

#: `screener_responses` のスキーマ。`responses` と違い `stimulus_id` / `sequence` を持たない。
#: `config_hash` はその行を得たときのスクリーニング設定の指紋（`screening_fingerprint()`）。
SCREENER_SCHEMA = (
    "survey_id string, persona_uuid string, question_id string, "
    "answer_raw string, answer_codes array<int>, answer_text string, "
    "options_order array<int>, flags array<string>, latency_ms int, "
    "input_tokens int, output_tokens int, attempt int, ts timestamp, "
    "config_hash string"
)

#: 判定設定の指紋を入れる列。この値が変わった行は「別の設定で得た判定」なので使わない。
CONFIG_HASH_COLUMN = "config_hash"


#: E2 の再試行回数（初回を含まない）。仕様どおり最大3回まで倍にする。
MAX_OVERSAMPLE_RETRIES = 3


# --------------------------------------------------------------------------- #
# 純関数（Spark 不要）
# --------------------------------------------------------------------------- #


def screening_fingerprint(survey: SurveyDefinition) -> str:
    """判定結果の再利用可否を決める、スクリーニング設定の指紋。

    `screener_responses` はペルソナ単位で upsert され、判定済みの候補は聞き直さない。
    どの設定で得た判定なのかを行に持たせておかないと、**プロンプトやモデルを変えても
    古い判定がそのまま使われる**（判定に手を入れたのに結果が1件も変わらない、という
    形で表面化する）。この指紋を行に残し、変わったら未判定として扱う。

    含めるのは「判定の中身を変えうるもの」だけ。**`oversample_factor` は含めない** —
    通過者不足の再試行で倍になるが、既に済んだ判定の中身は変わらないため、含めると
    再試行のたびに全件を聞き直すことになる。`concurrency` も結果を変えないので入れない。
    """
    if survey.screening is None:
        return ""

    from persona_sim.panel.infer import judge_model

    screener = survey.screening
    # 判定は別セッションの LLM。見せる人物像もプロンプトもスクリーニング側のもの。
    payload: dict[str, Any] = {
        "conditions": list(screener.conditions),
        "batch_size": screener.batch_size,
        "model": _model_fingerprint(judge_model(survey.model, screener)),
        "prompt": {
            "system": screener.prompt.system,
            "rule": screener.prompt.rule,
        },
        "persona_card": _persona_card_fingerprint(screener.persona_card),
    }

    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _model_fingerprint(model: ModelConfig) -> dict[str, Any]:
    """モデル設定のうち、判定の中身を変えうるものだけ。`concurrency` は入れない。"""
    return {
        "endpoint": str(model.endpoint),
        "deployment": model.deployment,
        "thinking": model.thinking,
        "max_tokens": model.max_tokens,
        "structured_output": str(model.structured_output),
    }


def _persona_card_fingerprint(card: PersonaCardConfig) -> dict[str, Any]:
    """ペルソナカードの中身。見せる情報が変われば判定も変わる。"""
    return {
        "attributes": [
            {"field": attribute.field, "suffix": attribute.suffix}
            for attribute in card.attributes
        ],
        "include_summary": card.include_summary,
        "persona_fields": [
            {"field": persona_field.field, "label": persona_field.label}
            for persona_field in card.persona_fields
        ],
    }


def infer_premise(
    screener: ScreeningConfig, headings: PromptHeadings | None = None
) -> PremiseBlock | None:
    """`infer` の前提ブロック。文言は `assume` と同じ条件文を使う。

    見出しだけ分ける。本人が答えたのでも、全員に一律で与えたのでもなく、
    「別の判定で合致する蓋然性が高いとされた」という由来を残すため。
    """
    headings = headings or PromptHeadings()
    lines = screener.condition_texts()
    return PremiseBlock(heading=headings.infer_premise, lines=lines) if lines else None


@dataclass
class ScreeningVerdict:
    """判定結果。`panels.role` を書き換えるための材料。"""

    passed: set[str] = field(default_factory=set)
    tested: set[str] = field(default_factory=set)
    codes: dict[str, dict[str, tuple[int, ...]]] = field(default_factory=dict)

    @property
    def failed(self) -> set[str]:
        return self.tested - self.passed


def judge(codes_by_persona: Mapping[str, Mapping[str, Sequence[int]]]) -> ScreeningVerdict:
    """判定結果を通過／非通過に振り分ける。

    `infer` は候補1人につき判定1回で、予約IDの下に通過（`[1]`）／非通過（`[]`）だけが入る。
    条件をどう結合するかは判定プロンプト（`screening.prompt.rule`）の文面が決める。
    """
    verdict = ScreeningVerdict()
    for persona_uuid, codes in codes_by_persona.items():
        verdict.tested.add(persona_uuid)
        verdict.codes[persona_uuid] = {qid: tuple(v) for qid, v in codes.items()}
        if codes.get(INFER_QUESTION_ID):
            verdict.passed.add(persona_uuid)
    return verdict


# --------------------------------------------------------------------------- #
# 前提ブロックの供給（`run` から使う）
# --------------------------------------------------------------------------- #


def load_premises(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage,
    persona_uuids: Sequence[str],
) -> dict[str, PremiseBlock]:
    """ペルソナごとの前提ブロックを作る（§6.1）。スクリーナーが無ければ空。

    判定結果は**現在の設定で得たものだけ**を見る。指紋で絞らないと、設定を変えた後に
    古い判定を根拠として前提ブロックを配ってしまう。
    """
    screener = survey.screening
    if screener is None:
        return {}

    premise = infer_premise(screener, survey.prompt.headings)
    if premise is None:
        return {}

    # 通過者だけに与える。ここに来るのは確定パネルの面々なので全員が通過者だが、
    # 判定結果が残っていないペルソナには与えない（黙って条件を付けない）。
    judged = read_screener_codes(
        spark, survey, storage, config_hash=screening_fingerprint(survey)
    )
    return {
        uuid: premise for uuid in persona_uuids if judged.get(uuid, {}).get(INFER_QUESTION_ID)
    }


def expected_question_ids(screener: ScreeningConfig) -> tuple[str, ...]:
    """`screener_responses` に現れるべき `question_id`。候補1人につき1行（予約ID）。"""
    return (INFER_QUESTION_ID,)


def skipped_session_count(screener: ScreeningConfig, skipped_personas: int) -> int:
    """判定済みで聞き直さなかったぶんを、`sessions_total` と**同じ単位**で数える。

    1バッチ＝1呼び出しなので**バッチ数**に換算する。ここを揃えないと `sessions_total` が
    バッチ数・`sessions_skipped` が人数になり、同じラベルで並べたときに
    「1バッチ中 380 スキップ」のように**内訳が総数を超えて**見える。

    バッチ境界とは厳密には一致しない（実際にどう切られたかは候補の並びによる）が、
    「どのくらい省けたか」を同じ単位で示すという用途には足りる。
    """
    if skipped_personas <= 0:
        return 0
    from persona_sim.panel.infer import infer_session_count

    return infer_session_count(skipped_personas, screener)


def read_screener_codes(
    spark: SparkSession, survey: SurveyDefinition, storage, *, config_hash: str | None = None
) -> dict[str, dict[str, tuple[int, ...]]]:
    """`screener_responses` から選択肢番号を読む。

    `answer_codes` は提示順の番号なので、`options_order` で定義順に戻す
    （スクリーナーはシャッフルしないので実質恒等だが、経路を揃えておく）。
    `multi` は複数選ばれるため、番号は常に並びとして扱う。

    `config_hash` を渡すと、**その設定で得た判定だけ**を返す。プロンプトやモデルを
    変えた後に古い判定を使い回さないための絞り込みで、判定済みかどうかを見るところ
    （`screen_survey` の todo 算出、`load_premises`）は必ず現在の指紋を渡すこと。
    列を持たない古いテーブルは、判定が1件も無いものとして扱う（全件を聞き直す）。

    **`judge_error` の行は返さない。** 判定の呼び出しそのものが失敗した行であり、
    条件に合わなかったのではなく判定できていない。これを返すと、通信の失敗が
    「判定済み・非通過」として居座り、再実行しても聞き直されない（`flags.JUDGE_ERROR`）。
    """
    from pyspark.sql import functions as F

    from persona_sim.storage import delta
    from persona_sim.storage.locator import SCREENER_RESPONSES, locator

    screener_locator = locator(SCREENER_RESPONSES, storage)
    if not delta.table_exists(spark, screener_locator):
        return {}

    table = delta.read_table(spark, screener_locator)
    if config_hash is not None:
        if CONFIG_HASH_COLUMN not in table.columns:
            return {}
        table = table.filter(F.col(CONFIG_HASH_COLUMN) == F.lit(config_hash))

    if "flags" in table.columns:
        # `flags` が null の行（フラグを持たない古い行）は落とさない。
        table = table.filter(
            F.col("flags").isNull()
            | ~F.array_contains(F.col("flags"), F.lit(flag_names.JUDGE_ERROR))
        )

    rows = (
        table.filter(F.col("survey_id") == F.lit(survey.survey_id))
        .select("persona_uuid", "question_id", "answer_codes", "options_order")
        .collect()
    )

    codes: dict[str, dict[str, tuple[int, ...]]] = {}
    for row in rows:
        by_question = codes.setdefault(row["persona_uuid"], {})
        order = row["options_order"] or []
        by_question[row["question_id"]] = tuple(
            order[code - 1] if 1 <= code <= len(order) else code
            for code in (row["answer_codes"] or [])
        )
    return codes


def to_screener_dataframe(spark: SparkSession, records, config_hash: str) -> DataFrame:
    """`ResponseRecord` を `screener_responses` の形にする。

    `config_hash` はどの設定で得た判定かを示す指紋（`screening_fingerprint()`）。
    全行に同じ値が入る。これを残さないと、設定を変えた後も古い判定が再利用される。
    """
    from datetime import UTC, datetime

    rows = [
        (
            record.survey_id,
            record.persona_uuid,
            record.question_id,
            record.answer_raw,
            record.answer_codes,
            record.answer_text,
            record.options_order,
            record.flags,
            record.latency_ms,
            record.input_tokens,
            record.output_tokens,
            record.attempt,
            datetime.fromtimestamp(record.ts, UTC),
            config_hash,
        )
        for record in records
    ]
    return spark.createDataFrame(rows, SCREENER_SCHEMA)


def unreachable_cells(
    finalized, requested: Mapping[str, int], factor: int, attempts_left: int
) -> dict[str, str]:
    """通過率から見て、倍率を上限まで上げても必要数に届かないセルとその理由。

    通過者不足の再試行（`oversample_factor` を2倍・最大3回）は「通過率はあるが揺らいだ」
    ときのための仕組みで、**通過率そのものが構造的に低い場合には効かない**。
    候補を倍にしても通過率は変わらないので必要数には届かず、判定の呼び出し費用だけが増える。
    届かないと分かった時点で止め、何が起きているかを言って返す。

    到達可能の条件は `通過率 × 必要数 × 上限倍率 >= 必要数`。**両辺の必要数が相殺されて
    `通過率 × 上限倍率 >= 1` になる** — セルの大小によらず、通過率と倍率だけで決まる。
    通過率0のセルは何倍にしても0なので、この式で自動的に到達不能になる（固定閾値は持たない）。
    """
    max_factor = factor * 2**attempts_left
    reasons: dict[str, str] = {}

    for cell_id in finalized.shortfalls:
        needed = requested.get(cell_id, 0)
        passed = finalized.achieved.get(cell_id, 0) + finalized.reserve.get(cell_id, 0)
        judged = passed + finalized.screened_out.get(cell_id, 0)
        if not judged or not needed:
            # 判定していない。通過率が出せないので、ここでは何も言えない。
            continue

        rate = passed / judged
        if rate * max_factor >= 1:
            continue

        if passed == 0:
            reasons[cell_id] = "通過者が0名のため、候補を何倍に増やしても届かない"
        else:
            reasons[cell_id] = (
                f"必要な候補数の見積もり 約{math.ceil(needed / rate)}名"
                f"（現在の通過率 {rate:.1%}）に対し、上限の {max_factor} 倍でも"
                f" {needed * max_factor} 名までしか増やせない"
            )
    return reasons


def incidence_from_panels(panel_rows: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """`panels` の役割からセル別・全体のインシデンスを出す（§9）。

    通過率 = (main + reserve) / (main + reserve + screened_out)。
    判定していない行（`candidate`）は分母に入れない。
    """
    from persona_sim.panel.sampling import ROLE_MAIN, ROLE_RESERVE, ROLE_SCREENED_OUT

    judged = {ROLE_MAIN, ROLE_RESERVE, ROLE_SCREENED_OUT}
    passed_roles = {ROLE_MAIN, ROLE_RESERVE}

    totals: dict[str, list[int]] = {}
    for row in panel_rows:
        role = row["role"]
        if role not in judged:
            continue
        for key in ("total", str(row["cell_id"])):
            bucket = totals.setdefault(key, [0, 0])
            bucket[1] += 1
            if role in passed_roles:
                bucket[0] += 1

    return {
        key: round(passed / tested, 6)
        for key, (passed, tested) in totals.items()
        if tested
    }


def infer_call_count(survey: SurveyDefinition) -> int:
    """`infer` で必要になる判定呼び出し回数（§10.1 の見積もり）。

    候補数（`size × oversample_factor`）をバッチサイズで割った切り上げ。
    設問数には比例しない。1回の判定で全条件をまとめて見せるため。
    """
    from persona_sim.panel.infer import infer_session_count

    screener = survey.screening
    if screener is None:
        return 0
    candidates = survey.panel.size * screener.oversample_factor
    return infer_session_count(candidates, screener)


@dataclass
class ScreeningResult:
    """`screen` の結果。"""

    method: str
    executed: bool = False
    sessions_total: int = 0
    sessions_ok: int = 0
    sessions_failed: int = 0
    sessions_skipped: int = 0
    oversample_actual: int = 1
    attempts: int = 0
    incidence: dict[str, float] = field(default_factory=dict)
    achieved: dict[str, int] = field(default_factory=dict)
    reserve: dict[str, int] = field(default_factory=dict)
    screened_out: dict[str, int] = field(default_factory=dict)
    aborted_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: 判定に使ったトークン。**この実行で呼び出したぶんだけ**を数える（`run_metadata.json`
    #: の `tokens_scope: "run"` と同じ範囲）。判定済みで聞き直さなかった候補と、
    #: `assume` / スクリーナー無しは 0 のまま。
    input_tokens: int = 0
    output_tokens: int = 0
    #: この実行で使ったスクリーニング設定の指紋（`screening_fingerprint()`）。
    config_hash: str = ""
    #: 確定済みのパネルを、設定変更（または `--force`）のために作り直して判定し直したか。
    refreshed: bool = False
    #: 伝送層の再試行の実績（§6.5）。統計を出せないクライアントでは None。
    retry_stats: RetryStats | None = None
    #: 出力予算を引き上げた／使い切った回数（`llm/budget.py`）。
    #: オーバーサンプル再試行をまたいで足し込む。
    output_budget_escalated: int = 0
    output_budget_exhausted: int = 0


def screen_survey(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage,
    *,
    client=None,
    detector=None,
    progress=None,
    force: bool = False,
) -> ScreeningResult:
    """スクリーニングを実行してパネルを確定する（§4.2）。

    `ask` のときだけ働く。スクリーナーが無い場合と `assume` では、判定するものが
    無いので何もしない（`panel` が既に確定パネルを出している）。

    通過者が足りなければ `oversample_factor` を2倍にして最大3回まで再試行する。
    抽出はハッシュ順なので、再試行は同じ並びの先を伸ばすだけで、
    判定済みの候補に聞き直すことはない。ただし**通過率から見て上限まで倍にしても
    必要数に届かないと分かった時点で打ち切る**（届かない判定に費用をかけない）。

    判定済みかどうかは、現在のスクリーニング設定の指紋（`screening_fingerprint()`）が
    一致する行だけで見る。プロンプトやモデルを変えれば自動的に聞き直しになる。
    `force=True` は指紋が一致していても全候補を判定し直す（判定側を変えていないのに
    引き直したいとき用の逃げ道）。
    """
    from pyspark.sql import functions as F

    from persona_sim.errors import InsufficientCandidatesError, ScreenerShortfallError
    from persona_sim.llm.registry import build_client
    from persona_sim.panel.build import build_panel, finalize_panel
    from persona_sim.panel.infer import judge_model
    from persona_sim.panel.quotas import allocate_cell_sizes
    from persona_sim.panel.sampling import ROLE_CANDIDATE
    from persona_sim.personas.load import load_personas
    from persona_sim.storage import delta
    from persona_sim.storage.locator import PANELS, SCREENER_RESPONSES, locator

    screener = survey.screening
    if screener is None:
        return ScreeningResult(method="none")

    # 判定用モデル。省略されたキーは調査本体の model を引き継ぐ。
    model = judge_model(survey.model, screener)
    client = client or build_client(model)
    screener_locator = locator(SCREENER_RESPONSES, storage)
    panels_locator = locator(PANELS, storage)

    result = ScreeningResult(method=SCREENER_METHOD)
    result.config_hash = fingerprint = screening_fingerprint(survey)
    result.refreshed = force
    factor = screener.oversample_factor

    if _already_finalized(spark, survey, panels_locator):
        reusable = not force and bool(
            read_screener_codes(spark, survey, storage, config_hash=fingerprint)
        )
        if reusable:
            # 判定済み。聞き直さずに現状を返す（同じコマンドを二度打っても壊れないように）。
            result.oversample_actual = factor
            _fill_from_panels(spark, survey, panels_locator, result)
            return _finish(result, client)
        # 設定が変わった（または --force）。確定済みの `panels` には候補が残っていないので、
        # 候補から作り直してから判定し直す。
        result.refreshed = True
        build_panel(spark, survey, storage, oversample_factor=factor)

    population_capped = False

    for attempt in range(MAX_OVERSAMPLE_RETRIES + 1):
        result.attempts = attempt + 1
        result.oversample_actual = factor
        if attempt > 0:
            # 候補を増やす。ハッシュ順なので既存の候補はそのまま残る。
            try:
                build_panel(spark, survey, storage, oversample_factor=factor)
            except InsufficientCandidatesError:
                # 条件に合うペルソナを使い切った。これ以上は増やせないので、
                # 今ある候補で判定して結果を返す。ここで E1 を投げると
                # 「30人頼んだのに480人足りない」という分かりにくい報告になる。
                population_capped = True
                factor = result.oversample_actual

        candidates = [
            row["persona_uuid"]
            for row in delta.read_table(spark, panels_locator)
            .filter(F.col("survey_id") == F.lit(survey.survey_id))
            .filter(F.col("role") == F.lit(ROLE_CANDIDATE))
            .select("persona_uuid")
            .collect()
        ]

        # 現在の設定で得た判定だけを「済み」とみなす。指紋が変わっていれば全件やり直す。
        # `force` が効くのは初回だけ。毎回効かせると、この実行の中で既に判定した候補まで
        # 再試行のたびに聞き直してしまう（`force` は前回までの判定を捨てる指定であって、
        # 同じ実行の中で何度も聞き直す指定ではない）。
        answered = (
            {}
            if (force and attempt == 0)
            else read_screener_codes(spark, survey, storage, config_hash=fingerprint)
        )
        todo = [
            persona_uuid
            for persona_uuid in candidates
            if INFER_QUESTION_ID not in answered.get(persona_uuid, {})
        ]
        result.sessions_skipped += skipped_session_count(
            screener, len(candidates) - len(todo)
        )

        if todo:
            personas = load_personas(spark, survey, storage, todo)
            execution = _infer_execution(
                survey, screener, personas, todo, client, model, progress
            )
            result.output_budget_escalated += execution.budget_escalated
            result.output_budget_exhausted += execution.budget_exhausted
            result.sessions_total += execution.sessions_total
            result.sessions_ok += execution.sessions_ok
            result.sessions_failed += execution.sessions_failed
            # 中断で下の early return に入っても、呼んだぶんは計上済みにしておく。
            # 再試行のたびにここを通るので足し込む。
            result.input_tokens += execution.input_tokens
            result.output_tokens += execution.output_tokens
            result.executed = True
            if execution.records:
                delta.merge_upsert(
                    spark,
                    to_screener_dataframe(spark, execution.records, fingerprint),
                    screener_locator,
                    SCREENER_KEYS,
                    # `config_hash` を足したリリースを、既にあるテーブルの上に載せられるように。
                    evolve_schema=True,
                )
            if execution.aborted:
                result.aborted_reason = execution.aborted_reason
                return _finish(result, client)

        verdict = judge(read_screener_codes(spark, survey, storage, config_hash=fingerprint))
        finalized = finalize_panel(spark, survey, storage, verdict.passed)
        result.achieved = finalized.achieved
        result.reserve = finalized.reserve
        result.screened_out = finalized.screened_out

        if finalized.complete:
            break

        # 通過率から見て上限まで倍にしても届かないなら、ここで打ち切る。
        # 届かない再試行に判定の呼び出し費用をかけない。
        attempts_left = MAX_OVERSAMPLE_RETRIES - attempt
        unreachable = unreachable_cells(
            finalized, allocate_cell_sizes(survey), factor, attempts_left
        )
        if unreachable or population_capped or attempt == MAX_OVERSAMPLE_RETRIES:
            raise ScreenerShortfallError(
                _shortfall_message(
                    finalized,
                    result,
                    factor,
                    population_capped,
                    unreachable=unreachable,
                    max_factor=factor * 2**attempts_left,
                )
            )
        factor *= 2

    result.incidence = incidence_from_panels(
        [
            row.asDict()
            for row in delta.read_table(spark, panels_locator)
            .filter(F.col("survey_id") == F.lit(survey.survey_id))
            .select("cell_id", "role")
            .collect()
        ]
    )
    return _finish(result, client)


def _finish(result: ScreeningResult, client) -> ScreeningResult:
    """判定を終えた結果に、伝送層の再試行の実績と警告を載せて返す。

    `screen_survey` の戻り口が複数あるので関数に切る。どの経路で返しても
    同じものが載っていないと、「判定は通ったが異様に遅かった」実行だけ記録が欠ける。
    """
    result.retry_stats = retry_stats_of(client)
    warning = retry_warning(result.retry_stats)
    if warning:
        result.warnings.append(warning)

    budget_warning = output_budget_warning(
        escalated=result.output_budget_escalated, exhausted=result.output_budget_exhausted
    )
    if budget_warning:
        result.warnings.append(budget_warning)
    return result


@dataclass
class _InferExecution:
    """`infer` の判定結果を `execute` の戻り値と同じ形にした入れ物。

    呼び出し側の集計・書き出し・中断判定を1本の経路に保つための薄い変換。
    `infer` は1バッチ＝1呼び出しなので、セッション数はバッチ数で数える。
    """

    sessions_total: int
    sessions_ok: int
    sessions_failed: int
    records: list
    #: `ExecutionResult` と同じ属性名で持つ（呼び出し側を1本の経路に保つため）。
    #: あちらと違い `records` から求めない。理由は `InferResult` のコメント。
    input_tokens: int = 0
    output_tokens: int = 0
    aborted_reason: str | None = None
    #: 出力予算の引き上げ実績（`llm/budget.py`）。`ask` 経路の `OutputBudgetState` に相当する。
    budget_escalated: int = 0
    budget_exhausted: int = 0

    @property
    def aborted(self) -> bool:
        return self.aborted_reason is not None


def _infer_execution(
    survey: SurveyDefinition,
    screener: ScreeningConfig,
    personas,
    todo: Sequence[str],
    client,
    model,
    progress,
) -> _InferExecution:
    """候補をまとめて判定する（§4.2 `infer`）。"""
    from persona_sim.panel.infer import run_inference

    inferred = run_inference(
        survey.survey_id,
        todo,
        personas,
        screener,
        client,
        model,
        progress=progress,
    )
    aborted_reason = None
    if inferred.batches_total and inferred.batches_ok == 0:
        # 1バッチも通らなかった。このまま進めると全員が非通過になり、
        # 「条件に合う人がいない」と「判定できていない」を取り違える。
        # エラー文をそのまま載せる。これが無いと、どこを直せばよいのか
        # `screener_responses.answer_raw` を掘るまで分からない。
        aborted_reason = (
            f"infer の判定が全 {inferred.batches_total} バッチで失敗した: "
            f"{inferred.errors[0] if inferred.errors else '理由不明'}"
        )
    return _InferExecution(
        sessions_total=inferred.batches_total,
        sessions_ok=inferred.batches_ok,
        sessions_failed=inferred.batches_failed,
        records=inferred.records,
        input_tokens=inferred.input_tokens,
        output_tokens=inferred.output_tokens,
        aborted_reason=aborted_reason,
        budget_escalated=inferred.budget_escalated,
        budget_exhausted=inferred.budget_exhausted,
    )


def _already_finalized(spark: SparkSession, survey: SurveyDefinition, panels_locator) -> bool:
    """判定済みの行があり、未判定の候補が残っていないか。"""
    from pyspark.sql import functions as F

    from persona_sim.panel.sampling import ROLE_CANDIDATE
    from persona_sim.storage import delta

    if not delta.table_exists(spark, panels_locator):
        return False
    rows = (
        delta.read_table(spark, panels_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .select("role")
        .collect()
    )
    roles = {row["role"] for row in rows}
    return bool(roles) and ROLE_CANDIDATE not in roles


def _fill_from_panels(
    spark: SparkSession, survey: SurveyDefinition, panels_locator, result: ScreeningResult
) -> None:
    """確定済みの `panels` から集計値を埋める。"""
    from pyspark.sql import functions as F

    from persona_sim.panel.sampling import ROLE_MAIN, ROLE_RESERVE, ROLE_SCREENED_OUT
    from persona_sim.storage import delta

    rows = [
        row.asDict()
        for row in delta.read_table(spark, panels_locator)
        .filter(F.col("survey_id") == F.lit(survey.survey_id))
        .select("cell_id", "role")
        .collect()
    ]
    buckets = {ROLE_MAIN: result.achieved, ROLE_RESERVE: result.reserve, ROLE_SCREENED_OUT: result.screened_out}
    for row in rows:
        bucket = buckets.get(row["role"])
        if bucket is not None:
            bucket[row["cell_id"]] = bucket.get(row["cell_id"], 0) + 1
    result.incidence = incidence_from_panels(rows)


def _shortfall_message(
    finalized,
    result: ScreeningResult,
    factor: int,
    population_capped: bool = False,
    *,
    unreachable: Mapping[str, str] | None = None,
    max_factor: int | None = None,
) -> str:
    unreachable = unreachable or {}
    lines = [
        f"  {cell_id}: 不足 {shortfall} 名"
        f"（通過 {finalized.achieved.get(cell_id, 0)} / 判定 "
        f"{finalized.achieved.get(cell_id, 0) + finalized.reserve.get(cell_id, 0) + finalized.screened_out.get(cell_id, 0)}）"
        + (f"\n    {unreachable[cell_id]}" if cell_id in unreachable else "")
        for cell_id, shortfall in finalized.shortfalls.items()
    ]
    incidence = incidence_from_panels(
        [
            {"cell_id": cell_id, "role": role}
            for cell_id in finalized.achieved
            for role, count in (
                ("main", finalized.achieved.get(cell_id, 0)),
                ("reserve", finalized.reserve.get(cell_id, 0)),
                ("screened_out", finalized.screened_out.get(cell_id, 0)),
            )
            for _ in range(count)
        ]
    )
    rates = " / ".join(f"{cell_id}={rate:.1%}" for cell_id, rate in sorted(incidence.items()))
    if population_capped:
        reason = "条件に合うペルソナを使い切ったため、これ以上候補を増やせない"
    elif unreachable:
        # 通過率が構造的に低い。倍率を上げ続けても届かないので、上限まで試さずに止めた。
        reason = (
            f"実インシデンスが低く、oversample_factor を上限の {max_factor} 倍まで"
            f"上げても必要数に届かないため、{result.attempts} 回で打ち切った"
        )
    else:
        reason = f"oversample_factor を {factor} まで上げて {result.attempts} 回試行した"
    return (
        f"スクリーニング通過者が必要数に満たない（{reason}）:\n"
        + "\n".join(lines)
        + f"\n実インシデンス: {rates}"
    )
