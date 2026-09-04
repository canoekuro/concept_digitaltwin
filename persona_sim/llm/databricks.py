"""Databricks Model Serving 呼び出し（`SPEC_PHASE1.md` §6.5）。

`databricks-sdk` の `serving_endpoints.query` は `extra_params` が `Dict[str, str]` で
構造化出力の入れ子スキーマを渡せないため、OpenAI 互換の invocations エンドポイントを
`api_client.do` で直接叩く。

**認証は SDK の既定の認証チェーンに任せる。** Databricks のノートブック・調査・Apps では
環境から解決され、ローカルでは `DATABRICKS_HOST` / `DATABRICKS_TOKEN` を読む。
トークンやワークスペース URL をこのモジュールにも調査定義にも書かない。

## 再試行は2層あるが、掛け算ではなく担当が分かれている

`databricks-sdk` は**内部にも再試行を持っている**。`_BaseClient` が
`http_timeout_seconds`（既定60秒。1リクエストの socket タイムアウト）と
`retry_timeout_seconds`（既定300秒。内部再試行の総予算）を持つ。
何を投げ直すかは SDK 側の実装で決まっていて、こちらとは重ならない。

| 失敗 | SDK 側 | この層（`_invoke_with_backoff`） |
|---|---|---|
| 429 / 503 | `Retry-After` を尊重して再試行（300秒まで） | **到達しない** |
| 500 / 502 / 504 | 再試行しない | **ここが担当**（`max_attempts` 回） |
| 接続リセット・socket タイムアウト | 再試行（300秒まで） | **到達しない** |
| 400 系 | 再試行しない | 分類して送出 |

429 / 503 に必ず `retry_after_secs` が付くのは SDK 既定の `_RetryAfterCustomizer` で、
`retried()` はそれがあれば必ず投げ直す。だから**生の 429 がここへ出てくることはない**。

**予算を使い切ったときだけ、その事実がここへ来る。** SDK は
`TimeoutError("Timed out after 0:05:00") from 元の例外` を投げる。包まれたままだと
`status_code` が消えて「タイムアウト」としか読めなくなるので、`_unwrap_sdk_retry_timeout()`
で `__cause__` を開いてから分類する。**開いたものは再試行しない**——SDK が既に300秒
使い切った後なので、ここで投げ直すのは二重に待つだけ。

`retry_timeout_seconds` は触らない。SDK の `Retry-After` 尊重はこちらの指数バックオフより
正確だし、下げても `retried()` は諦める前に必ず1回 `Retry-After` 秒眠る実装なので得が無い。

**`RetryStats` に載るのは 5xx（503を除く）だけ。** 429・503・接続エラーの再試行は
SDK が吸収するので `run_metadata.json` の `model.endpoint_retries` には現れない。
予算を使い切った場合だけ、その1件が `reasons` に元のステータスで載る。
`llm/client.py::retry_warning` と `llm/budget.py` 冒頭にも同じ前提を書いてある。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from persona_sim.llm.client import (
    ChatMessage,
    Completion,
    LLMError,
    OutputLimitExceeded,
    RetryableLLMError,
    RetryStats,
    StructuredOutputUnsupported,
)
from persona_sim.panel.schema import DEFAULT_REQUEST_TIMEOUT_SEC

#: 再試行して意味のある SDK 例外（レート制限・一時的な障害）。
#:
#: **このうち 429 / 503 に当たるものは、SDK 経由では届かない**（モジュール冒頭の表）。
#: それでも消さない。`workspace_client` を注入する経路（テスト・将来のアダプタ）では
#: 素通りしてここへ来るし、SDK が 429 を customizer から外したときに、
#: 消していると**無言で1回で捨てる**側に倒れるため。安全側に残している。
_RETRYABLE_ERROR_NAMES = (
    "TooManyRequests",
    "RequestLimitExceeded",
    "TemporarilyUnavailable",
    "ResourceExhausted",
    "InternalError",
    "DeadlineExceeded",
    "OperationTimeout",
)

#: 構造化出力が受け付けられなかったと判断する手がかり。
_STRUCTURED_OUTPUT_HINTS = ("response_format", "json_schema", "structured output")

#: 出力が上限に達したと判断する手がかり。**打ち切られたことを示す語だけ**を並べる。
#:
#: `max_tokens` という語だけで判定すると、`max_tokens must be <= 4096` のような
#: **値そのものが不正**だという 400 まで拾ってしまう。すると `complete_within_budget` が
#: さらに大きい値で2回投げ直し（必ず失敗する）、1呼び出しが3倍に膨らんだ上に
#: 「`model.max_tokens` を見直すこと」と**下げるべき場面で上げるよう**案内される。
_OUTPUT_LIMIT_HINTS = ("reached", "output limit", "max output", "truncat")

#: 上の語に一致しても、**値の指定が不正**だと言っているなら出力長超過ではない。
#: 枠を広げても直らないので、`LLMError` として素直に失敗させる。
_INVALID_VALUE_HINTS = ("must be", "invalid", "expected", "out of range", "not allowed")

#: 応答が上限で打ち切られたことを表す `finish_reason`（OpenAI 互換）。
_LENGTH_FINISH_REASON = "length"

class DatabricksServingClient:
    """1つのサービングエンドポイントに対する呼び出し。

    一時障害はこのクラスの中で指数バックオフして吸収する（伝送層の関心事）。
    パース失敗によるリトライはセッション側が扱う（§6.3）。責務を混ぜない。

    **担当するのは 5xx（503を除く）だけ。** 429・503・接続エラーは SDK 内部の再試行が
    持っていく（モジュール冒頭の表）。`RetryStats` に載るのもこの層のぶんだけ。
    """

    def __init__(
        self,
        endpoint_name: str,
        *,
        workspace_client: Any | None = None,
        # 担当が 5xx だけなので3回で足りる。3回続くならエンドポイント側の障害で、
        # 回数を増やしても通らない。早く失敗させたほうが E5（連続失敗の検知、
        # `run/executor.py`）に早く届き、詰まったまま課金され続ける時間が短い。
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        request_timeout_sec: int = DEFAULT_REQUEST_TIMEOUT_SEC,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._endpoint_name = endpoint_name
        self._workspace_client = workspace_client
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._request_timeout_sec = request_timeout_sec
        self._sleep = sleep
        # ジッタは再現性の対象外（伝送層の待ち時間であって回答内容に影響しない）。
        self._rng = rng or random.Random()

        # `WorkspaceClient` の遅延生成を1回に保つ錠。並列実行では複数スレッドが
        # 同時に `_client` を触るので、無いと人数分だけ認証チェーンを叩く。
        # 統計用の錠とは分ける（用途の違うものを1本で兼ねない）。
        self._client_lock = threading.Lock()

        # 再試行の実績。並列実行では複数スレッドが同じクライアントを共有する（§6.5）。
        self._stats_lock = threading.Lock()
        self._calls = 0
        self._retried_calls = 0
        self._retries = 0
        # 名前を `_backoff_seconds`（1回ぶんの待ち時間を返すメソッド）とぶつけない。
        self._backoff_total = 0.0
        self._reasons: dict[str, int] = {}

    @property
    def _client(self) -> Any:
        if self._workspace_client is None:
            with self._client_lock:
                # 錠を取ってからもう一度見る。待っている間に他のスレッドが作り終えている。
                if self._workspace_client is None:
                    from databricks.sdk import WorkspaceClient
                    from databricks.sdk.core import Config

                    # タイムアウトは `Config` からしか渡せない（`api_client.do()` に引数が無い）。
                    # 既定値は SDK と同じなので挙動は変わらないが、何秒で切れるのかが
                    # 調査定義と実行メタデータから読めるようになる。
                    self._workspace_client = WorkspaceClient(
                        config=Config(http_timeout_seconds=self._request_timeout_sec)
                    )
        return self._workspace_client

    def describe(self) -> str:
        return f"databricks:{self._endpoint_name}"

    def retry_stats(self) -> RetryStats:
        """再試行の累計を返す（`SupportsRetryStats`）。"""
        with self._stats_lock:
            return RetryStats(
                calls=self._calls,
                retried_calls=self._retried_calls,
                retries=self._retries,
                backoff_seconds=self._backoff_total,
                reasons=dict(self._reasons),
            )

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        # サンプリングパラメータ（temperature 等）は載せない。エンドポイント既定に任せる。
        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": m.role,
                    "content": m.content if isinstance(m.content, (str, list)) else str(m.content),
                }
                for m in messages
            ],
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        started = time.monotonic()
        body = self._invoke_with_backoff(payload, structured=response_format is not None)
        latency_ms = int((time.monotonic() - started) * 1000)
        return _to_completion(body, latency_ms, max_tokens=max_tokens)

    # ----------------------------------------------------------------- #

    def _invoke_with_backoff(self, payload: dict[str, Any], *, structured: bool) -> dict[str, Any]:
        path = f"/serving-endpoints/{self._endpoint_name}/invocations"
        last_error: Exception | None = None

        attempts = 0
        slept = 0.0
        reasons: dict[str, int] = {}

        # 成功・失敗・送出のどれで抜けても計上する。`finally` に置くのは、
        # 例外で抜ける経路（構造化出力の非対応・再試行不能・連続失敗）を数え落とすと、
        # 「詰まっているのに統計上は綺麗」という最悪の見え方になるため。
        try:
            for attempt in range(1, self._max_attempts + 1):
                attempts = attempt
                try:
                    return self._client.api_client.do("POST", path, body=payload)
                except Exception as raw:  # SDK の例外階層に依存せず名前で判定する
                    # SDK が予算を使い切って包んだものは、先に元の失敗へ戻す。
                    # 以降の分類（理由キー・再試行の可否・400 の切り分け）は
                    # すべて開いた側を見る。
                    exc = _unwrap_sdk_retry_timeout(raw)
                    exhausted_by_sdk = exc is not raw

                    reason = _reason_key(exc)
                    reasons[reason] = reasons.get(reason, 0) + 1

                    # 構造化出力の判定より先に見る。誤分類のコストが非対称なため。
                    # 出力長超過を `StructuredOutputUnsupported` と取り違えると、
                    # `StructuredOutputState` は実行全体で共有される1個なので、調査まるごとが
                    # 正規表現パースに落ちる。逆向きの取り違えは1呼び出しで済む。
                    if _looks_like_output_limit(exc):
                        raise OutputLimitExceeded(
                            f"{self.describe()} の出力が上限に達した: {exc}",
                            max_tokens=payload.get("max_tokens"),
                        ) from exc
                    if structured and _looks_like_structured_output_rejection(exc):
                        raise StructuredOutputUnsupported(
                            f"{self.describe()} が構造化出力を受け付けなかった: {exc}"
                        ) from exc
                    # SDK が予算を使い切ったものは、ここで投げ直さない。
                    # 既に300秒を費やした後なので、二重に待つだけで通る見込みが無い。
                    # **再試行できない失敗ではない**ので `RetryableLLMError` にする
                    # （記録に残るコードを実態に合わせる）。
                    if exhausted_by_sdk:
                        raise RetryableLLMError(
                            f"{self.describe()} が {reason} で詰まり、"
                            f"SDK の再試行予算を使い切った: {exc}" + _exhaustion_hint(exc)
                        ) from raw
                    if not _is_retryable(exc):
                        raise LLMError(f"{self.describe()} の呼び出しに失敗: {exc}") from exc

                    last_error = exc
                    if attempt == self._max_attempts:
                        break
                    delay = self._backoff_seconds(attempt)
                    slept += delay
                    self._sleep(delay)

            raise RetryableLLMError(
                f"{self.describe()} が {self._max_attempts} 回連続で失敗: {last_error}"
            ) from last_error
        finally:
            self._record(attempts=attempts, slept=slept, reasons=reasons)

    def _record(self, *, attempts: int, slept: float, reasons: dict[str, int]) -> None:
        """1呼び出しぶんの実績をまとめて足す。

        再試行の回数は「余分に投げた回数」＝ `attempts - 1`。`reasons` は失敗した試行の
        内訳なので、全滅した呼び出しでは合計が再試行回数より1多くなる（初回の失敗を含むため）。
        """
        with self._stats_lock:
            self._calls += 1
            self._retries += max(0, attempts - 1)
            if attempts > 1:
                self._retried_calls += 1
            self._backoff_total += slept
            for reason, count in reasons.items():
                self._reasons[reason] = self._reasons.get(reason, 0) + count

    def _backoff_seconds(self, attempt: int) -> float:
        delay = min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)
        return delay * (0.5 + self._rng.random() / 2)


def _unwrap_sdk_retry_timeout(exc: Exception) -> Exception:
    """SDK が再試行予算を使い切って包んだ例外を、元の失敗に戻す。

    `databricks-sdk` の `retried()` は予算切れのとき
    `TimeoutError("Timed out after 0:05:00") from 元の例外` を投げる。包んだままだと
    `status_code` が消え、**最も知りたい失敗（429 で詰まっていた）が「タイムアウト」
    としか読めなくなる**。`run_metadata.json` の `reasons` にも `TimeoutError` としか残らない。

    開くのは1段だけ。`__cause__` を辿り続けると、元の失敗のさらに下にある
    無関係な例外まで拾いうる。
    """
    if isinstance(exc, TimeoutError) and isinstance(exc.__cause__, Exception):
        return exc.__cause__
    return exc


def _exhaustion_hint(exc: Exception) -> str:
    """予算切れがレート制限だったときの打ち手。

    `retry_warning()` の `W_ENDPOINT_RETRY` は 429 を数えられない（SDK が吸収するため）。
    **この文面が、レート制限だと分かる唯一の場所になる。**
    """
    status = getattr(exc, "status_code", None)
    if status in (429, 503):
        return "。`model.concurrency` を下げるか、エンドポイント側の割り当てを確認すること"
    return ""


def _reason_key(exc: Exception) -> str:
    """内訳に使う短いキー。

    HTTP ステータスが取れればそれを使う。429（レート制限）と 503（一時的な過負荷）は
    打つ手が違う（前者は並列度、後者は待って再実行）ので、まとめてはいけない。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return str(status)
    return type(exc).__name__


