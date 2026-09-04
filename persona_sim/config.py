"""実行環境の設定。**テーブルの置き場所と資格情報は環境変数からのみ読む。**

`AGENTS.md` のデータ・法務の順守事項により、カタログ名・スキーマ名・資格情報を
コードや調査定義に書かない。サービングエンドポイント名は秘密ではないので調査定義
（`model.deployment`）に書く。何で回したかが読み取れないと再現できないため。

| 環境変数 | 用途 |
|---|---|
| `PERSONA_SIM_CATALOG` / `PERSONA_SIM_SCHEMA` | Unity Catalog のカタログ／スキーマ名。両方揃うとマネージドテーブルを使う |
| `PERSONA_SIM_WAREHOUSE` | テーブルを置くベースパス。カタログ未設定時に使う |
| `PERSONA_SIM_SPARK_MASTER` | ローカル実行時の Spark master（既定 `local[*]`） |
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from persona_sim.errors import PersonaSimError

ENV_CATALOG = "PERSONA_SIM_CATALOG"
ENV_SCHEMA = "PERSONA_SIM_SCHEMA"
ENV_WAREHOUSE = "PERSONA_SIM_WAREHOUSE"
ENV_SPARK_MASTER = "PERSONA_SIM_SPARK_MASTER"
ENV_OUTPUT_DIR = "PERSONA_SIM_OUTPUT_DIR"


@dataclass(frozen=True)
class StorageConfig:
    """テーブルの置き場所。カタログ指定とパス指定のどちらか一方を持つ。"""

    catalog: str | None = None
    schema: str | None = None
    warehouse: str | None = None

    @property
    def uses_catalog(self) -> bool:
        return bool(self.catalog and self.schema)

    def __post_init__(self) -> None:
        if not self.uses_catalog and not self.warehouse:
            raise PersonaSimError(
                f"テーブルの置き場所が決まらない。{ENV_CATALOG} と {ENV_SCHEMA} の両方、"
                f"または {ENV_WAREHOUSE} を設定すること"
            )


def storage_config(warehouse: str | None = None) -> StorageConfig:
    """環境変数（と明示指定）から置き場所を解決する。

    `warehouse` を明示した場合はカタログ設定より優先する（CLI の `--warehouse`）。
    """
    if warehouse:
        return StorageConfig(warehouse=warehouse)
    return StorageConfig(
        catalog=os.environ.get(ENV_CATALOG) or None,
        schema=os.environ.get(ENV_SCHEMA) or None,
        warehouse=os.environ.get(ENV_WAREHOUSE) or None,
    )


def spark_master() -> str:
    return os.environ.get(ENV_SPARK_MASTER) or "local[*]"


def output_dir(override: str | None = None) -> str:
    """ファイル成果物（`run_metadata.json` など）の出力先（§7.4）。"""
    return override or os.environ.get(ENV_OUTPUT_DIR) or "outputs"
