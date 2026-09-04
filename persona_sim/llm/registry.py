"""`model.endpoint` の値から推論クライアントを組み立てる。"""

from __future__ import annotations

from persona_sim.errors import PersonaSimError
from persona_sim.llm.client import LLMClient
from persona_sim.llm.databricks import DatabricksServingClient
from persona_sim.llm.fake import FakeClient
from persona_sim.panel.schema import Endpoint, ModelConfig


def build_client(model: ModelConfig) -> LLMClient:
    """エンドポイント種別に対応するクライアントを返す。

    `azure_ai_foundry` は未実装。動かないアダプタを黙って通すより、ここで止める
    （`validate` も同じ理由で先に停止させる）。
    """
    match model.endpoint:
        case Endpoint.DATABRICKS:
            return DatabricksServingClient(
                model.deployment, request_timeout_sec=model.request_timeout_sec
            )
        case Endpoint.FAKE:
            return FakeClient()
        case Endpoint.AZURE_AI_FOUNDRY:
            raise PersonaSimError(
                "model.endpoint: azure_ai_foundry はフェーズ1では未実装。"
                f"使えるのは {Endpoint.DATABRICKS} / {Endpoint.FAKE}"
            )
    raise PersonaSimError(f"未知の model.endpoint: {model.endpoint}")
