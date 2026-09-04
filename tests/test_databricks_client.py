"""Databricks Model Serving アダプタの応答の解釈（`persona_sim.llm.databricks`）。

Spark も実エンドポイントも使わない。`WorkspaceClient` はダミーを注入する。
"""

from __future__ import annotations

import pytest

from persona_sim.llm.client import (
    ChatMessage,
    LLMError,
    OutputLimitExceeded,
    RetryableLLMError,
    StructuredOutputUnsupported,
)
from persona_sim.llm.databricks import DatabricksServingClient

#: issue 202607281042 で実際に観測された形。Gemini 系は content part の配列を返し、
#: text part に `thoughtSignature` が同居する。
_THOUGHT_SIGNATURE = "AY89a1/Gijbk8aRzGa6GTELZ7YImNhXRs+Yvf1wQDUQiI1fAQusaYgh3cSe"
_ANSWER = "翌日の現場や家族のことを考えると、こうした強い酒を好んで飲むことはないですね。"


class _FakeApiClient:
    def __init__(self, body):
        self._body = body
        self.calls = []

    def do(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._body


class _FakeWorkspaceClient:
    def __init__(self, body):
        self.api_client = _FakeApiClient(body)


def _complete(content, *, usage=None, model=None, finish_reason=None, max_tokens=500):
    choice: dict = {"message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    body = {
        "choices": [choice],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 34},
        "model": model or "databricks-gemini-3-5-flash-lite",
    }
    client = DatabricksServingClient("endpoint", workspace_client=_FakeWorkspaceClient(body))
    return client.complete([ChatMessage(role="user", content="設問")], max_tokens=max_tokens)


def test_plain_string_content_passes_through():
    assert _complete(_ANSWER).text == _ANSWER


def test_content_blocks_yield_only_the_body():
    """本文だけを取り出す。リストの repr も thoughtSignature も混ぜない。"""
    completion = _complete(
        [{"type": "text", "text": _ANSWER, "thoughtSignature": _THOUGHT_SIGNATURE}]
    )

    assert completion.text == _ANSWER
    assert _THOUGHT_SIGNATURE not in completion.text
    assert "[{" not in completion.text
    assert "thoughtSignature" not in completion.text


def test_multiple_text_blocks_are_joined_in_order():
    completion = _complete(
        [
            {"type": "text", "text": "前半。"},
            {"type": "text", "text": "後半。"},
        ]
    )
    assert completion.text == "前半。後半。"


def test_non_text_blocks_are_dropped():
    completion = _complete(
        [
            {"type": "thought", "thoughtSignature": _THOUGHT_SIGNATURE},
            {"type": "text", "text": _ANSWER},
        ]
    )
    assert completion.text == _ANSWER


def test_single_content_block_as_object():
    assert _complete({"type": "text", "text": _ANSWER}).text == _ANSWER


def test_no_text_block_fails_loudly():
    """repr にフォールバックしない。黙って劣化した経路に落ちるほうが害が大きい。"""
    with pytest.raises(LLMError) as exc:
        _complete([{"type": "thought", "thoughtSignature": _THOUGHT_SIGNATURE}])

    # 診断に type は出すが、本文（thoughtSignature を含みうる）は載せない。
    assert _THOUGHT_SIGNATURE not in str(exc.value)


def test_missing_content_still_fails():
    with pytest.raises(LLMError):
        _complete(None)


def test_usage_and_model_are_carried():
    completion = _complete(
        _ANSWER,
        usage={"prompt_tokens": 120, "completion_tokens": 34},
        model="databricks-gemini-3-5-flash-lite",
    )

    assert completion.input_tokens == 120
    assert completion.output_tokens == 34
    assert completion.model_version == "databricks-gemini-3-5-flash-lite"
    assert completion.latency_ms >= 0


# --------------------------------------------------------------------------- #
# 出力長超過（`finish_reason: "length"`）
#
# 400 で返るとは限らない。200 で途中までの本文を返すエンドポイントでは、これを見ていないと
# 切り詰められた回答が無言で `answer_text` に流れる。
# --------------------------------------------------------------------------- #


def test_finish_reason_length_becomes_an_output_limit():
    """切り詰めを普通の完了として扱うと、短い回答との区別が永久につかなくなる。"""
    with pytest.raises(OutputLimitExceeded) as exc:
        _complete("途中まで書い", finish_reason="length", max_tokens=8)

    assert exc.value.max_tokens == 8
    assert exc.value.partial_text == "途中まで書い"