def _is_retryable(exc: Exception) -> bool:
    if type(exc).__name__ in _RETRYABLE_ERROR_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    return status == 429 or (isinstance(status, int) and 500 <= status < 600)


def _looks_like_output_limit(exc: Exception) -> bool:
    """出力が上限に達したのか、他の不正リクエストなのかを見分ける。

    **これは補助の経路。** 主経路は `_to_completion()` の
    `finish_reason == "length"` 判定で、あちらは根拠が明確で誤検知しない。
    ここは 200 ではなく 400 を返すエンドポイント向けの手当てなので、
    文言からの推測になるぶん慎重に絞る。

    再試行可能なもの（429・5xx）を先に除く。あちらは待てば直るのであって、枠を広げる話ではない。
    そのうえで「打ち切られた」と言っている場合だけ真にし、
    **「値が不正」と言っている場合は除く**（枠を広げても直らない）。
    """
    if _is_retryable(exc):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not 400 <= status < 500:
        return False
    message = str(exc).lower()
    if any(hint in message for hint in _INVALID_VALUE_HINTS):
        return False
    return any(hint in message for hint in _OUTPUT_LIMIT_HINTS)


def _looks_like_structured_output_rejection(exc: Exception) -> bool:
    """構造化出力そのものを拒否されたのか、他の不正リクエストなのかを見分ける。

    見分けを誤ると、本来直すべきリクエスト不正を「構造化出力が使えない」と誤認して
    黙って劣化した経路に落ちる。そのため 400 系かつ文言に手がかりがある場合に限る。
    """
    if _is_retryable(exc):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not 400 <= status < 500:
        return False
    message = str(exc).lower()
    return any(hint in message for hint in _STRUCTURED_OUTPUT_HINTS)


