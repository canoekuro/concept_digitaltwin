"""出力予算（`max_tokens`）の引き上げ再送。

エンドポイントが「出力が上限に達した」と言ってきたとき、同じ枠で投げ直しても意味が薄い。
**予算を広げて送り直す**のがこのモジュールの責務。

再試行の軸を混ぜないこと。この系には独立した再試行が5つある。

- **SDK 内部の再試行（`databricks-sdk`）— 429・503・接続エラー。既定300秒まで自前で
  投げ直す。こちらのコードからは見えず、`RetryStats` にも数えられない**
- 伝送層のバックオフ（`llm/databricks.py`）— 5xx（503を除く）。待てば直る
- 出力予算の引き上げ（ここ）— 出力が長すぎた。枠を広げれば通る
- パース再送（`run/session.py` の `PARSE_MAX_ATTEMPTS`）— 番号が取れなかった
- オーバーサンプル再試行（`panel/screening.py`）— 通過者が足りなかった

先頭の2つは**担当が分かれている**（どちらが投げ直すかは失敗の種類で決まる）ので、
待ち時間は掛け算にならない。SDK が予算を使い切った失敗を伝送層は投げ直さない。
詳細は `llm/databricks.py` のモジュール docstring の表。

送信そのものは呼び出し側の closure に委ねる。`_ask` は構造化出力のフォールバックを
内側に持っており、クライアントをここへ渡すと2つの方針が絡んで組み合わせが増えるため。
ここは予算の決め方だけを持つ。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from persona_sim.llm.client import Completion, OutputLimitExceeded

#: 予算超過のたびに元の `max_tokens` へ掛ける倍率。初回は設定値そのままで送る。
#: 倍率を細かく刻まないのは、推論トークンを含めて足りない場合に1.5倍程度では届かず、
#: 刻んだぶんだけ無駄な呼び出しが増えるため。
OUTPUT_LIMIT_MULTIPLIERS = (4, 16)

#: 引き上げの天井。これ以上は広げない。
#: 青天井にすると、本当に終わらないプロンプト（設問の作り間違い等）で延々とトークンを
#: 使い続ける。上限で止めてフラグを立て、設定を直す判断を人に返す。
OUTPUT_LIMIT_CEILING = 4096


@dataclass(frozen=True)
class BudgetOutcome:
    """予算超過に対して何をしたか。

    `escalated` と `exhausted` は排他ではない。広げたうえで天井まで使い切れば両方立つ。
    """

    #: 1回でも予算を引き上げたか。実行メタデータの集計対象（設定を見直す材料）。
    escalated: bool = False
    #: 天井まで広げても通らなかったか。**回答は欠落か切り詰めになっている。**
    exhausted: bool = False
    #: 最後に送った予算。
    final_max_tokens: int = 0
    #: 使い切ったときに得られていた本文。`exhausted` のときだけ意味を持つ。
    partial_text: str = ""
    #: 全試行を通した実測。使い切って `Completion` を組み立てるときに使う。
    latency_ms: int = 0


def budgets(max_tokens: int) -> tuple[int, ...]:
    """試す予算の並び。先頭は必ず設定値そのもの。

    天井で頭打ちにしたあと重複を畳む。設定値が既に天井以上なら1回しか試さない
    （同じ枠で投げ直すだけの呼び出しを作らないため）。
    """
    values = [max_tokens]
    for multiplier in OUTPUT_LIMIT_MULTIPLIERS:
        raised = min(max_tokens * multiplier, OUTPUT_LIMIT_CEILING)
        if raised > values[-1]:
            values.append(raised)
    return tuple(values)


def complete_within_budget(
    send: Callable[[int], Completion], *, max_tokens: int
) -> tuple[Completion | None, BudgetOutcome]:
    """`send(max_tokens)` を、出力長超過なら予算を広げて呼び直す。

    通れば `(Completion, outcome)`。天井まで広げても通らなければ `(None, outcome)` を返し、
    **送出はしない。** 1設問の予算超過で調査を止めないため。呼び出し側が `outcome` を見て、
    記録して続けるのか（本調査）判定失敗として残すのか（`infer`）を決める。

    `OutputLimitExceeded` 以外の例外はそのまま素通りさせる。伝送層のバックオフや
    構造化出力の非対応はここの関心事ではない。
    """
    started = time.monotonic()
    plan = budgets(max_tokens)
    last_error: OutputLimitExceeded | None = None

    for index, budget in enumerate(plan):
        try:
            completion = send(budget)
        except OutputLimitExceeded as exc:
            last_error = exc
            continue

        return completion, BudgetOutcome(
            escalated=index > 0,
            final_max_tokens=budget,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    return None, BudgetOutcome(
        escalated=len(plan) > 1,
        exhausted=True,
        final_max_tokens=plan[-1],
        partial_text=last_error.partial_text if last_error else "",
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def output_budget_warning(*, escalated: int, exhausted: int) -> str | None:
    """引き上げが起きていたら警告文を返す。起きていなければ `None`。

    閾値を設けず1回でも出す。引き上げて通った回答にはフラグが立たないので、ここで出さないと
    「予算が足りていない」という兆候がどこにも残らない。頻度が低いことは無害の意味ではなく、
    次に設問を長くしたときに失敗へ変わる余地がある、ということ。

    本調査とスクリーニングで同じ文面を使う（`retry_warning` と同じ理由。どちらも同じ
    エンドポイントを同じ設定で叩くので、片方だけ出ると切り分けが片手落ちになる）。
    """
    if not escalated and not exhausted:
        return None

    detail = f"{escalated:,} 回で予算を広げて成功"
    if exhausted:
        detail += f"、{exhausted:,} 回は天井（{OUTPUT_LIMIT_CEILING:,}）まで広げても完了せず"

    return (
        f"[W_OUTPUT_LIMIT] 出力が `max_tokens` に達した呼び出しがあった（{detail}）。"
        + (
            "完了しなかったぶんは回答が欠落・切り詰めになり `output_limit` フラグが立っている。"
            if exhausted
            else ""
        )
        + "`model.max_tokens`（自由回答は `model.max_tokens_open`）を見直すこと"
    )
