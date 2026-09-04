"""`screener.mode: infer` の判定（`SPEC.md` §4.2）。

候補ペルソナを一定数ずつまとめて別セッションの LLM に渡し、対象者条件に合致する
蓋然性が高い者を選ばせる。`ask` の費用と `assume` の矛盾の中間を取る方式。

| | `ask` | `assume` | `infer` |
|---|---|---|---|
| 費用 | `size × oversample × 設問数` | 0 | `候補数 ÷ batch_size` |
| 属性付与 | 本人の回答 | 全候補に強制 | 蓋然性の高い候補のみ |
| インシデンス | 実測 | 測れない | **推定値**（実測とは別枠に記録） |

**聞いてはいない。** ここで出る通過率は推定値であり、`ask` の実測値と同じ欄に
入れてはいけない。判定結果には `inferred` フラグを必ず立て、実回答と区別できるようにする。

このモジュールは判定ループを `run.executor` とは別に持つ。既存の `Session` / `Unit` は
1ペルソナ1セッション前提で、複数ペルソナを1回の呼び出しにまとめる `infer` とは形が
合わないため。クライアント生成・メッセージ型・番号列のパースは既存のものを再利用する。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from persona_sim.llm.budget import complete_within_budget
from persona_sim.llm.client import ChatMessage, LLMClient, LLMError
from persona_sim.panel.schema import (
    ModelConfig,
    PersonaCardConfig,
    Question,
    QuestionType,
    ScreeningConfig,
)
from persona_sim.panel.screening import INFER_PASSED_CODE, INFER_QUESTION_ID
from persona_sim.run import flags as flag_names
from persona_sim.run.parsing import parse_answer
from persona_sim.run.progress import ProgressUpdate
from persona_sim.run.session import ResponseRecord

#: 判定プロンプトで人物一覧に付ける見出し。
PERSONA_LIST_HEADING = "■人物一覧"

#: 判定プロンプトで対象者条件に付ける見出し。
CONDITION_HEADING = "■対象者条件"


def judge_model(base: ModelConfig, screening: ScreeningConfig) -> ModelConfig:
    """判定に使うモデル設定。書かれたキーだけを本調査の `model` に重ねる。"""
    return screening.model.apply(base)


def batches(persona_uuids: Sequence[str], batch_size: int) -> list[tuple[str, ...]]:
    """候補を判定単位に切る。

    並べ替えない。候補は既にハッシュ順で、その順序のままスライスすることで
    再実行時も同じ組み合わせになる（§9.1 の再現性）。
    """
    if batch_size < 1:
        raise ValueError("batch_size は1以上")
    return [
        tuple(persona_uuids[start : start + batch_size])
        for start in range(0, len(persona_uuids), batch_size)
    ]


def persona_card_for_judge(persona: Mapping[str, Any], card_config: PersonaCardConfig) -> str:
    """判定に見せるペルソナ像。

    回答生成用の `run.prompt.persona_card` とは**別に持つ**。あちらを流用すると
    1回の判定プロンプトが長くなり、この方式の費用面の利点が消える。だから整形は
    共用しない（こちらは1人1行に詰める）。どの属性を出すかだけ
    `run.prompt.attribute_row()` に寄せてある。
    載せる情報は `screening.persona_card` で決まる。
    """
    from persona_sim.run.prompt import attribute_row

    parts: list[str] = []

    row = attribute_row(persona, card_config.attributes)
    if row:
        parts.append(row)

    if card_config.include_summary:
        summary = persona.get("persona")
        if summary not in (None, ""):
            parts.append(str(summary))

    for persona_field in card_config.persona_fields:
        value = persona.get(persona_field.field)
        if value not in (None, ""):
            parts.append(f"【{persona_field.label}】{value}")

    return " / ".join(parts) if parts else "（情報なし）"


def condition_lines(screening: ScreeningConfig) -> list[str]:
    """判定用 LLM に見せる対象者条件。調査定義の文言をそのまま使う（言い換えない）。"""
    return list(screening.condition_texts())


def build_judge_messages(
    personas: Sequence[Mapping[str, Any]],
    screening: ScreeningConfig,
) -> list[ChatMessage]:
    """1バッチ分の判定プロンプト。

    人物を番号つきで並べ、条件を提示して該当する番号を返させる。
    判定に要るもの（モデル・プロンプト・ペルソナカード）はすべて `screening` の中にある。

    **判定の指示は `prompt.rule` だけが出す。** ここで「すべての条件を満たす人物を選んで
    ください」のような文を足してはいけない。設定から触れない文が rule の直前という
    最も効く位置に入り、`prompt` をどう書き換えても判定が動かなくなる。
    """
    listing = [
        f"{index}. {persona_card_for_judge(persona, screening.persona_card)}"
        for index, persona in enumerate(personas, start=1)
    ]
    conditions = [f"- {line}" for line in condition_lines(screening)]

    user = "\n".join(
        [
            PERSONA_LIST_HEADING,
            *listing,
            "",
            CONDITION_HEADING,
            *conditions,
            "",
            screening.prompt.rule,
        ]
    )
    return [
        ChatMessage(role="system", content=screening.prompt.system),
        ChatMessage(role="user", content=user),
    ]


def parse_judgement(raw: str, batch_size: int) -> tuple[tuple[int, ...], bool]:
    """判定結果の番号列を読む。

    戻り値は (バッチ内の番号, 範囲外があったか)。番号は1始まり。
    「なし」など番号が1つも無い出力は空タプル（＝全員非通過）として扱う。

    番号列のパースは本調査の `multi` と同じ経路を使う。書式の揺れ（全角・読点・
    「と」つなぎ）への対応を二重に持たないため。
    """
    question = Question(
        id="_infer",
        text="",
        type=QuestionType.MULTI,
        options=tuple("" for _ in range(batch_size)),
    )
    parsed = parse_answer(raw, question, batch_size)
    in_range = tuple(code for code in parsed.codes if 1 <= code <= batch_size)
    return in_range, parsed.out_of_range


@dataclass
class InferBatchResult:
    """1バッチの判定結果。"""

    persona_uuids: tuple[str, ...]
    passed: tuple[str, ...]
    raw: str
    out_of_range: bool
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    model_version: str | None = None
    error: str | None = None
    #: 出力予算を引き上げたか／天井まで使い切ったか（`llm/budget.py`）。
    #: 使い切った場合は `error` も立つ（判定できていない）。
    budget_escalated: bool = False
    budget_exhausted: bool = False

    @property
    def failed(self) -> bool:
        return self.error is not None


def judge_batch(
    persona_uuids: Sequence[str],
    personas: Mapping[str, Mapping[str, Any]],
    screening: ScreeningConfig,
    client: LLMClient,
    model: ModelConfig,
) -> InferBatchResult:
    """1バッチを判定する。失敗しても例外にせず、結果に理由を残して返す。

    1バッチの失敗で調査全体を止めない。非通過扱いになるので、通過者が足りなければ
    呼び出し側のオーバーサンプル再試行が働く。
    """
    ordered = [personas[uuid] for uuid in persona_uuids]
    messages = build_judge_messages(ordered, screening)

    started = time.time()
    outcome = None
    try:
        # `complete_within_budget` は `except LLMError` の内側で呼ぶ。`OutputLimitExceeded` は
        # `LLMError` のサブクラスなので、直に `client.complete` を囲むと引き上げが働く前に
        # ここで握りつぶされ、1バッチまるごと未判定になる。
        completion, outcome = complete_within_budget(
            lambda budget: client.complete(messages, max_tokens=budget),
            max_tokens=model.max_tokens,
        )
    except LLMError as exc:
        return InferBatchResult(
            persona_uuids=tuple(persona_uuids),
            passed=(),
            raw="",
            out_of_range=False,
            latency_ms=int((time.time() - started) * 1000),
            input_tokens=None,
            output_tokens=None,
            error=str(exc),
        )

    if outcome.exhausted:
        # 判定できていないので通過・非通過のどちらにも倒さない。`judge_error` として残せば
        # `read_screener_codes` が判定済みから除き、再実行で聞き直される。
        return InferBatchResult(
            persona_uuids=tuple(persona_uuids),
            passed=(),
            raw="",
            out_of_range=False,
            latency_ms=outcome.latency_ms,
            input_tokens=None,
            output_tokens=None,
            error=(
                f"出力が上限に達し、予算を {outcome.final_max_tokens} まで広げても完了しなかった。"
                f"`screening.model.max_tokens` を見直すか `batch_size` を下げること"
            ),
            budget_escalated=outcome.escalated,
            budget_exhausted=True,
        )

    codes, out_of_range = parse_judgement(completion.text, len(persona_uuids))
    return InferBatchResult(
        persona_uuids=tuple(persona_uuids),
        passed=tuple(persona_uuids[code - 1] for code in codes),
        raw=completion.text,
        out_of_range=out_of_range,
        latency_ms=completion.latency_ms,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        model_version=completion.model_version,
        budget_escalated=outcome.escalated,
    )


@dataclass
class InferResult:
    """判定全体の結果。"""

    batches_total: int = 0
    batches_ok: int = 0
    batches_failed: int = 0
    #: ペルソナごとの、設問ごとの選択肢番号（定義順）。`judge` にそのまま渡せる形。
    #: **失敗したバッチの候補は入らない**（判定できていないので、非通過とは言えない）。
    codes: dict[str, dict[str, tuple[int, ...]]] | None = None
    records: list[ResponseRecord] | None = None
    #: 失敗したバッチのエラー文（呼び出し順）。中断理由に載せて原因を読めるようにする。
    errors: list[str] | None = None
    #: この判定で使ったトークン。**`records` を合算して求めてはいけない** —
    #: 1回のバッチ呼び出しのトークン数は、そのバッチの候補人数ぶんの行に複製されるため
    #: （`_to_record`）、合算すると `batch_size` 倍に膨れる。呼び出し単位で足す。
    input_tokens: int = 0
    output_tokens: int = 0
    #: 出力予算を引き上げた／使い切ったバッチ数（`llm/budget.py`）。
    budget_escalated: int = 0
    budget_exhausted: int = 0

    def __post_init__(self) -> None:
        if self.codes is None:
            self.codes = {}
        if self.records is None:
            self.records = []
        if self.errors is None:
            self.errors = []


def run_inference(
    survey_id: str,
    persona_uuids: Sequence[str],
    personas: Mapping[str, Mapping[str, Any]],
    screening: ScreeningConfig,
    client: LLMClient,
    model: ModelConfig,
    *,
    progress=None,
) -> InferResult:
    """候補全体をバッチに分けて判定する。

    通過者には `pass_if` の先頭の番号を、非通過者には空の番号列を割り当てる。
    「その選択肢を選んだ」と記録するのではなく、「条件に合致すると判断された」
    ことを通過判定と同じ形で表すための便宜であり、`inferred` フラグで区別する。
    """
    groups = batches(list(persona_uuids), screening.batch_size)
    result = InferResult(batches_total=len(groups))

    if not groups:
        return result

    concurrency = max(1, model.concurrency)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(judge_batch, group, personas, screening, client, model)
            for group in groups
        ]
        for group, future in zip(groups, futures, strict=True):
            batch = _result_or_failure(group, future)
            if batch.failed:
                result.batches_failed += 1
                result.errors.append(batch.error or "")
            else:
                result.batches_ok += 1
            result.input_tokens += batch.input_tokens or 0
            result.output_tokens += batch.output_tokens or 0
            result.budget_escalated += int(batch.budget_escalated)
            result.budget_exhausted += int(batch.budget_exhausted)
            _collect(survey_id, batch, result)
            if progress is not None:
                # 単位はバッチだが、報せる形は本調査と同じ（`ProgressUpdate`）。
                # ここだけ整数を渡していたため、CLI の進捗表示が infer モードで落ちていた。
                progress(
                    ProgressUpdate(
                        done=result.batches_ok + result.batches_failed,
                        total=result.batches_total,
                        failed=result.batches_failed,
                    )
                )

    return result


def _result_or_failure(group: Sequence[str], future) -> InferBatchResult:
    """1バッチの結果を受け取る。想定外の例外もバッチの失敗として扱う。

    `judge_batch()` が吸収するのは `LLMError` だけ。ここで受けないと、それ以外の例外
    （`personas` に無い uuid の `KeyError` など）が `run_inference()` を突き抜けて
    `screen_survey()` まで伝播し、**完了済みバッチの判定結果も書き出される前に失われる**
    （書き出しは全バッチを受け取ってから `merge_upsert` するため）。
    セッション単位で失敗を吸収する `run.executor._collect()` と同じ扱いにする。

    `error` が入るので `failed` が真になり、`_collect()` が `judge_error` を立てて
    通過判定から外す。行は残るので、再実行時に `read_screener_codes()` が
    未判定として扱い聞き直される。
    """
    try:
        return future.result()
    except Exception as exc:  # noqa: BLE001 - 1バッチの失敗で判定全体を止めない
        return InferBatchResult(
            persona_uuids=tuple(group),
            passed=(),
            raw="",
            out_of_range=False,
            latency_ms=0,
            input_tokens=None,
            output_tokens=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def _collect(survey_id: str, batch: InferBatchResult, result: InferResult) -> None:
    """1バッチの判定を、通過判定と `screener_responses` の形に落とす。

    判定は条件ごとではなく候補1人につき1回なので、**1人あたり1レコード**にする
    （`question_id` は予約値 `INFER_QUESTION_ID`）。条件の数だけ行を作ると、
    聞いてもいない設問に答えたように見えてしまう。

    **呼び出しが失敗したバッチの候補は通過判定に載せない。** 呼べていないのだから
    条件に合わなかったのではなく、判定できていない。空の番号列を判定結果として扱うと、
    通信の失敗がそのまま「非通過」として確定し、実インシデンス0%と見分けがつかなくなる。
    行自体は `judge_error` を立てて残す（`answer_raw` にエラー文が入る）。
    """
    passed = set(batch.passed)
    for persona_uuid in batch.persona_uuids:
        codes = (INFER_PASSED_CODE,) if persona_uuid in passed else ()
        if not batch.failed:
            result.codes[persona_uuid] = {INFER_QUESTION_ID: codes}
        result.records.append(_to_record(survey_id, persona_uuid, codes, batch))


def _to_record(
    survey_id: str,
    persona_uuid: str,
    codes: tuple[int, ...],
    batch: InferBatchResult,
) -> ResponseRecord:
    """判定結果を `screener_responses` の1行にする。

    `inferred` を必ず立てる。これが無いと `ask` の実回答と区別できなくなる。

    呼び出しが失敗した行には `judge_error` を立てる。`parse_error`（返ってきたが番号が
    取れなかった＝非通過）とは意味が違い、こちらは**未判定**。この目印が無いと、
    再実行時に判定済みとして読み戻され、二度と聞き直されない。
    """
    record_flags = [flag_names.INFERRED]
    if batch.failed:
        record_flags.append(flag_names.JUDGE_ERROR)
    elif batch.out_of_range:
        record_flags.append(flag_names.OUT_OF_RANGE)

    return ResponseRecord(
        survey_id=survey_id,
        persona_uuid=persona_uuid,
        stimulus_id="",
        question_id=INFER_QUESTION_ID,
        sequence=0,
        answer_raw=batch.error or batch.raw,
        answer_codes=list(codes),
        answer_text=None,
        # 判定は理由を書かせない（`questions[].reasoning` は本調査の設問の設定）。
        answer_reasoning=None,
        # 判定は選択肢を提示していないので、提示順という概念が無い。
        options_order=[],
        flags=record_flags,
        latency_ms=batch.latency_ms,
        input_tokens=batch.input_tokens,
        output_tokens=batch.output_tokens,
        attempt=1,
        ts=time.time(),
        model_version=batch.model_version,
    )


def infer_session_count(candidates: int, screening: ScreeningConfig) -> int:
    """判定に必要な呼び出し回数（§10.1 の見積もり）。"""
    if candidates <= 0 or screening.batch_size < 1:
        return 0
    return -(-candidates // screening.batch_size)