def _content_to_text(content: Any) -> str:
    """`message.content` から本文だけを取り出す。

    OpenAI 互換の `content` は**文字列とは限らない**。Gemini 系のエンドポイントは
    content part の配列 `[{"type": "text", "text": "…", "thoughtSignature": "…"}]`
    を返す。これを `str()` に通すと Python のリテラル表記が `Completion.text` に入り、
    `answer_raw` と `answer_text` にそのまま流れる。自由回答が読めなくなるだけでなく、
    構造化出力が使えず正規表現に落ちた選択式では、`thoughtSignature` の base64 に
    含まれる数字を選択肢番号として拾う。

    ここは推論クライアントの契約（`Completion.text` はプレーンテキスト）を守る場所。
    パーサ側で防御すると `answer_raw` は壊れたまま残り、スクリーニングの判定経路にも
    同じ穴が残る。

    text 以外の part（thought / reasoning）は捨てる。1文字も取り出せなければ
    repr にフォールバックせず失敗させる（`AGENTS.md`: 黙って劣化した経路に落ちない）。
    """
    if isinstance(content, str):
        return content

    parts = content if isinstance(content, list) else [content]
    texts = []
    for part in parts:
        text = _part_text(part)
        if text:
            texts.append(text)

    if not texts:
        # 本文そのものは載せない（thoughtSignature を含みうるため）。
        seen = [part.get("type") for part in parts if isinstance(part, dict)]
        raise LLMError(
            f"応答の message.content から本文を取り出せない: "
            f"{type(content).__name__} / part の type={seen}"
        )
    return "".join(texts)


