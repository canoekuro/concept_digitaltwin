"""`app/config/ui_config.yaml` の読み込み。

調査定義ローダ（`persona_sim.panel.loader`）と同じ方針で、**未知のキーは黙って捨てず
停止する**。綴り違いの設定がそのまま消えると、「設定したのに効いていない」ことに
実行後まで気づけない。

最上位は2群に分かれている。`survey_defaults` はそのまま調査定義になるもの、
`ui` は画面を描くためだけのもの。混ざっていると、設定を変えたときに調査結果が
変わるのか画面の見た目が変わるのかを読み分けられない。

`survey_defaults` の `model` / `prompt` / `persona_card` はここでは検証しない。
調査定義に渡した先で `survey_from_dict()` が見る。二重に定義すると片方だけ直す
事故が起きる。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from persona_sim.uiconfig.schema import (
    SUPPORTED_BANDS,
    AllocationPattern,
    AnswerBenchmark,
    Benchmarks,
    MainSurveyDefaults,
    Pricing,
    ScreeningDefaults,
    SessionBenchmark,
    UIConfig,
    UIConfigError,
)

_TOP_LEVEL_KEYS = ("survey_defaults", "ui")

_SURVEY_DEFAULTS_KEYS = ("survey_type", "screening", "main_survey", "questions", "output")

_UI_KEYS = ("allocation_patterns", "estimation_benchmarks")

def load_ui_config(path: str | Path) -> UIConfig:
    """設定ファイル（`app/config/ui_config.yaml`）を読む。

    **既定パスは持たない。** 置き場所を決められるのは呼ぶ側だけで、ここに cwd 相対の
    既定を置くと起動ディレクトリ次第で解決できたりできなかったりする。アプリの解決は
    `app/lib/context.py` の `DEFAULT_UI_CONFIG_PATH`（`__file__` 基準）が持つ。
    """
    target = Path(path)
    if not target.exists():
        raise UIConfigError(f"UI 設定ファイルが無い: {target}")
    return ui_config_from_dict(yaml.safe_load(target.read_text(encoding="utf-8")) or {})


def ui_config_from_dict(data: Mapping[str, Any]) -> UIConfig:
    """dict から組み立てる。ノートブックやテストから直接呼べるようにしてある。"""
    mapping = _as_mapping(data, "")
    _reject_unknown(mapping, _TOP_LEVEL_KEYS, "")

    defaults = _as_mapping(mapping.get("survey_defaults"), "survey_defaults")
    _reject_unknown(defaults, _SURVEY_DEFAULTS_KEYS, "survey_defaults")

    ui = _as_mapping(mapping.get("ui"), "ui")
    _reject_unknown(ui, _UI_KEYS, "ui")

    output = _as_mapping(defaults.get("output"), "survey_defaults.output")
    _reject_unknown(output, ("base_segments",), "survey_defaults.output")

    return UIConfig(
        main_survey=_main_survey(defaults.get("main_survey")),
        screening=_screening(defaults.get("screening")),
        default_questions=tuple(
            _as_mapping(item, f"survey_defaults.questions[{i}]")
            for i, item in enumerate(
                _as_sequence(defaults.get("questions"), "survey_defaults.questions")
            )
        ),
        allocation_patterns=_allocation(ui.get("allocation_patterns")),
        benchmarks=_benchmarks(ui.get("estimation_benchmarks")),
        base_segments=tuple(
            _as_str(v, f"survey_defaults.output.base_segments[{i}]")
            for i, v in enumerate(output.get("base_segments", ["total", "sex"]))
        ),
        survey_type=_as_str(defaults.get("survey_type", "concept"), "survey_defaults.survey_type"),
    )


def _main_survey(source: Any) -> MainSurveyDefaults:
    path = "survey_defaults.main_survey"
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, ("model", "prompt", "persona_card"), path)
    return MainSurveyDefaults(
        model=_as_mapping(mapping.get("model"), f"{path}.model"),
        prompt=_as_mapping(mapping.get("prompt"), f"{path}.prompt"),
        persona_card=_as_mapping(mapping.get("persona_card"), f"{path}.persona_card"),
    )


def _screening(source: Any) -> ScreeningDefaults:
    path = "survey_defaults.screening"
    mapping = _as_mapping(source, path)
    _reject_unknown(
        mapping,
        ("mode", "oversample_factor", "logic", "batch_size", "model", "prompt", "persona_card"),
        path,
    )
    defaults = ScreeningDefaults()
    batch_size = mapping.get("batch_size")
    return ScreeningDefaults(
        mode=_as_str(mapping.get("mode", defaults.mode), f"{path}.mode"),
        oversample_factor=_as_int(
            mapping.get("oversample_factor", defaults.oversample_factor), f"{path}.oversample_factor"
        ),
        logic=_as_str(mapping.get("logic", defaults.logic), f"{path}.logic"),
        batch_size=None if batch_size is None else _as_int(batch_size, f"{path}.batch_size"),
        model=_as_mapping(mapping.get("model"), f"{path}.model"),
        prompt=_as_mapping(mapping.get("prompt"), f"{path}.prompt"),
        persona_card=_as_mapping(mapping.get("persona_card"), f"{path}.persona_card"),
    )


def _allocation(source: Any) -> tuple[AllocationPattern, ...]:
    path = "ui.allocation_patterns"
    patterns = []
    for index, item in enumerate(_as_sequence(source, path)):
        item_path = f"{path}[{index}]"
        entry = _as_mapping(item, item_path)
        _reject_unknown(entry, ("id", "label", "band", "census"), item_path)
        band = _as_int(entry.get("band"), f"{item_path}.band")
        if band not in SUPPORTED_BANDS:
            allowed = ", ".join(str(b) for b in SUPPORTED_BANDS)
            raise UIConfigError(
                f"{item_path}.band: {band} は使えない。集計軸があるのは {allowed} 歳刻みのみ"
            )
        patterns.append(
            AllocationPattern(
                id=_as_str(entry.get("id"), f"{item_path}.id"),
                label=_as_str(entry.get("label"), f"{item_path}.label"),
                band=band,
                census=bool(entry.get("census", False)),
            )
        )
    if not patterns:
        raise UIConfigError(f"{path} が空。割り付けパターンが1つも無い")

    duplicates = _duplicates([p.id for p in patterns])
    if duplicates:
        raise UIConfigError(f"{path}: id が重複している: {', '.join(duplicates)}")
    return tuple(patterns)


def _benchmarks(source: Any) -> Benchmarks:
    path = "ui.estimation_benchmarks"
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, ("survey_answer", "screener_session", "pricing"), path)
    return Benchmarks(
        survey_answer=_answer_benchmark(mapping.get("survey_answer"), f"{path}.survey_answer"),
        screener_session=_session_benchmark(
            mapping.get("screener_session"), f"{path}.screener_session"
        ),
        pricing=_pricing(mapping.get("pricing"), f"{path}.pricing"),
    )


def _answer_benchmark(source: Any, path: str) -> AnswerBenchmark:
    keys = ("input_tokens_per_answer", "output_tokens_per_answer", "answers_per_min")
    mapping = _required_benchmark(source, path, keys)
    return AnswerBenchmark(
        input_tokens_per_answer=_as_int(mapping[keys[0]], f"{path}.{keys[0]}"),
        output_tokens_per_answer=_as_int(mapping[keys[1]], f"{path}.{keys[1]}"),
        answers_per_min=_positive_float(mapping[keys[2]], f"{path}.{keys[2]}"),
    )


def _session_benchmark(source: Any, path: str) -> SessionBenchmark:
    keys = ("input_tokens_per_session", "output_tokens_per_session", "sessions_per_min")
    mapping = _required_benchmark(source, path, keys)
    return SessionBenchmark(
        input_tokens_per_session=_as_int(mapping[keys[0]], f"{path}.{keys[0]}"),
        output_tokens_per_session=_as_int(mapping[keys[1]], f"{path}.{keys[1]}"),
        sessions_per_min=_positive_float(mapping[keys[2]], f"{path}.{keys[2]}"),
    )


def _required_benchmark(source: Any, path: str, keys: Sequence[str]) -> Mapping[str, Any]:
    """実績値1組を読む前の共通チェック。

    **キー名に単位が入っている**（`*_per_answer` / `*_per_session`）。1回答あたりと
    1セッションあたりは `memory` の設定次第で一致しないので、名前で取り違えを防ぐ。
    """
    mapping = _as_mapping(source, path)
    _reject_unknown(mapping, keys, path)
    missing = [key for key in keys if mapping.get(key) is None]
    if missing:
        raise UIConfigError(f"{path}: {', '.join(missing)} が無い。見積もりを出せない")
    return mapping


def _pricing(source: Any, path: str) -> Pricing:
    mapping = _as_mapping(source, path)
    keys = ("input_usd_per_million", "output_usd_per_million", "jpy_per_usd", "budget_margin")
    _reject_unknown(mapping, keys, path)
    missing = [key for key in keys[:3] if mapping.get(key) is None]
    if missing:
        raise UIConfigError(f"{path}: {', '.join(missing)} が無い。予算目安を出せない")
    margin = _as_float(mapping.get("budget_margin", 1.1), f"{path}.budget_margin")
    if margin < 1:
        # 1未満は「標準の概算より安く見積もる」ことになる。安全側に振るための係数なので、
        # 意図せず下振れさせない。
        raise UIConfigError(f"{path}.budget_margin: 1 以上（安全側の係数）。今は {margin}")
    return Pricing(
        input_usd_per_million=_as_float(mapping[keys[0]], f"{path}.{keys[0]}"),
        output_usd_per_million=_as_float(mapping[keys[1]], f"{path}.{keys[1]}"),
        jpy_per_usd=_positive_float(mapping[keys[2]], f"{path}.{keys[2]}"),
        budget_margin=margin,
    )


# --------------------------------------------------------------------------- #
# 小道具
# --------------------------------------------------------------------------- #


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        repeated.add(value) if value in seen else seen.add(value)
    return sorted(repeated)


def _reject_unknown(mapping: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        where = path or "ルート"
        raise UIConfigError(
            f"{where}: 未知のキー {', '.join(unknown)}。指定できるのは {', '.join(sorted(allowed))}"
        )


def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise UIConfigError(f"{path or 'ルート'}: マッピングである必要がある（実際: {type(value).__name__}）")
    return value


def _as_sequence(value: Any, path: str) -> Sequence[Any]:
    if value is None:
        raise UIConfigError(f"{path} が無い")
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise UIConfigError(f"{path}: リストである必要がある（実際: {type(value).__name__}）")
    return value


def _as_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise UIConfigError(f"{path}: 文字列である必要がある（実際: {type(value).__name__}）")
    return value


def _as_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UIConfigError(f"{path}: 整数である必要がある（実際: {type(value).__name__}）")
    return value


def _as_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UIConfigError(f"{path}: 数値である必要がある（実際: {type(value).__name__}）")
    return float(value)


def _positive_float(value: Any, path: str) -> float:
    """0以下を弾く。速度は所要時間の分母に入るので、0だと見積もりがゼロ除算で壊れる。"""
    number = _as_float(value, path)
    if number <= 0:
        raise UIConfigError(f"{path}: 0 より大きい必要がある（実際: {number}）")
    return number
