"""実行前の見積もり（`docs/SPEC_UI.md` §4、`SPEC.md` §10.1）。

**セッション数も回答件数もここで数えない。** `validate_static()` が返す `Estimate` を
そのまま使う。数え方を2箇所に持つと、画面の見積もりと CLI の見積もりが食い違いうる。
このモジュールがやるのは、その数を実績値でトークン・時間・金額に換算することだけ。

**単位を取り違えないこと。** 本調査の実績値は**1回答あたり**なので `Estimate.answers` に、
判定の実績値は**1セッションあたり**なので `Estimate.screener_sessions` に掛ける。
セッション数は `remember` でまとまった設問群を1つに数えるため、記憶を持つ設問がある
調査では回答件数と一致しない。現行のコンセプト調査は `memory: none` 固定なので
一致するが、記憶を持つ調査種別を足すときは `AnswerBenchmark` の説明を読むこと。

金額は**画面に出す概算**として出す。単価・為替は `ui.estimation_benchmarks.pricing` の
設定値で、ワークスペースの実請求ではない。実行メタデータの `estimated_cost` は
`SPEC.md` §9 のとおり `null` のままにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from persona_sim.panel.validate import Estimate
from persona_sim.uiconfig.schema import Benchmarks, Pricing


@dataclass(frozen=True)
class CostEstimate:
    """画面に出す見積もり。**実績値からの概算**であって保証値ではない。"""

    #: 本調査で書き出される回答レコードの数。トークンと時間の換算はこれが基準。
    answers: int
    survey_sessions: int
    screener_sessions: int
    input_tokens: int
    output_tokens: int
    duration_sec: float
    #: 安全側に振った予算目安（円）。標準の概算は持たない——予算取りに使う数字を
    #: 1つに絞らないと、どちらを見たかで結論が変わる。
    budget_jpy: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_sessions(self) -> int:
        return self.survey_sessions + self.screener_sessions

    def lines(self) -> list[str]:
        minutes = self.duration_sec / 60
        return [
            f"本調査回答件数    : {self.answers:,}",
            f"本調査セッション  : {self.survey_sessions:,}",
            f"スクリーニング    : {self.screener_sessions:,} 回",
            f"入力トークン      : 約 {self.input_tokens:,}",
            f"出力トークン      : 約 {self.output_tokens:,}",
            f"想定所要時間      : 約 {minutes:,.1f} 分",
            f"予算目安          : 約 {self.budget_jpy:,.0f} 円",
        ]


def estimate_cost(
    benchmarks: Benchmarks, estimate: Estimate, oversample_factor: int = 1
) -> CostEstimate:
    """回答件数と判定回数を、実績値でトークン・時間・予算に換算する。

    並列度は受け取らない。`answers_per_min` / `sessions_per_min` が並列度込みの
    エンドツーエンド実測値なので、さらに割ると二重に効く。
    """
    answers = estimate.answers
    screening = estimate.screener_sessions
    survey = benchmarks.survey_answer
    screener = benchmarks.screener_session

    input_tokens = (
        answers * survey.input_tokens_per_answer + screening * screener.input_tokens_per_session
    )
    output_tokens = (
        answers * survey.output_tokens_per_answer + screening * screener.output_tokens_per_session
    )
    duration = (
        answers / survey.answers_per_min + screening / screener.sessions_per_min
    ) * 60

    return CostEstimate(
        answers=answers,
        survey_sessions=estimate.sessions,
        screener_sessions=screening,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_sec=duration,
        budget_jpy=_budget(input_tokens, output_tokens, benchmarks.pricing),
        notes=_notes(screening, oversample_factor),
    )


def _budget(input_tokens: int, output_tokens: int, pricing: Pricing) -> float:
    """安全側の予算目安（円）。

    判定ぶんも足す。issue の式は本調査だけを扱うが、判定も実際にトークンを消費するので、
    足さないと対象者条件を付けた調査で過小に出る。
    """
    usd = (
        input_tokens * pricing.input_usd_per_million
        + output_tokens * pricing.output_usd_per_million
    ) / 1_000_000
    return usd * pricing.jpy_per_usd * pricing.budget_margin


def _notes(screener_sessions: int, oversample_factor: int) -> tuple[str, ...]:
    notes = [
        "過去の実績値からの概算です。実際の消費量・所要時間は前後します。",
        "入力トークンはコンセプト文の分量で増減します。長い提示文では上振れします。",
        "予算目安は設定した単価・為替に安全側の係数を掛けた概算で、実際の請求額ではありません。",
    ]
    if screener_sessions:
        notes.append(
            "対象者条件の通過者が不足した場合、抽出倍率"
            f"（現在 {oversample_factor} 倍）を2倍にして最大3回まで再試行します。"
            "そのぶんスクリーニングの回数は上振れします。"
        )
    return tuple(notes)
