"""テーブルの所在解決。

ローカル（パス指定の Delta テーブル）と Databricks（`catalog.schema.table`）を
同じコードから扱うため、論理テーブル名を `TableLocator` に解決させる。
pyspark に依存しないので Spark 抜きで単体テストできる。
"""

from __future__ import annotations

from dataclasses import dataclass

from persona_sim.config import StorageConfig

#: 論理テーブル名（`SPEC_PHASE1.md` §2）。
PERSONAS_BASE = "personas_base"
PANELS = "panels"
RESPONSES = "responses"
#: スクリーニングの生データ（§2.6）。`responses` とは分けて持つ。
SCREENER_RESPONSES = "screener_responses"
RUNS = "runs"
AGGREGATES = "aggregates"


@dataclass(frozen=True)
class TableLocator:
    """1つのテーブルの所在。"""

    name: str
    config: StorageConfig

    @property
    def uses_catalog(self) -> bool:
        return self.config.uses_catalog

    @property
    def identifier(self) -> str:
        """`catalog.schema.table` 形式。カタログ未設定なら使えない。"""
        if not self.uses_catalog:
            raise ValueError(f"{self.name}: カタログが設定されていない")
        return f"{self.config.catalog}.{self.config.schema}.{self.name}"

    @property
    def path(self) -> str:
        """パス形式。カタログ設定時は使えない。"""
        if self.uses_catalog:
            raise ValueError(f"{self.name}: カタログ設定時はパスではなく identifier を使う")
        base = (self.config.warehouse or "").rstrip("/")
        return f"{base}/{self.name}"

    def describe(self) -> str:
        """ログ・メタデータ出力用の表示名。"""
        return self.identifier if self.uses_catalog else self.path


def locator(name: str, config: StorageConfig) -> TableLocator:
    return TableLocator(name=name, config=config)
