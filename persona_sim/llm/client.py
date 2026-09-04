"""推論クライアントの契約。

実装（Databricks Model Serving / Fake）はこのプロトコルだけを満たせばよい。
呼び出し側（`persona_sim.run`）は具体的な SDK を知らない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from persona_sim.errors import PersonaSimError


class LLMError(PersonaSimError):
    """推論呼び出しの失敗。"""

    code = "LLM"


class RetryableLLMError(LLMError):
    """再試行して意味のある失敗（429・5xx・タイムアウト）。"""

    code = "LLM_RETRYABLE"


class StructuredOutputUnsupported(LLMError):
    """エンドポイントが構造化出力を受け付けない（§6.3 の auto/always 判定に使う）。"""

    code = "LLM_NO_STRUCTURED_OUTPUT"


class OutputLimitExceeded(LLMError):
    """出力が `max_tokens` かモデルの出力上限に達し、応答が完了しなかった。

    **リクエストの不正ではない。** 同じ入力でも出力長は揺れるので、予算を広げた再送に
    意味がある。`RetryableLLMError`（バックオフして同じ枠で投げ直す）とは打つ手が違うため
    分けている。まとめると、広げれば通る呼び出しを待ち時間だけ使って捨てることになる。

    `partial_text` は本文が途中まで返っていた場合の中身。予算を広げても通らなかったとき、
    生成できていた分を捨てずに記録するために持つ。
    """

    code = "LLM_OUTPUT_LIMIT"

    def __init__(
        self, message: str, *, max_tokens: int | None = None, partial_text: str = ""
    ) -> None:
        super().__init__(message)
        self.max_tokens = max_tokens
        self.partial_text = partial_text


@dataclass(frozen=True)
class ChatMessage:
    """1メッセージ。role は system / user / assistant。"""

    role: str
    content: str | list[dict[str, Any]]


@dataclass(frozen=True)
class Completion:
    """1回の呼び出しの結果。`responses` に記録する材料（§2.3）。"""

    text: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: エンドポイントが返したモデル識別子。実行メタデータの `model_version`（§9）。
    model_version: str | None = None


@dataclass(frozen=True)
class RetryStats:
    """伝送層での再試行の実績（§6.5）。

    **`Completion.latency_ms` にはバックオフの待ち時間も含まれる。** これを分けて数えないと、
    「モデルの応答が遅い」のか「眠っていた」のかが後から区別できない
    （`responses.attempt` はパース失敗の試行回数であって、伝送層の再試行ではない）。

    **数えられるのは 5xx（503を除く）だけ。** Databricks 経路では 429・503・接続エラーの
    再試行を `databricks-sdk` が内部で行い、こちらからは見えない
    （`llm/databricks.py` 冒頭の表）。SDK が予算を使い切った場合だけ、その1件が
    元のステータスで `reasons` に載る。**0 は「詰まっていない」の意味ではない。**
    """

    calls: int = 0
    #: 1回でも再試行した呼び出しの数。
    retried_calls: int = 0
    #: 再試行の総回数（初回は含まない）。
    retries: int = 0
    #: バックオフで眠った合計秒数。
    backoff_seconds: float = 0.0
    #: 理由の内訳。HTTP ステータスが取れればその値、無ければ例外クラス名。
    reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def retried_rate(self) -> float:
        """再試行を要した呼び出しの割合。呼び出しが無ければ 0.0。"""
        if self.calls <= 0:
            return 0.0
        return self.retried_calls / self.calls


@runtime_checkable
class SupportsRetryStats(Protocol):
    """再試行の実績を出せるクライアント。

    `LLMClient` 本体には入れない。実装が満たすべき最小の契約は `complete` と `describe`
    だけで、計測はあくまで付随情報だから。呼ぶ側は `isinstance` で判定する。
    """

    def retry_stats(self) -> RetryStats:
        """呼び出し開始からの累計を返す。"""
        ...


#: 再試行を警告に出す閾値。E3・E4（§11）と同じ 5%。
#: これを超えていたら、遅さの原因はモデルの応答ではなくエンドポイントの詰まり側にある。
RETRY_WARNING_THRESHOLD = 0.05


def retry_stats_of(client: object) -> RetryStats | None:
    """クライアントが出せるなら再試行の実績を返す。出せなければ `None`。

    テスト用のスタブや将来のアダプタが `complete` しか持たない場合でも、呼ぶ側が
    `isinstance` を書かずに済むようにする。
    """
    if isinstance(client, SupportsRetryStats):
        return client.retry_stats()
    return None


def retry_warning(
    stats: RetryStats | None, *, threshold: float = RETRY_WARNING_THRESHOLD
) -> str | None:
    """再試行が閾値を超えていたら警告文を返す。超えていなければ `None`。

    本調査とスクリーニングで同じ文面を使う（どちらも同じエンドポイントを同じ並列度で
    叩くので、片方だけ出ると原因の切り分けが片手落ちになる）。文面を1つにしておくのは、
    レイテンシだけ見ても「モデルが遅い」のか「眠っていた」のかを区別できないため
    ——`Completion.latency_ms` にはバックオフの待ち時間も含まれる。

    **出ないからといってレート制限が無かったとは読めない。** Databricks 経路では 429 を
    SDK が吸収するので `RetryStats` に届かず、この警告は事実上 5xx 専用になる
    （`RetryStats` の説明を参照）。レート制限で詰まり切った場合は、警告ではなく
    `RetryableLLMError` の文面として現れる。
    """
    if stats is None or stats.retried_rate <= threshold:
        return None

    reasons = "・".join(
        f"{reason} {count}回"
        for reason, count in sorted(stats.reasons.items(), key=lambda kv: -kv[1])
    )
    return (
        f"[W_ENDPOINT_RETRY] {stats.calls:,} 回の呼び出しのうち {stats.retried_calls:,} 回"
        f"（{stats.retried_rate:.1%}）で再試行が発生し、合計 {stats.retries:,} 回・"
        f"{stats.backoff_seconds:,.0f} 秒をバックオフに使った"
        + (f"（内訳: {reasons}）" if reasons else "")
        + "。エンドポイントが並列度を捌けていない可能性がある。"
        "`model.concurrency` を下げるか、エンドポイント側の割り当てを確認すること"
    )


@runtime_checkable
class LLMClient(Protocol):
    """推論エンドポイント。

    実装は失敗時に `RetryableLLMError`（再試行可）か `LLMError`（再試行しても無駄）を
    投げ分ける。呼び出し側はこの区別だけを見てバックオフする。
    """

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        """1ターン分を生成する。

        `temperature` などのサンプリングパラメータは**渡さない**。最新モデルでは
        指定が非推奨・無効化される傾向にあるため、エンドポイント既定に任せる。

        `response_format` を渡した実装が構造化出力を扱えない場合は
        `StructuredOutputUnsupported` を投げる。
        """
        ...

    def describe(self) -> str:
        """実行メタデータに残す識別文字列（資格情報を含めないこと）。"""
        ...
