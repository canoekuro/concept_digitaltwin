"""Web UI の設定と画面入力（`docs/SPEC_UI.md`）。

設定は**2層**に分かれる。

| 層 | 何が入るか | どこから来るか |
|---|---|---|
| `UIConfig` | 調査をまたいで変わらないもの（本調査とスクリーニングのモデル・プロンプト・ペルソナカード、既定設問、割り付けパターン、見積もり実績値） | `app/config/ui_config.yaml` |
| `SurveyForm` | 1調査ごとに変わるもの（調査名・N数・年齢範囲・対象者条件・コンセプト・設問） | 画面入力 |

`UIConfig` 自身も**最上位で2群に分かれる**。`survey_defaults` はそのまま調査定義に
なるもの、`ui` は画面を描くためだけのもの（`UIConfig` の説明を参照）。

2つを `persona_sim.uiconfig.build.build_survey()` が合成して調査定義（`SPEC.md` §3）を作る。
分けているのは、画面に出す入力欄と、運用で固定しておきたい値の寿命が違うため。
毎回入力させると事故るし、設定ファイルに埋めると調査ごとに変えられない。

このモジュールは **pyspark に依存しない**。合成と割り付けの計算を Spark 抜きでテストできるようにする。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 割り付けの刻み幅として使えるもの。集計軸 `age_band_5` / `age_band_10` に対応する。
SUPPORTED_BANDS = (5, 10)

#: 割り付けに使う性別。`personas_base.sex` の値（§2.1）。
SEXES = ("男", "女")


@dataclass(frozen=True)
class AnswerBenchmark:
    """本調査の実績値。単位は **1回答あたり**（1セッションあたりではない）。

    **なぜ回答単位か。** 実測はペルソナ1人が1問に答えるたびの消費量として取っている。
    一方セッション数は `remember` で結ばれた設問群を1つに数えるので、記憶を持つ
    設問がある調査では回答数と一致しない（`persona_sim.run.memory.session_groups()`）。
    セッション数に掛けると、そういう調査で出力トークンがまとまり具合のぶんだけ
    過小に出る。だから `Estimate.answers` に掛ける。

    現行のコンセプト調査は `memory: none` 固定（`persona_sim.uiconfig.build`）なので
    1セッション＝1回答で一致するが、**記憶を持つ調査種別を足すときはここを見直すこと。**
    履歴を再生するぶん入力トークンは回答あたりでも増えるため、この実績値のままでは
    合わなくなる。

    速度も同じ単位で持つ。`answers_per_min` は並列度込みのエンドツーエンド実測値なので、
    さらに並列度で割らない（割ると二重に効く）。
    """

    input_tokens_per_answer: int
    output_tokens_per_answer: int
    answers_per_min: float


@dataclass(frozen=True)
class SessionBenchmark:
    """判定（スクリーニング）の実績値。単位は **1セッションあたり**。

    判定は1回で `batch_size` 人分をまとめて捌くので、回答単位に直せない。
    `sessions_per_min` も並列度込みの実測値として扱う。
    """

    input_tokens_per_session: int
    output_tokens_per_session: int
    sessions_per_min: float


@dataclass(frozen=True)
class Pricing:
    """見積もりを金額に直すための前提（`ui.estimation_benchmarks.pricing`）。

    単価はモデル改定で、為替は日々で動く。別々に直せるよう分けて持つ。
    ここで出すのは**画面に出す概算**であって実請求ではない。実行メタデータの
    `estimated_cost` は `SPEC.md` §9 のとおり `null` のままにする。
    """

    input_usd_per_million: float
    output_usd_per_million: float
    jpy_per_usd: float
    #: 安全側に振るための係数。入力トークンはコンセプト文の分量で上振れするので、
    #: 予算取りには標準の概算をそのまま使わせない。
    budget_margin: float = 1.1


@dataclass(frozen=True)
class Benchmarks:
    """見積もりに使う実績値。本調査と判定は**単位が違う**ので型ごと分けて持つ。

    判定は1回で `batch_size` 人分のペルソナカードを渡すため、入力トークンが本調査の
    数十倍になる。同じ値で見積もると桁を間違える。
    """

    survey_answer: AnswerBenchmark
    screener_session: SessionBenchmark
    pricing: Pricing


@dataclass(frozen=True)
class AllocationPattern:
    """割り付けパターン（画面のラジオボタン1つ分）。

    整数配分は自分で書かない。`quotas.mode: proportion` で比率を渡し、
    `persona_sim.panel.quotas.allocate_cell_sizes()` の最大剰余法に委ねる（§4.1）。
    """

    id: str
    label: str
    #: 年代の刻み幅（歳）。`SUPPORTED_BANDS` のいずれか。
    band: int
    #: 人口構成比を使うか。False なら性年代セルに均等配分する。
    census: bool = False


@dataclass(frozen=True)
class ScreeningDefaults:
    """スクリーニングの既定（`survey_defaults.screening`、§4.2）。

    UI は自然言語の条件文を1つ受け取るだけなので、オーバーサンプル倍率と判定設定は
    ここで固定する。

    `model` / `prompt` / `persona_card` は解釈せずそのまま調査定義へ渡す
    （`UIConfig` の説明を参照）。
    """

    oversample_factor: int = 4
    batch_size: int | None = None
    model: Mapping[str, Any] = field(default_factory=dict)
    prompt: Mapping[str, Any] = field(default_factory=dict)
    persona_card: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MainSurveyDefaults:
    """本調査の既定（`survey_defaults.main_survey`）。

    すべて解釈せずそのまま調査定義の `main_survey:` へ渡す（`UIConfig` の説明を参照）。
    """

    model: Mapping[str, Any] = field(default_factory=dict)
    prompt: Mapping[str, Any] = field(default_factory=dict)
    persona_card: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UIConfig:
    """`app/config/ui_config.yaml` の中身。

    **最上位は2群に分かれている。**

    - `survey_defaults` … そのまま調査定義になるもの。ここに書いた `model` /
      `prompt` / `persona_card` は解釈せず素通しする。UI 側で再定義すると
      調査定義スキーマ（`persona_sim.panel.schema`）と二重管理になり、片方だけ
      直す事故が起きる。妥当性は `survey_from_dict()` が見る
    - `ui` … 画面を描くためだけのもの。調査定義には入らない
    """

    #: `survey_defaults.main_survey`。
    main_survey: MainSurveyDefaults
    #: `survey_defaults.screening`。
    screening: ScreeningDefaults
    #: `survey_defaults.questions`。画面の設問フォームの初期値になり、
    #: 編集された結果が調査定義の `questions:` になる。
    default_questions: tuple[Mapping[str, Any], ...]
    #: `ui.allocation_patterns`。画面のラジオボタンの定義。
    allocation_patterns: tuple[AllocationPattern, ...]
    #: `ui.estimation_benchmarks`。画面の見積もり表示にだけ使う。
    benchmarks: Benchmarks
    #: 割り付けパターンとは独立に必ず入れる集計軸（`survey_defaults.output.base_segments`）。
    base_segments: tuple[str, ...] = ("total", "sex")
    #: この画面が作る調査の種類（`SPEC.md` §3.0）。設定に置いてあるのは、
    #: 将来ほかの種別の画面を作るときにコードではなく設定で切り替えられるようにするため。
    survey_type: str = "concept"

    def pattern(self, pattern_id: str) -> AllocationPattern:
        for pattern in self.allocation_patterns:
            if pattern.id == pattern_id:
                return pattern
        available = ", ".join(p.id for p in self.allocation_patterns)
        raise UIConfigError(f"割り付けパターン {pattern_id!r} は未定義。使えるのは {available}")

    def segments_for(self, band: int) -> tuple[str, ...]:
        """刻み幅に応じた集計軸（§7.1）。

        5歳刻みで割り付けたのに10歳刻みでしか集計できないと、割り付けたセルの
        構成を確認できない。刻み幅に合わせて軸を切り替える。
        """
        return (*self.base_segments, f"age_band_{band}", f"sex_x_age_band_{band}", "cell_id")


@dataclass(frozen=True)
class Concept:
    """画面で入力する1コンセプト。調査定義の `stimuli` 1件になる。"""

    name: str
    text: str


@dataclass(frozen=True)
class SurveyForm:
    """調査設計ページの入力内容。

    `condition` が空文字なら対象者条件なし＝スクリーニングを行わない
    （調査定義の `screening:` を書かない）。

    `sex_ranges` は対象にする性別だけをキーに持つ（`SEXES` の部分集合。少なくとも1件必要）。
    値は `(age_min, age_max)`。性別ごとに対象年齢を変えられ、チェックを外した性別は
    調査対象から除外される。
    """

    name: str
    panel_size: int
    sex_ranges: Mapping[str, tuple[int, int]]
    allocation_pattern: str
    concepts: tuple[Concept, ...]
    condition: str = ""
    seed: int = 42
    survey_id: str | None = None
    #: 画面で編集した設問。省略時は `UIConfig.default_questions` をそのまま使う。
    questions: tuple[Mapping[str, Any], ...] | None = None


class UIConfigError(Exception):
    """UI 設定・画面入力の不整合。読み込み時か合成時に停止する。"""