def _part_text(part: Any) -> str:
    """content part 1件の本文。text part でなければ空文字を返す。"""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    part_type = part.get("type")
    if part_type is not None and part_type != "text":
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _partial_text(content: Any) -> str:
    """途中まで返っていた本文。取り出せなければ空文字。

    `_content_to_text` は本文を1文字も取れないと送出する（完成した応答では黙って劣化させない
    ため）。打ち切られた応答では「本文がまだ無い」のが正常なので、ここでは空文字に落とす。
    """
    if content is None:
        return ""
    try:
        return _content_to_text(content)
    except LLMError:
        return ""


def _to_completion(body: Any, latency_ms: int, *, max_tokens: int | None = None) -> Completion:
    if not isinstance(body, dict):
        raise LLMError(f"応答が JSON オブジェクトではない: {type(body).__name__}")

    choices = body.get("choices")
    if not choices:
        raise LLMError(f"応答に choices が無い: {sorted(body)}")

    message = choices[0].get("message") or {}
    content = message.get("content")

    # `content is None` より先に見る。推論だけで予算を使い切った応答は 200 で返りつつ本文が
    # 空になるので、後に置くと「message.content が空」という原因を取り違えた失敗になる。
    # ここを見ていないと、切り詰められた回答が無言で `answer_text` に流れる経路も残る。
    if choices[0].get("finish_reason") == _LENGTH_FINISH_REASON:
        raise OutputLimitExceeded(
            f"応答が出力上限で打ち切られた（finish_reason={_LENGTH_FINISH_REASON}）",
            max_tokens=max_tokens,
            partial_text=_partial_text(content),
        )

    if content is None:
        raise LLMError("応答の message.content が空")

    usage = body.get("usage") or {}
    return Completion(
        text=_content_to_text(content),
        latency_ms=latency_ms,
        input_tokens=_as_optional_int(usage.get("prompt_tokens")),
        output_tokens=_as_optional_int(usage.get("completion_tokens")),
        model_version=body.get("model"),
    )


def _as_optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
