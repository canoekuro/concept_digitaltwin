"""調査定義の検証（`SPEC_PHASE1.md` §10.1, §11）。

2段構えになっている。

- `validate_static`: Spark を使わない検証。2軸の整合（E6）・不変条件・設問定義・割り付けの合計。
- `validate_feasibility`: `personas_base` を読み、割り付けが埋まるか（E1）を確認する。

エラーは最初の1件で止めず、**全件を集めて返す**。1つ直すたびに再実行させないため。

**設問IDの一意性と `slot` の被覆はここでは見ない。** `run` は `validate` を通らずに
実行できるので、放っておくと設問が黙って消える。読み込み（`panel/loader.py`）で
止めており、そこを通らずに調査定義が作られることはない。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from persona_sim.panel.schema import (
    REASONING_QUESTION_TYPES,
    Endpoint,
    ImageMode,
    Question,
    QuestionType,
    QuotaMode,
    RememberMode,
    StructuredOutput,
    SurveyDefinition,
)
from persona_sim.run.memory import ask_order, session_groups

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

#: proportion モードの合計を 1.0 とみなす許容誤差。
_PROPORTION_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Issue:
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class Estimate:
    """実行前に必ず提示する見積もり（§10.1）。

    **`sessions` と `answers` は別物。** セッションは LLM を呼ぶ回数、回答は書き出す
    レコードの数で、記憶を持つ設問がある調査では一致しない（`remember` でまとまった
    設問群は1セッションで複数の回答を出す）。換算するときは量の単位で選ぶ——
    呼び出し回数に比例するものは `sessions`、回答1件あたりで実測した値は `answers`。
    """

    panel_size: int
    stimuli_total: int
    stimuli_per_persona: int
    #: 調査定義に書かれた設問の数。設問は slot ごとに展開されるので、
    #: 1人が答える数そのもの（＝聞かれる問いの延べ数）になる。
    questions: int
    #: そのうち別々の問いは何種類か（`measure` の異なり数）。展開前の設問数にあたる。
    measures: int
    sessions: int
    effective_n_per_stimulus: int
    #: スクリーニングの判定呼び出し回数。`screening:` が無ければ0。
    screener_sessions: int = 0

    @property
    def answers(self) -> int:
        """本調査で書き出される回答レコードの数（人 × 聞かれる問いの延べ数）。

        `questions` は slot 展開後なので、これだけで「人 × コンセプト数 × 設問数」になる。
        記憶を持たない調査では `sessions` と一致するが、一致を前提にしてはいけない。
        """
        return self.panel_size * self.questions

    def lines(self) -> list[str]:
        lines = [
            f"パネル人数        : {self.panel_size:,}",
            f"コンセプト数      : {self.stimuli_total}",
            f"1人あたり評価数   : {self.stimuli_per_persona}",
            f"設問数            : {self.questions}（別々の問い {self.measures} 種）",
        ]
        if self.screener_sessions:
            lines.append(f"スクリーニング    : {self.screener_sessions:,} 回")
        lines.extend(
            [
                f"本調査            : {self.sessions:,} セッション",
                f"有効サンプル数/案 : {self.effective_n_per_stimulus:,}",
            ]
        )
        return lines


@dataclass
class ValidationReport:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    estimate: Estimate | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str) -> None:
        self.errors.append(Issue(code, message))

    def warn(self, code: str, message: str) -> None:
        self.warnings.append(Issue(code, message))

    def merge(self, other: ValidationReport) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.estimate = other.estimate or self.estimate


def validate_static(survey: SurveyDefinition) -> ValidationReport:
    """Spark を使わずに検証できるものすべて。"""
    report = ValidationReport()
    _check_invariants(survey, report)
    _check_stimuli(survey, report)
    _check_questions(survey, report)
    _check_quotas(survey, report)
    _check_screener(survey, report)
    _check_prompt(survey, report)
    _check_output_budgets(survey, report)
    _check_output(survey, report)
    _check_unimplemented(survey, report)
    if report.ok:
        report.estimate = estimate(survey)
    return report


def estimate(survey: SurveyDefinition) -> Estimate:
    """セッション数と有効サンプル数の見積もり（§10.1）。"""
    from persona_sim.panel.screening import infer_call_count

    k = survey.stimuli_count
    m = survey.stimuli_per_persona
    size = survey.panel.size
    return Estimate(
        panel_size=size,
        stimuli_total=k,
        stimuli_per_persona=m,
        questions=len(survey.questions),
        measures=len({question.measure_key for question in survey.questions}),
        # 設問どうしの記憶の持ち方でセッションの粒度が変わるので、実行と同じ関数から数える
        # （`memory: none` なら設問数、`full_session` なら1）。設問数を掛けた式で置くと、
        # 記憶を持たせた調査でコスト見積が実際の呼び出し回数とずれる。
        sessions=size * len(session_groups(survey)),
        effective_n_per_stimulus=size * m // k,
        screener_sessions=infer_call_count(survey),
    )


# --------------------------------------------------------------------------- #
# 静的検証
# --------------------------------------------------------------------------- #


def _check_invariants(survey: SurveyDefinition, report: ValidationReport) -> None:
    """`AGENTS.md` の不変条件のうち、調査定義から機械的に確認できるもの。"""
    if survey.model.thinking:
        report.error(
            "INVARIANT",
            "model.thinking は常に false。思考モードを有効にすると回答が収束し、"
            "ペルソナ間のばらつきが失われる",
        )

    for question in survey.questions:
        if question.randomize_options and question.is_ordinal():
            report.error(
                "INVARIANT",
                f"questions[{question.id}]: 順序尺度の選択肢はシャッフルしない。"
                "randomize_options: true を許可するのは非順序の選択肢のみ"
                "（type: scale、または top_box を持つ設問は順序尺度とみなす）",
            )

    _check_reasoning(survey, report)


def _check_reasoning(survey: SurveyDefinition, report: ValidationReport) -> None:
    """理由を書かせる設問（`questions[].reasoning`、§6.3）の前提を確かめる。"""
    reasoning_questions = [question for question in survey.questions if question.reasoning]
    if not reasoning_questions:
        return

    for question in reasoning_questions:
        if question.type not in REASONING_QUESTION_TYPES:
            report.error(
                "SURVEY",
                f"questions[{question.id}]: type: {question.type} に reasoning は書けない。"
                "理由を書かせられるのは番号で答える設問"
                f"（{', '.join(str(t) for t in REASONING_QUESTION_TYPES)}）のみ",
            )
        if question.reasoning_max_length < 1:
            report.error(
                "SURVEY",
                f"questions[{question.id}].reasoning_max_length: 1 以上の整数で書くこと",
            )

    if survey.model.structured_output is not StructuredOutput.ALWAYS:
        report.error(
            "SURVEY",
            "reasoning: true の設問があるときは model.structured_output を always にすること"
            f"（現在: {survey.model.structured_output}）。正規表現パースへ落ちると、"
            "理由の文中に現れた最初の数字を回答番号として拾ってしまう",
        )

    report.warn(
        "W_REASONING",
        f"reasoning: true の設問が {len(reasoning_questions)} 問ある。"
        "理由を書かせると回答が収束してペルソナ間のばらつきが縮みうる"
        "（SPEC_PHASE1.md §13）。水準だけでなく、選択肢の分布・セグメント間の差が"
        "残っているかを確かめること。出力トークンと所要時間も増える",
    )


#: 日本語1文字あたりのトークン数の目安。**厳密な値ではない**（トークナイザとモデルで変わる）。
#: 予算が明らかに足りない組み合わせを見つけるためだけに使う。だからこの検査は警告であって
#: エラーではない——推定値で調査を止めると、実際には通る設定まで拒むことになる。
TOKENS_PER_JAPANESE_CHAR = 1.5

#: 構造化出力の器（キー・引用符・エスケープ・番号）に要るトークンの目安。
#: `{"reasoning": "…", "answer": 3}` の本文以外の部分。
JSON_ENVELOPE_TOKENS = 32


def _required_tokens(max_length: int, *, envelope: int = 0) -> int:
    """指示した字数を書き切るのに要るトークン数の目安。"""
    return int(max_length * TOKENS_PER_JAPANESE_CHAR) + envelope


def _check_output_budgets(survey: SurveyDefinition, report: ValidationReport) -> None:
    """**指示した字数**と**出力予算**が噛み合っているかを見る（§6.3）。

    この2つは二重管理ではなく、単位も役割も違う。

    - `questions[].max_length` / `.reasoning_max_length` は**文字数**で、モデルへの
      指示文に入る「目安」。守られないこともある
    - `model.max_tokens_open` / `.max_tokens_reasoning` は**トークン**で、
      エンドポイント側の**絶対の打ち切り**

    だから片方から他方を自動で決めない——文字とトークンの比はトークナイザとモデルで
    変わるので、係数をコードに埋めると「設定したつもり」の乖離が設定ファイルから
    実装へ移るだけになる。代わりに、明らかに足りない組み合わせをここで警告する。

    足りないと黙って壊れはしない（`llm/budget.py` が予算を広げて再送する）が、
    呼び出しが増え、天井まで届かなければ理由や自由回答が切り詰められて
    `output_limit` フラグが付く。どちらも実行後にしか気づけない。
    """
    model = survey.model

    for question in survey.questions:
        if question.reasoning:
            _warn_if_budget_is_short(
                report,
                question_id=question.id,
                max_length=question.reasoning_max_length,
                length_key="reasoning_max_length",
                budget=model.max_tokens_reasoning,
                budget_key="max_tokens_reasoning",
                envelope=JSON_ENVELOPE_TOKENS,
            )
        elif question.type is QuestionType.OPEN and question.max_length is not None:
            _warn_if_budget_is_short(
                report,
                question_id=question.id,
                max_length=question.max_length,
                length_key="max_length",
                budget=model.max_tokens_open,
                budget_key="max_tokens_open",
            )


def _warn_if_budget_is_short(
    report: ValidationReport,
    *,
    question_id: str,
    max_length: int,
    length_key: str,
    budget: int,
    budget_key: str,
    envelope: int = 0,
) -> None:
    required = _required_tokens(max_length, envelope=envelope)
    if budget >= required:
        return

    report.warn(
        "W_OUTPUT_BUDGET",
        f"questions[{question_id}].{length_key}: {max_length}文字を指示しているのに "
        f"model.{budget_key} が {budget} トークンしかない（目安 {required} 以上）。"
        "字数はモデルへの指示、max_tokens はエンドポイント側の打ち切りで別物なので、"
        "揃えるのは書いた人の仕事になる。足りないと予算の引き上げ再送が起き、"
        "天井まで届かなければ回答が切り詰められて output_limit フラグが付く",
    )


#: Unity Catalog Volumes のパス接頭辞。フェーズ1の実行環境は Databricks（`AGENTS.md`）。
VOLUMES_PREFIX = "/Volumes/"


def _check_stimuli(survey: SurveyDefinition, report: ValidationReport) -> None:
    _check_unique_ids([s.id for s in survey.stimuli], "stimuli", report)
    for stimulus in survey.stimuli:
        if not stimulus.text.strip():
            report.error("SURVEY", f"stimuli[{stimulus.id}]: text が空")
        if stimulus.image_mode is ImageMode.NATIVE and not stimulus.image_uri:
            report.error(
                "SURVEY",
                f"stimuli[{stimulus.id}]: image_mode が native ですが image_uri が指定されていません",
            )
        _check_image_uri(stimulus, report)


def _check_image_uri(stimulus, report: ValidationReport) -> None:
    """画像の置き場所（§3）。"""
    uri = stimulus.image_uri
    if not uri or uri.startswith(VOLUMES_PREFIX):
        return

    path = f"stimuli[{stimulus.id}].image_uri"
    if uri.startswith(("dbfs:/", "/dbfs/")):
        report.warn(
            "W_IMAGE_URI_LEGACY",
            f"{path}: DBFS のパスは旧形式。Unity Catalog Volumes"
            f"（{VOLUMES_PREFIX}{{catalog}}/{{schema}}/{{volume}}/...）に置き換えること",
        )
        return

    report.warn(
        "W_IMAGE_URI",
        f"{path}: {uri!r} は Unity Catalog Volumes のパスではない。"
        f"フェーズ1の実行環境は Databricks なので "
        f"{VOLUMES_PREFIX}{{catalog}}/{{schema}}/{{volume}}/... 形式で指定すること",
    )


def _check_questions(survey: SurveyDefinition, report: ValidationReport) -> None:
    # 設問IDの一意性はここでは見ない。読み込みで止まるので届かない
    # （`panel/loader.py::_reject_duplicate_question_ids()`）。
    for question in survey.questions:
        path = f"questions[{question.id}]"
        needs_options = question.type in (QuestionType.SINGLE, QuestionType.MULTI, QuestionType.SCALE)
        if needs_options and len(question.options) < 2:
            report.error("SURVEY", f"{path}: type {question.type} には選択肢が2つ以上必要")
        if question.type is QuestionType.OPEN and question.options:
            report.error("SURVEY", f"{path}: type open に options は指定できない")
        if question.top_box:
            out_of_range = [i for i in question.top_box if not 1 <= i <= len(question.options)]
            if out_of_range:
                report.error(
                    "SURVEY",
                    f"{path}: top_box {out_of_range} が選択肢の範囲（1〜{len(question.options)}）外",
                )

    _check_measures(survey, report)
    _check_remember(survey, report)
    _check_system_prompts(survey, report)


def _check_system_prompts(survey: SurveyDefinition, report: ValidationReport) -> None:
    """`main_survey.prompt.systems` に、どの設問からも指されていない文面が無いか（§6.1）。

    未知の名前を**指した**側は読み込みで止まる（`panel/loader.py`）。ここで見るのは逆で、
    書いたのに誰も指していない側。書き分けたつもりの文面が1問にも当たっていない状態は、
    設問側の `system` の書き忘れであることが多い。実行しても既定で聞かれるだけで
    エラーにならないので、開始前に見せる。
    """
    used = {question.system for question in survey.questions if question.system is not None}
    unused = sorted(set(survey.prompt.systems) - used)
    if unused:
        report.warn(
            "W_UNUSED_SYSTEM_PROMPT",
            f"main_survey.prompt.systems: {', '.join(unused)} を"
            "どの設問も指していない。書いた文面が1問にも当たっていないので、"
            "使わせたい設問に system: を書くこと",
        )


def _check_measures(survey: SurveyDefinition, report: ValidationReport) -> None:
    """同じ `measure` の設問が、同じ表として集計できる形か（E6, §8）。

    コンセプト比較表は measure ごとに1表で、**表頭も指標も代表1つの設問から作る**。
    食い違ったまま束ねると、別の問いの数字が同じ列に並ぶ。

    `top_box` を見るのは、T2B が代表の定義で全コンセプトぶん計算されるため。
    コンセプトごとに「上位いくつ」がずれていても表は出てしまい、数字だけが静かに狂う。
    `is_ordinal()` も `top_box` を見るので、平均を出す／出さないも入れ替わる。
    """
    representatives: dict[str, Question] = {}
    for question in survey.questions:
        first = representatives.setdefault(question.measure_key, question)
        if first is question:
            continue
        if first.type is not question.type:
            report.error(
                "E6",
                f"questions[{question.id}]: measure {question.measure_key!r} の設問で type が違う"
                f"（{first.id} は {first.type}、{question.id} は {question.type}）。"
                "同じ measure はコンセプト横断で1つの表に束ねるので、型が揃っている必要がある",
            )
        elif first.options != question.options:
            report.error(
                "E6",
                f"questions[{question.id}]: measure {question.measure_key!r} の設問で options が違う"
                f"（{first.id} と食い違う）。表頭は代表1つから作るため、選択肢は揃えること",
            )
        elif first.top_box != question.top_box:
            report.error(
                "E6",
                f"questions[{question.id}]: measure {question.measure_key!r} の設問で top_box が違う"
                f"（{first.id} は {list(first.top_box or ())}、"
                f"{question.id} は {list(question.top_box or ())}）。"
                "T2B は代表1つの定義で全コンセプトぶん計算するので、揃っていないと"
                "コンセプトごとに違う定義の数字が同じ列に並ぶ",
            )


def _check_remember(survey: SurveyDefinition, report: ValidationReport) -> None:
    """`remember` の参照が解決できるか（E6, §5.1）。

    参照できるのは ask order（`slot` 昇順・定義順）で**自分より前**の設問だけ。
    まだ答えていない設問の回答は記憶になりえない。
    """
    order = ask_order(survey)
    known = {question.id for question in survey.questions}
    answered: set[str] = set()

    for question in order:
        answered.add(question.id)
        remember = question.remember
        if remember is None or remember.mode is not RememberMode.SELECTED:
            continue
        path = f"questions[{question.id}].remember"
        for qid in remember.question_ids:
            if qid == question.id:
                report.error("E6", f"{path}: 自分自身は指定できない")
            elif qid not in known:
                report.error("E6", f"{path}: {qid!r} という設問は無い")
            elif qid not in answered:
                report.error(
                    "E6",
                    f"{path}: {qid!r} はこの設問より後に聞かれる。"
                    "まだ答えていない設問の記憶は持てない（slot と記述順で並べ替えた順序で判定する）",
                )


def _check_quotas(survey: SurveyDefinition, report: ValidationReport) -> None:
    quotas = survey.panel.quotas
    _check_unique_ids([c.cell_id for c in quotas.cells], "panel.quotas.cells", report)

    if quotas.mode is QuotaMode.COUNT:
        for cell in quotas.cells:
            if cell.n is not None and cell.n <= 0:
                report.error("SURVEY", f"panel.quotas.cells[{cell.cell_id}]: n は1以上")
        total = sum(cell.n or 0 for cell in quotas.cells)
        if total != survey.panel.size:
            report.error(
                "SURVEY",
                f"割り付けの合計 {total} が panel.size {survey.panel.size} と一致しない",
            )
    else:
        for cell in quotas.cells:
            if cell.proportion is not None and cell.proportion <= 0:
                report.error("SURVEY", f"panel.quotas.cells[{cell.cell_id}]: proportion は0より大きい値")
        total = sum(cell.proportion or 0.0 for cell in quotas.cells)
        if abs(total - 1.0) > _PROPORTION_TOLERANCE:
            report.error("SURVEY", f"proportion の合計が {total} で 1.0 にならない")

    if survey.panel.size <= 0:
        report.error("SURVEY", "panel.size は1以上")


def _check_unimplemented(survey: SurveyDefinition, report: ValidationReport) -> None:
    """未実装の機能を黙って無視しないための検出。"""
    if survey.model.endpoint is Endpoint.AZURE_AI_FOUNDRY:
        report.error(
            "UNSUPPORTED",
            "model.endpoint: azure_ai_foundry はフェーズ1では未実装。"
            f"使えるのは {Endpoint.DATABRICKS} / {Endpoint.FAKE}",
        )


def _check_screener(survey: SurveyDefinition, report: ValidationReport) -> None:
    """スクリーナー定義の検証（§4.2）。"""
    screener = survey.screening
    if screener is None:
        return

    _check_screener_conditions(screener, report)

    if screener.oversample_factor < 1:
        report.error("SURVEY", "screening.oversample_factor は1以上")

    _check_infer(survey, screener, report)


def _check_screener_conditions(screener, report: ValidationReport) -> None:
    """`mode: assume` / `infer` の対象者条件。自然言語のまま使う（§4.2）。"""
    if not screener.conditions:
        report.error(
            "SURVEY",
            "screening.conditions が空。"
            "対象者条件が無いと誰にも何も付与できない",
        )
    for index, condition in enumerate(screener.conditions):
        if not condition.strip():
            report.error("SURVEY", f"panel.screener.conditions[{index}] が空文字")


def _check_infer(survey: SurveyDefinition, screener, report: ValidationReport) -> None:
    """判定設定（§4.2）。"""
    card = screener.persona_card
    if screener.batch_size < 1:
        report.error("SURVEY", "screening.batch_size は1以上")

    _check_persona_card(card, "screening.persona_card", report)

    if not (card.attributes or card.include_summary or card.persona_fields):
        report.error(
            "SURVEY",
            "screening.persona_card: 判定に渡す情報が何も無い。"
            "attributes / include_summary / persona_fields のいずれかを指定すること",
        )


def _check_persona_card(card, path: str, report: ValidationReport) -> None:
    """ペルソナカードに書かれた列が `personas_base` にあるか（§6.1）。

    無い列を指定しても実行時は黙って空になるだけなので、ここで止める。
    """
    from persona_sim.personas.build import PERSONAS_BASE_COLUMNS

    for attribute in card.attributes:
        if attribute.field not in PERSONAS_BASE_COLUMNS:
            report.error(
                "SURVEY",
                f"{path}.attributes: {attribute.field!r} は personas_base に無い列。"
                f"指定できるのは {', '.join(PERSONAS_BASE_COLUMNS)}",
            )
    for persona_field in card.persona_fields:
        if persona_field.field not in PERSONAS_BASE_COLUMNS:
            report.error(
                "SURVEY",
                f"{path}.persona_fields: {persona_field.field!r} は "
                f"personas_base に無い列。指定できるのは {', '.join(PERSONAS_BASE_COLUMNS)}",
            )


def _check_prompt(survey: SurveyDefinition, report: ValidationReport) -> None:
    """ペルソナカードに載せる列が `personas_base` に存在すること（§6.1）。"""
    _check_persona_card(survey.persona_card, "main_survey.persona_card", report)


def _check_output(survey: SurveyDefinition, report: ValidationReport) -> None:
    """集計軸と出力形式（§7）。綴り違いを黙って捨てない。

    軸が黙って無視されると、集計表からその軸が消えたことに実行後まで気づけない。
    """
    from persona_sim.aggregate import segments as segment_axes
    from persona_sim.aggregate.export import FORMAT_CSV, FORMAT_XLSX

    for segment in survey.output.segments:
        unknown = segment_axes.unknown_axes(segment)
        if unknown:
            available = ", ".join([segment_axes.TOTAL, *segment_axes.ATTRIBUTE_AXES])
            report.error(
                "SURVEY",
                f"output.segments: {segment!r} に使えない軸がある（{', '.join(unknown)}）。"
                f"指定できるのは {available}、および `_x_` でつないだ組み合わせ"
                f"（例: sex{segment_axes.COMPOSITE_SEPARATOR}age_band_10）",
            )

    known_formats = (FORMAT_CSV, FORMAT_XLSX)
    for output_format in survey.output.formats:
        if output_format not in known_formats:
            report.error(
                "SURVEY",
                f"output.formats: {output_format!r} は未対応。"
                f"指定できるのは {', '.join(known_formats)}",
            )


def _check_unique_ids(ids: list[str], path: str, report: ValidationReport) -> None:
    # `ids.count()` を id ごとに呼ぶと設問数の2乗になる。1回の走査で数える。
    duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicates:
        report.error("SURVEY", f"{path}: id が重複している: {', '.join(duplicates)}")


# --------------------------------------------------------------------------- #
# 抽出可能性（Spark が必要）
# --------------------------------------------------------------------------- #


def validate_feasibility(
    spark: SparkSession,
    personas: DataFrame,
    survey: SurveyDefinition,
) -> ValidationReport:
    """割り付けが埋まるかを確認する（E1, §4.1）。

    セル間の重複除外を適用した後の候補数で判定するため、実際の選抜と同じ経路を通す。
    """
    from persona_sim.errors import InsufficientCandidatesError
    from persona_sim.panel.sampling import select_members

    report = ValidationReport()
    try:
        _, selection = select_members(spark, personas, survey)
    except InsufficientCandidatesError as exc:
        report.error("E1", str(exc))
        return report

    for cell_id, excluded in selection.overlap_excluded.items():
        if excluded:
            report.warn(
                "W_CELL_OVERLAP",
                f"セル {cell_id}: 他セルで確定済みのペルソナ {excluded} 件を候補から除外した。"
                "割り付けセルの条件が重なっている可能性がある",
            )
    return report