def test_finish_reason_length_with_no_content_reports_the_limit_not_an_empty_body():
    """推論だけで予算を使い切ると本文が空で返る。「content が空」では原因を取り違える。"""
    with pytest.raises(OutputLimitExceeded) as exc:
        _complete(None, finish_reason="length")

    assert exc.value.partial_text == ""


def test_finish_reason_length_with_only_thought_parts_still_reports_the_limit():
    """本文が1文字も取れない content part でも、上限に達したことは伝える。"""
    with pytest.raises(OutputLimitExceeded):
        _complete(
            [{"type": "thought", "thoughtSignature": _THOUGHT_SIGNATURE}],
            finish_reason="length",
        )


def test_finish_reason_stop_is_a_normal_completion():
    """過剰発火の番人。正常に終わった応答まで引き上げ再送すると、費用が跳ね上がる。"""
    assert _complete(_ANSWER, finish_reason="stop").text == _ANSWER


def test_a_response_without_finish_reason_is_a_normal_completion():
    """`finish_reason` を返さないエンドポイントでも従来どおり動くこと。"""
    assert _complete(_ANSWER).text == _ANSWER


def test_multimodal_user_message_payload():
    body = {
        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    fake_ws = _FakeWorkspaceClient(body)
    client = DatabricksServingClient("endpoint", workspace_client=fake_ws)
    multimodal_content = [
        {"type": "text", "text": "説明"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
    ]
    client.complete([ChatMessage(role="user", content=multimodal_content)], max_tokens=100)

    sent_body = fake_ws.api_client.calls[0][2]
    assert sent_body["messages"][0]["content"] == multimodal_content
    # サンプリングパラメータは送らない（エンドポイント既定に任せる）。
    assert "temperature" not in sent_body


# --------------------------------------------------------------------------- #
# 再試行の計測（`RetryStats`）
#
# `latency_ms` にはバックオフの待ち時間も含まれるので、これを別に数えていないと
# 「モデルの応答が遅い」のか「429 を食らって眠っていた」のかを後から分けられない。
# --------------------------------------------------------------------------- #


class _Boom(Exception):
    """SDK 例外の代わり。`status_code` で再試行の可否が決まる。

    `message` を渡せるのは、400 のうち出力長超過・構造化出力の非対応・本物の不正リクエストを
    分ける手がかりが文面しかないため。
    """

    def __init__(self, status_code=None, message=None):
        super().__init__(message if message is not None else f"status={status_code}")
        self.status_code = status_code


class _FlakyApiClient:
    """指定回数だけ失敗してから成功する。"""

    def __init__(self, failures, *, status_code=429, body=None, message=None):
        self._remaining = failures
        self._status_code = status_code
        self._message = message
        self._body = body or {
            "choices": [{"message": {"role": "assistant", "content": "1"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        self.calls = 0

    def do(self, method, path, body=None):
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise _Boom(self._status_code, self._message)
        return self._body


def _flaky_client(failures, *, status_code=429, max_attempts=5, message=None):
    api = _FlakyApiClient(failures, status_code=status_code, message=message)
    workspace = type("_WS", (), {"api_client": api})()
    slept = []
    client = DatabricksServingClient(
        "endpoint",
        workspace_client=workspace,
        max_attempts=max_attempts,
        sleep=slept.append,  # 実時間を使わない
    )
    return client, api, slept


def _ask(client):
    return client.complete([ChatMessage(role="user", content="設問")], max_tokens=8)


def test_a_clean_call_counts_no_retries():
    client, _, slept = _flaky_client(0)
    _ask(client)

    stats = client.retry_stats()
    assert (stats.calls, stats.retried_calls, stats.retries) == (1, 0, 0)
    assert stats.backoff_seconds == 0.0
    assert stats.reasons == {}
    assert slept == []


def test_retries_and_backoff_are_counted():
    client, api, slept = _flaky_client(2)
    _ask(client)

    stats = client.retry_stats()
    assert api.calls == 3
    assert (stats.calls, stats.retried_calls, stats.retries) == (1, 1, 2)
    # 実際に眠った時間と一致すること（バックオフはジッタで揺れるので合計で見る）。
    assert stats.backoff_seconds == pytest.approx(sum(slept))
    assert stats.backoff_seconds > 0
    assert stats.reasons == {"429": 2}


def test_stats_accumulate_across_calls():
    client, _, _ = _flaky_client(1)
    _ask(client)
    _ask(client)  # 2回目は最初から成功する

    stats = client.retry_stats()
    assert (stats.calls, stats.retried_calls, stats.retries) == (2, 1, 1)


def test_exhausted_retries_are_still_counted():
    """全滅した呼び出しこそ再試行が多い。例外で抜けても数え落とさない。"""
    client, _, _ = _flaky_client(99, max_attempts=5)

    with pytest.raises(LLMError):
        _ask(client)

    stats = client.retry_stats()
    assert (stats.calls, stats.retried_calls, stats.retries) == (1, 1, 4)
    # 内訳は失敗した試行の数なので、再試行回数より1多い（初回の失敗を含む）。
    assert stats.reasons == {"429": 5}


def test_a_non_retryable_failure_is_counted_once():
    client, api, _ = _flaky_client(99, status_code=400)

    with pytest.raises(LLMError):
        _ask(client)

    stats = client.retry_stats()
    assert api.calls == 1
    assert (stats.calls, stats.retried_calls, stats.retries) == (1, 0, 0)
    assert stats.reasons == {"400": 1}


# --------------------------------------------------------------------------- #
# 400 の中身の切り分け
#
# 出力長超過・構造化出力の非対応・本物の不正リクエストは、どれも 400 で返るが打つ手が違う
# （枠を広げる／正規表現へ落とす／直す）。文面で分ける以外に手がかりが無い。
# --------------------------------------------------------------------------- #

#: 本番で実際に観測された文面（`docs/issues/20260807001.md`）。
_OUTPUT_LIMIT_MESSAGE = (
    "Could not finish the message because max_tokens or model output limit was reached. "
    "Please try again with higher max_tokens."
)


def test_an_output_limit_400_is_classified_apart_from_a_bad_request():
    """同じ 400 でも、こちらは枠を広げれば通る。`LLMError` のままだと1回で捨ててしまう。"""
    client, api, slept = _flaky_client(99, status_code=400, message=_OUTPUT_LIMIT_MESSAGE)

    with pytest.raises(OutputLimitExceeded) as exc:
        _ask(client)

    assert exc.value.max_tokens == 8  # `_ask` が送った予算
    # 出力長超過はバックオフでは直らない。待たずに呼び出し側へ返す。
    assert (api.calls, slept) == (1, [])


def test_a_structured_output_400_is_still_a_structured_output_rejection():
    """出力長超過の判定が横取りしないこと。取り違えると調査全体が正規表現パースに落ちる。"""
    api = _FlakyApiClient(99, status_code=400, message="response_format is not supported")
    workspace = type("_WS", (), {"api_client": api})()
    client = DatabricksServingClient("endpoint", workspace_client=workspace, sleep=lambda _: None)

    with pytest.raises(StructuredOutputUnsupported):
        client.complete(
            [ChatMessage(role="user", content="設問")],
            max_tokens=8,
            response_format={"type": "json_schema"},
        )


def test_a_retryable_failure_is_never_read_as_an_output_limit():
    """429 の本文にたまたま max_tokens が出ても、待てば直るものは待つ。"""
    client, api, _ = _flaky_client(1, status_code=429, message=_OUTPUT_LIMIT_MESSAGE)

    _ask(client)

    assert api.calls == 2


#: 値そのものが不正だと言っている 400。枠を広げても直らない。
_INVALID_MAX_TOKENS_MESSAGES = (
    "max_tokens must be less than or equal to 4096",
    "Invalid value for max_tokens: expected integer",
    "max_tokens is out of range",
)


@pytest.mark.parametrize("message", _INVALID_MAX_TOKENS_MESSAGES)
def test_an_invalid_max_tokens_400_is_not_an_output_limit(message):
    """`max_tokens` という語だけで出力長超過と決めない。

    値の不正を `OutputLimitExceeded` にすると `complete_within_budget` が
    **さらに大きい値**で2回投げ直し（必ず失敗する）、1呼び出しが3倍に膨らむ。
    そのうえ「`model.max_tokens` を見直すこと」と、**下げるべき場面で上げるよう**案内される。
    """
    client, api, _ = _flaky_client(99, status_code=400, message=message)

    with pytest.raises(LLMError) as exc:
        _ask(client)

    assert not isinstance(exc.value, OutputLimitExceeded)
    # 再試行しても直らないので1回で返す。
    assert api.calls == 1


def test_the_observed_output_limit_message_still_classifies_as_one():
    """絞り込みで本物を取りこぼさないこと（`docs/issues/20260807001.md` の実文面）。"""
    client, _, _ = _flaky_client(99, status_code=400, message=_OUTPUT_LIMIT_MESSAGE)

    with pytest.raises(OutputLimitExceeded):
        _ask(client)


def test_reasons_keep_status_codes_apart():
    """429（並列度を下げる）と 503（待って再実行）は打つ手が違う。まとめない。"""
    api = _FlakyApiClient(0)
    errors = [_Boom(429), _Boom(503), None]

    def do(method, path, body=None):
        error = errors.pop(0)
        if error is not None:
            raise error
        return api._body

    workspace = type("_WS", (), {"api_client": type("_A", (), {"do": staticmethod(do)})()})()
    client = DatabricksServingClient(
        "endpoint", workspace_client=workspace, sleep=lambda _: None
    )
    _ask(client)

    assert client.retry_stats().reasons == {"429": 1, "503": 1}


def test_retried_rate_is_the_share_of_affected_calls():
    client, _, _ = _flaky_client(1)
    _ask(client)
    _ask(client)
    _ask(client)
    _ask(client)

    assert client.retry_stats().retried_rate == 0.25


# --------------------------------------------------------------------------- #
# SDK 内部の再試行との住み分け（`docs/issues/20260807002.md` M3）
#
# `databricks-sdk` は 429・503・接続エラーを内部で投げ直し、予算（既定300秒）を使い切ると
# `TimeoutError(...) from 元の例外` を投げる。包まれたままだと status_code が消えて
# 「タイムアウト」としか読めず、レート制限で詰まっていたことがどこにも残らない。
# --------------------------------------------------------------------------- #


class _SdkExhaustedApiClient:
    """SDK が再試行予算を使い切ったときの形を再現する。"""

    def __init__(self, cause):
        self._cause = cause
        self.calls = 0

    def do(self, method, path, body=None):
        self.calls += 1
        raise TimeoutError("Timed out after 0:05:00") from self._cause


def _exhausted_client(cause):
    api = _SdkExhaustedApiClient(cause)
    workspace = type("_WS", (), {"api_client": api})()
    slept = []
    client = DatabricksServingClient(
        "endpoint", workspace_client=workspace, sleep=slept.append
    )
    return client, api, slept


def test_an_exhausted_sdk_retry_is_reported_as_the_original_failure():
    """429 で詰まっていたことを、文面にも理由の内訳にも残す。

    包んだままだと `TimeoutError: Timed out after 0:05:00` としか読めない。
    `W_ENDPOINT_RETRY` は 429 を数えられない（SDK が吸収する）ので、
    **この文面がレート制限だと分かる唯一の場所**になる。
    """
    client, _, _ = _exhausted_client(_Boom(429))

    with pytest.raises(RetryableLLMError) as excinfo:
        _ask(client)

    message = str(excinfo.value)
    assert "429" in message
    assert "Timed out" not in message  # 包みの文面で置き換えない
    assert "concurrency" in message  # 打ち手を添えること
    assert client.retry_stats().reasons == {"429": 1}


def test_an_exhausted_sdk_retry_is_not_retried_again():
    """SDK が既に300秒使った後なので、こちらで投げ直さない（二重に待たない）。"""
    client, api, slept = _exhausted_client(_Boom(429))

    with pytest.raises(RetryableLLMError):
        _ask(client)

    assert api.calls == 1
    assert slept == []
    stats = client.retry_stats()
    assert (stats.calls, stats.retried_calls, stats.retries) == (1, 0, 0)


def test_a_wrapped_connection_error_is_also_reported_as_itself():
    """接続エラーも SDK の担当。開かないと `TimeoutError` に化ける。"""
    client, api, _ = _exhausted_client(ConnectionError("cannot connect"))

    with pytest.raises(RetryableLLMError) as excinfo:
        _ask(client)

    assert "cannot connect" in str(excinfo.value)
    # レート制限ではないので並列度の話は出さない。
    assert "concurrency" not in str(excinfo.value)
    assert api.calls == 1
    assert client.retry_stats().reasons == {"ConnectionError": 1}


def test_a_bare_timeout_without_a_cause_is_left_alone():
    """包みでない `TimeoutError` を勝手に再試行可へ格上げしない。"""
    api = _SdkExhaustedApiClient(None)
    workspace = type("_WS", (), {"api_client": api})()
    client = DatabricksServingClient("endpoint", workspace_client=workspace)

    with pytest.raises(LLMError) as excinfo:
        _ask(client)

    assert not isinstance(excinfo.value, RetryableLLMError)
    assert api.calls == 1


def test_the_default_attempt_count_is_three():
    """自前の層が担当するのは 5xx だけ。3回で足りる（早く E5 へ届かせる）。"""
    api = _FlakyApiClient(99, status_code=500)
    workspace = type("_WS", (), {"api_client": api})()
    client = DatabricksServingClient(
        "endpoint", workspace_client=workspace, sleep=lambda _: None
    )

    with pytest.raises(LLMError):
        _ask(client)

    assert api.calls == 3
