"""決定論的なダミークライアント。

実エンドポイント無しでパイプラインを端から端まで動かすために使う。
プロンプトを読んで選択肢の数を数え、ハッシュから回答を決めるので、
同じ入力には必ず同じ答えを返す。

品質フラグ（§8）や E3〜E5 の経路を検証できるよう、拒否・パース不能・失敗を
**決定論的に**注入できる。乱数を使うとテストが不安定になるため、注入の判定も
プロンプトのハッシュで行う。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from persona_sim.llm.client import (
    ChatMessage,
    Completion,
    LLMError,
    RetryableLLMError,
    RetryStats,
)

#: 選択肢行（"1. ぜひ購入したい"）。
_OPTION_LINE = re.compile(r"^\s*(\d+)\.\s+\S", re.MULTILINE)

#: 自由回答として返す文面。ペルソナごとに散らすためのバリエーション。
_OPEN_ANSWERS = (
    "価格に対して中身が見合っていると感じたため。",
    "普段このカテゴリを飲まないので、あまり関心が持てなかった。",
    "味の想像がつきやすく、試してみたいと思った。",
    "似た商品が既にあるので、目新しさは感じなかった。",
)

_REFUSAL_TEXT = "申し訳ありませんが、その質問にはお答えできません。"
_UNPARSEABLE_TEXT = "うーん、どちらとも言い難いところですね。"


class FakeClient:
    """決定論的な回答を返すクライアント。

    各 `*_rate` は 0.0〜1.0 で、プロンプトのハッシュを 0〜1 に写した値と比較して
    注入するかどうかを決める。同じプロンプトなら毎回同じ判定になる。
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        refusal_rate: float = 0.0,
        unparseable_rate: float = 0.0,
        failure_rate: float = 0.0,
        retryable_failures: bool = True,
        latency_ms: int = 1,
    ) -> None:
        self._seed = seed
        self._refusal_rate = refusal_rate
        self._unparseable_rate = unparseable_rate
        self._failure_rate = failure_rate
        self._retryable_failures = retryable_failures
        self._latency_ms = latency_ms
        #: 呼び出し回数。テストで「何回呼ばれたか」を見るために公開する。
        self.calls = 0

    def describe(self) -> str:
        return f"fake:seed={self._seed}"

    def retry_stats(self) -> RetryStats:
        """伝送層を持たないので再試行は常に0（`SupportsRetryStats`）。

        呼び出し回数だけは実数を返す。fake でも進捗表示や実行メタデータの経路が
        本番と同じ形で通ることを確かめられるようにするため。
        """
        return RetryStats(calls=self.calls)

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls += 1
        prompt = "\n".join(_extract_text(message.content) for message in messages)

        if self._should_inject(prompt, "failure", self._failure_rate):
            message = "fake クライアントが注入した失敗"
            raise RetryableLLMError(message) if self._retryable_failures else LLMError(message)

        text = self._answer_text(prompt, response_format)
        return Completion(
            text=text,
            latency_ms=self._latency_ms,
            input_tokens=len(prompt),
            output_tokens=len(text),
            model_version=f"fake-model-{self._seed}",
        )

    # ----------------------------------------------------------------- #

    def _answer_text(self, prompt: str, response_format: dict[str, Any] | None) -> str:
        if self._should_inject(prompt, "refusal", self._refusal_rate):
            return _REFUSAL_TEXT
        if self._should_inject(prompt, "unparseable", self._unparseable_rate):
            return _UNPARSEABLE_TEXT

        option_count = len(_OPTION_LINE.findall(prompt))
        if option_count == 0:
            index = self._bucket(prompt, "open", len(_OPEN_ANSWERS))
            return _OPEN_ANSWERS[index]

        code = self._bucket(prompt, "choice", option_count) + 1
        if response_format is not None:
            if _wants_reasoning(response_format):
                # 理由も返させるスキーマ（§6.3）。本文はダミーだが、順序は本番と同じく
                # reasoning が先。JSON の並びを変えると、記録された生出力の見え方が変わる。
                reason = _OPEN_ANSWERS[self._bucket(prompt, "reasoning", len(_OPEN_ANSWERS))]
                return f'{{"reasoning": "{reason}", "answer": {code}}}'
            return f'{{"answer": {code}}}'
        return str(code)

    def _digest(self, prompt: str, salt: str) -> int:
        payload = f"{self._seed}|{salt}|{prompt}".encode()
        return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")

    def _bucket(self, prompt: str, salt: str, size: int) -> int:
        return self._digest(prompt, salt) % size

    def _should_inject(self, prompt: str, salt: str, rate: float) -> bool:
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return (self._digest(prompt, salt) % 10_000) / 10_000 < rate


def _wants_reasoning(response_format: dict[str, Any]) -> bool:
    """スキーマが `reasoning` を要求しているか（`run.parsing.answer_schema`）。

    形が違うものを渡されても落ちないように、辞書を辿れなければ False にする。
    ここは本番の挙動を写す道具であって、スキーマの検査役ではない。
    """
    schema = response_format.get("json_schema")
    if not isinstance(schema, dict):
        return False
    body = schema.get("schema")
    if not isinstance(body, dict):
        return False
    properties = body.get("properties")
    return isinstance(properties, dict) and "reasoning" in properties


def _extract_text(content: str | list[dict[str, Any]]) -> str:
    """`message.content` からテキスト部分を取り出す（マルチモーダルパーツ対応）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            part_type = part.get("type")
            if part_type is None or part_type == "text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "\n".join(texts)
