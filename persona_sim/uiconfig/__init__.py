"""コンセプト調査 Web UI の設定層（`docs/SPEC_UI.md`）。

`config/ui_config.yaml`（調査横断の固定設定）と画面入力を合成して、調査定義
（`SPEC_PHASE1.md` §3）を作る。**パネル構築・回答生成・集計のロジックは持たない。**
作った調査定義を既存の実行経路（`panel` → `screen` → `run` → `aggregate`）に渡すだけ。

pyspark に依存しないので、合成と割り付けの計算は Spark 抜きでテストできる。
"""

from persona_sim.uiconfig.allocation import age_bands, build_quotas, cell_id
from persona_sim.uiconfig.build import (
    build_screening,
    build_survey,
    build_survey_dict,
    build_survey_pair,
    survey_id,
)
from persona_sim.uiconfig.census import load_census
from persona_sim.uiconfig.cost import CostEstimate, estimate_cost
from persona_sim.uiconfig.loader import load_ui_config, ui_config_from_dict
from persona_sim.uiconfig.schema import (
    SEXES,
    AllocationPattern,
    AnswerBenchmark,
    Benchmarks,
    Concept,
    Pricing,
    SessionBenchmark,
    SurveyForm,
    UIConfig,
    UIConfigError,
)
from persona_sim.uiconfig.summary import target_summary

__all__ = [
    "SEXES",
    "AllocationPattern",
    "AnswerBenchmark",
    "Benchmarks",
    "Concept",
    "CostEstimate",
    "Pricing",
    "SessionBenchmark",
    "SurveyForm",
    "UIConfig",
    "UIConfigError",
    "age_bands",
    "build_quotas",
    "build_screening",
    "build_survey",
    "build_survey_dict",
    "build_survey_pair",
    "cell_id",
    "estimate_cost",
    "load_census",
    "load_ui_config",
    "survey_id",
    "target_summary",
    "ui_config_from_dict",
]
