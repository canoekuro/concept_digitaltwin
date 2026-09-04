"""調査定義のデータモデル（`SPEC_PHASE1.md` §3）。

このモジュールは **pyspark に依存しない**。調査定義の妥当性検証の大半を
Spark を起動せずに単体テストできるようにするため。Spark の述語への変換は
`persona_sim.panel.quotas` が担う。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from persona_sim.errors import SurveyDefinitionError


class SurveyType(StrEnum):
    """調査の種類（§3）。

    何を聞く調査だったのかを実行後に判別できるようにするための識別子。
    集計や画面の既定を種別ごとに切り替える起点にもなる。

    **自由文字列にしない。** 綴り違いがそのまま `runs` に残ると、あとから種別ごとに
    集計できなくなる。種別を増やすときはここに1行足す。
    """

    #: コンセプト調査。案を提示して購入意向・新規性などを聞く。
    CONCEPT = "concept"


class RememberMode(StrEnum):
    """1設問がどこまで記憶を持って回答するか（§5.1）。

    **意味は調査定義の中だけで完結する。** 他の設定を見て変わることはない。
    """

    #: 何も持たない（既定）。この設問だけで独立したセッションになる。
    NONE = "none"
    #: それまでに聞いた設問すべて。**コンセプトをまたぐ**。
    #: 「同じコンセプトの中だけ」にしたいなら設問IDを並べて書く——そのための
    #: 専用の値は用意しない。コンセプト調査は数ある調査の一種でしかなく、
    #: その都合をここに持ち込むと `all` の意味が読む人によって変わる。
    ALL = "all"
    #: 列挙した設問だけ。**推移しない**——Q4 が Q3 を、Q3 が Q1 を覚えていても、
    #: Q4 が見るのは Q3 だけ。Q1 も要るなら Q4 に両方書く。
    SELECTED = "selected"


@dataclass(frozen=True)
class Remember:
    """`questions[].remember` の解釈結果。

    `question_ids` は `SELECTED` のときだけ意味を持つ。順序は調査定義に書いた順ではなく
    **ask order（`slot` 昇順・定義順）に整列してから**プロンプトへ再生する
    （`persona_sim.run.memory` を参照）。
    """

    mode: RememberMode
    question_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode is not RememberMode.SELECTED and self.question_ids:
            raise SurveyDefinitionError(
                f"remember: {self.mode} では question_ids を持てない"
            )

    @property
    def retains(self) -> bool:
        """何かしら記憶を持つか。先行設問が無ければ実際には何も持たないが、
        設定としては「持つ」側に数える（順序効果が起きうるかの判定に使う）。"""
        return self.mode is not RememberMode.NONE

    def describe(self) -> str | list[str]:
        """実行メタデータに残す形（§9）。"""
        if self.mode is RememberMode.SELECTED:
            return list(self.question_ids)
        return str(self.mode)


#: 何も書かなかった設問の記憶。**調査全体の設定からは引かない。**
NO_MEMORY = Remember(RememberMode.NONE)


class QuotaMode(StrEnum):
    COUNT = "count"
    PROPORTION = "proportion"


class Endpoint(StrEnum):
    """推論エンドポイントの種別（§3）。"""

    #: Databricks Model Serving。
    DATABRICKS = "databricks"
    #: 決定論的なダミー。実エンドポイント無しでパイプラインを通すために使う。
    FAKE = "fake"
    #: フェーズ1では未実装。指定されたら `validate` が停止させる。
    AZURE_AI_FOUNDRY = "azure_ai_foundry"


class StructuredOutput(StrEnum):
    """構造化出力の使い方（§6.3）。"""

    #: 試して、駄目なら正規表現パースへ1回だけ切り替え、その事実を記録する。
    AUTO = "auto"
    #: 使えなければ停止する。
    ALWAYS = "always"
    #: 最初から正規表現パース。
    NEVER = "never"


class ImageMode(StrEnum):
    """画像の渡し方（§5.4 → 現 §5.5）。"""

    NATIVE = "native"
    TEXT = "text"
    NONE = "none"


class QuestionType(StrEnum):
    """フェーズ1で対応する設問タイプ（§3.1）。

    `rank` / `maxdiff` / コンジョイントはフェーズ2以降のため、ここに含めない。
    """

    SINGLE = "single"
    MULTI = "multi"
    SCALE = "scale"
    OPEN = "open"
    NUMERIC = "numeric"


#: フェーズ1で非対応の設問タイプ。指定されたら理由を添えて停止する。
UNSUPPORTED_QUESTION_TYPES = ("rank", "maxdiff", "conjoint")

#: 1リクエストのタイムアウト（秒）の既定。**databricks-sdk の既定値と同じ値**を明示的に持つ。
#:
#: SDK に委ねたままだと、何秒で切れるのかが調査定義にも `run_metadata.json` にも現れず、
#: 「モデルの応答が遅い」のか「タイムアウトで切られた」のかを後から判断できない。
#: 値そのものは SDK の既定と同じなので、明示しても挙動は変わらない。
DEFAULT_REQUEST_TIMEOUT_SEC = 60

#: `questions[].reasoning_max_length` の既定（§6.3）。
#: 指示行にだけ入る値で、実際の打ち切りは `model.max_tokens_reasoning` が担う。
DEFAULT_REASONING_MAX_LENGTH = 80

#: スクリーナーで方式に合わない書き方をしたときの案内（§4.2）。
#:
#: `assume` / `infer` は選択肢を提示しない。`options` と `pass_if` を書かせても
#: 使い道が無く、「設定したつもり」の記録だけが残るため、自然言語の `conditions` に寄せた。
MISPLACED_SCREENER_KEYS = {
    "questions": (
        "mode: assume / infer では questions: を書けない。選択肢を提示しないため"
        " options や pass_if に意味が無い。対象者条件は conditions: に自然言語で書くこと"
        "（questions[].premise に書いていた文言をそのまま移せばよい）。"
    ),
    "conditions": (
        "mode: ask では conditions: を書けない。実際に選択肢を見せて答えさせるため、"
        " questions: に text / options / pass_if を書くこと。"
        "ペルソナカードに載せる前提文は questions[].premise で指定する。"
    ),
}


#: `mode: infer` では書けなくなったキーと、その行き先（§4.2）。
#:
#: `logic` は以前、判定プロンプトに「すべての条件を満たす人物を選んでください」という
#: 一文を割り込ませていた。設定から触れないその文が `prompt.rule` の直前という最も効く
#: 位置に入るため、`prompt` をどう書き換えても判定が動かなかった。文を消した以上
#: `logic` は `infer` で何もしないので、黙って無視せず書けなくする。
UNUSED_INFER_SCREENER_KEYS = {
    "logic": (
        "mode: infer では logic: を書けない。判定の指示は screening.prompt.rule に"
        "一本化した（すべての条件を満たすのか、いずれかで足りるのかは、その文中に書くこと）。"
        "以前は logic から判定プロンプトに一文を差し込んでいたが、prompt.rule の直前に"
        "入るため prompt を書き換えても効かなくなっていた。"
        " logic: が効くのは mode: ask（pass_if の結合方法）だけ。"
    ),
}


#: `model` / `infer.model` から廃止したキーと、その理由。
REMOVED_MODEL_FIELDS = {
    "temperature": (
        "temperature は廃止した。最新モデルではサンプリングパラメータの指定が"
        "非推奨・無効化される傾向にあるため、リクエストに載せずエンドポイント既定に"
        "任せる（§6.3）。この行を削除すること。"
    ),
}


@dataclass(frozen=True)
class PersonaFilter:
    """`personas_base` に対する属性条件（§3 の `panel.filters` と割り付けセル条件で共用）。

    すべて None のフィールドは条件なしとして扱う。
    """

    sex: str | None = None
    age_min: int | None = None
    age_max: int | None = None
    prefecture_in: tuple[str, ...] | None = None
    region_in: tuple[str, ...] | None = None
    area_in: tuple[str, ...] | None = None
    marital_status_in: tuple[str, ...] | None = None
    education_level_in: tuple[str, ...] | None = None


@dataclass(frozen=True)
class QuotaCell:
    """割り付けセル。`n`（count モード）か `proportion`（proportion モード）のどちらかを持つ。"""

    cell_id: str
    conditions: PersonaFilter
    n: int | None = None
    proportion: float | None = None


@dataclass(frozen=True)
class Quotas:
    mode: QuotaMode
    cells: tuple[QuotaCell, ...]


class ScreenerMode(StrEnum):
    """対象者条件の満たし方（§4.2）。費用と、得られるものが違う。"""

    #: 実際に聞いて通過者だけを残す。インシデンスを実測できる。
    ASK = "ask"
    #: 聞かずに条件を前提として全候補に与える。費用は0だが、インシデンスは測れない。
    ASSUME = "assume"
    #: 別のセッションの LLM に候補をまとめて渡し、条件に合致する蓋然性が高い者だけに
    #: 属性を与える。`ask` より安く、`assume` のような全員への強制も避けられる。
    #: 聞いてはいないので通過率は**推定値**であり、実測値とは別枠に記録する。
    INFER = "infer"


class ScreenerLogic(StrEnum):
    """複数設問がある場合の通過条件。"""

    ALL = "all"
    ANY = "any"


@dataclass(frozen=True)
class ScreenerQuestion:
    id: str
    text: str
    type: QuestionType
    options: tuple[str, ...]
    pass_if: tuple[int, ...]
    #: ペルソナカードに載せるときの短い見出し。省略時は `text` を使う。
    label: str | None = None
    #: 通過者のペルソナカードに載せる前提文。省略時は設問文と通過選択肢から組み立てる。
    premise: str | None = None

    def card_label(self) -> str:
        return self.label or self.text


@dataclass(frozen=True)
class InferModelOverrides:
    """判定に使うモデルの部分指定（§4.2）。

    None のフィールドは調査本体の `model` を引き継ぐ。全項目を書き直させると
    「本体と同じにしたつもりが違っていた」事故が起きるため、差分だけを持つ。
    """

    endpoint: Endpoint | None = None
    deployment: str | None = None
    max_tokens: int | None = None
    concurrency: int | None = None
    structured_output: StructuredOutput | None = None

    def apply(self, base: ModelConfig) -> ModelConfig:
        """調査本体の `model` に、書かれたキーだけを重ねる。

        `thinking` は重ねない。常に OFF（`AGENTS.md` 不変条件）。
        """
        overrides = {
            name: value
            for name, value in vars(self).items()
            if value is not None
        }
        return replace(base, thinking=False, **overrides)


#: ペルソナカードに載せるナラティブ列の既定（§6.1）。
DEFAULT_PERSONA_FIELDS: tuple[tuple[str, str], ...] = (
    ("cultural_background", "生活背景"),
    ("professional_persona", "仕事"),
    ("hobbies_and_interests", "関心事"),
    ("culinary_persona", "食まわり"),
)


@dataclass(frozen=True)
class PersonaField:
    """ペルソナカードの1ブロック。`【label】value` の形で出す。"""

    field: str
    label: str


@dataclass(frozen=True)
class PersonaAttribute:
    """属性行に載せる1項目（§6.1）。`{値}{suffix}` の形で中黒区切りに連ねる。

    `suffix` を設定に持たせているのは、値だけでは何の数字か読めない項目があるため
    （`age` の「歳」、`prefecture` の「在住」）。値が空の項目は行ごと詰める。
    """

    field: str
    suffix: str = ""


#: 属性行の既定（§6.1）。従来ハードコードしていた6項目と同じ並び・同じ接尾辞。
DEFAULT_PERSONA_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("sex", ""),
    ("age", "歳"),
    ("prefecture", "在住"),
    ("marital_status", ""),
    ("education_level", ""),
    ("occupation_raw", ""),
)


@dataclass(frozen=True)
class PersonaCardConfig:
    """ペルソナカードに何を載せるか（§6.1）。本調査とスクリーニングが同じ形で持つ。

    真偽値の `include_attributes` は置かない。`attributes` を空にすれば属性行が出ないので、
    真偽値とリストの両方を持つと `include_attributes: false` と非空の `attributes` が
    矛盾しうる。総括文は対応するリストが無いので `include_summary` のまま残す。

    **載せる列は載せるだけでは足りない。** ここに書いた `field` は
    `persona_sim.personas.load` がドライバへ集める列にも入っていなければ、
    値が来ずに黙って空になる（`required_persona_columns()` が面倒を見る）。
    """

    attributes: tuple[PersonaAttribute, ...] = tuple(
        PersonaAttribute(field=field, suffix=suffix) for field, suffix in DEFAULT_PERSONA_ATTRIBUTES
    )
    include_summary: bool = True
    persona_fields: tuple[PersonaField, ...] = tuple(
        PersonaField(field=field, label=label) for field, label in DEFAULT_PERSONA_FIELDS
    )

    def column_names(self) -> tuple[str, ...]:
        """このカードを描くのに要る `personas_base` の列。"""
        summary = ("persona",) if self.include_summary else ()
        return (
            *(attribute.field for attribute in self.attributes),
            *summary,
            *(persona_field.field for persona_field in self.persona_fields),
        )


#: `[system]` ブロックの既定（§6.1）。
DEFAULT_SYSTEM_PROMPT = """あなたはこれから提示する人物になりきって、調査に回答します。
- この人物の実際の生活実感に即して答えてください
- 調査に協力的すぎる態度をとらないでください。
  興味のない対象には率直に興味がないと答えてください
- 指定された形式のみで回答し、説明や前置きは書かないでください"""


#: `mode: infer` の判定プロンプトの `[system]` 既定（§4.2）。
#: 回答生成と違い「なりきる」のではなく、外から見て判断させる。
DEFAULT_INFER_SYSTEM_PROMPT = """あなたは調査対象者の選定を担当します。
複数の人物像と対象者条件を読み、条件に当てはまる蓋然性が高い人物を選んでください。
- 断定できなくてよいので、書かれている情報から見て可能性が高い人物を選んでください
- 条件に無関係な理由で人数を調整しないでください
- 指定された形式のみで回答し、説明や前置きは書かないでください"""


@dataclass(frozen=True)
class PromptHeadings:
    """プロンプト各ブロックの見出し（§6.1）。

    前提ブロックの見出しを方式ごとに分けているのは、プロンプトを読んだときに
    「本人が答えたこと」と「こちらが与えた条件」を取り違えないようにするため。
    """

    profile: str = "■あなたのプロフィール"
    stimulus: str = "■提示物"
    question: str = "■設問"
    #: `ask`: 本人が実際に答えた内容。
    ask_premise: str = "調査前の確認"
    #: `assume`: こちらが与えた条件。
    assume_premise: str = "前提"
    #: `infer`: 別の LLM が「合致する蓋然性が高い」と判断して与えた条件。
    #: `ask` の実回答とも `assume` の一律付与とも由来が違うので、見出しを分ける。
    infer_premise: str = "推定前提"


#: `mode: infer` の判定プロンプトの指示行の既定（§4.2）。
#: 設問タイプごとの回答指示文（`PromptRules`）とは別物なので分けて持つ。
DEFAULT_INFER_RULE = (
    "上記の条件に当てはまる蓋然性が高い人物の番号を、カンマ区切りですべて挙げてください。"
    "該当者がいない場合は「なし」と答えてください。番号以外は書かないでください。"
)


@dataclass(frozen=True)
class ReasoningRules:
    """`reasoning: true` の設問で使う回答指示文（§6.3）。**設問タイプごとに持つ。**

    `PromptRules` と同じキー名で並べ、設問タイプの指示行を1対1で置き換える。
    併記しないのは、「番号のみで答えてください」と「理由を書いてから番号を選んで
    ください」が矛盾するため。

    **1本の文で全タイプを賄わない。** 置き換えである以上、設問タイプ固有の指示は
    そこに書き直さなければ落ちる——共通の1文にしていた頃、`multi` の
    「すべて、カンマ区切りで」が消え、複数回答なのに1つだけ選ばせる問いになっていた。
    キーを揃えておけば、書き換える人にも「どのタイプの指示を置き換えているのか」が見える。

    理由を書かせられるのは番号で答える設問だけなので、ここに並ぶのは
    `single` / `scale` / `multi` の3つ（`REASONING_QUESTION_TYPES` はこの並びから引く）。
    3つとも `{max_length}` を埋め込める（`questions[].reasoning_max_length` が入る）。
    """

    single: str = (
        "まず、そう考えた理由を{max_length}文字以内で書き、"
        "そのうえで当てはまる番号を1つ選んでください。"
    )
    scale: str = (
        "まず、そう考えた理由を{max_length}文字以内で書き、"
        "そのうえで当てはまる番号を1つ選んでください。"
    )
    multi: str = (
        "まず、そう考えた理由を{max_length}文字以内で書き、"
        "そのうえで当てはまる番号をすべて挙げてください。"
    )

    def for_type(self, question_type: QuestionType) -> str:
        """設問タイプに対応する指示行のテンプレートを返す。

        指示行を持たない型（`open` / `numeric`）は理由を書かせられない。既定へ黙って
        落とすと、設問タイプの指示が消えたまま聞くことになるので送出する。
        `validate` が `_check_reasoning()` で先に止めるため、ここへは届かないはず。
        """
        rule = getattr(self, str(question_type), None)
        if rule is None:
            raise SurveyDefinitionError(
                f"type: {question_type} に reasoning は書けない。理由を書かせられるのは"
                f"番号で答える設問（{', '.join(self.__dataclass_fields__)}）のみ"
            )
        return rule


@dataclass(frozen=True)
class PromptRules:
    """設問タイプごとの回答指示文（§6.1）。

    `open` だけ `{max_length}` を埋め込める。`open` は `max_length` が未指定の設問では
    指示行そのものを出さない。

    スクリーニング判定の指示行はここには入らない（`ScreeningPromptConfig.rule`）。
    設問タイプではないものを混ぜると `for_type()` から引けるように見えてしまう。

    **`reasoning` は設問タイプではない**ので、同じ並びには置かない。設問タイプの指示行を
    差し替える表として入れ子で持つ（`ReasoningRules`）。
    """

    single: str = "番号のみで答えてください。"
    scale: str = "番号のみで答えてください。"
    multi: str = "当てはまる番号をすべて、カンマ区切りで答えてください。"
    open: str = "{max_length}文字以内で答えてください。"
    numeric: str = "数値のみで答えてください。"
    #: `questions[].reasoning: true` の設問で、上の指示行の**代わりに**使う表（§6.3）。
    reasoning: ReasoningRules = field(default_factory=ReasoningRules)

    def for_type(self, question_type: QuestionType) -> str:
        """設問タイプに対応する指示行のテンプレートを返す。"""
        return getattr(self, str(question_type))

    def for_question(self, question: Question) -> str:
        """その設問で実際に使う指示行のテンプレート。

        `reasoning: true` なら、同じ設問タイプの `reasoning` 側の指示行に置き換える。
        """
        if question.reasoning:
            return self.reasoning.for_type(question.type)
        return self.for_type(question.type)


#: `reasoning: true` を書ける設問タイプ（§6.3）。番号を返す設問だけが対象で、
#: `open` / `numeric` は本文そのものが回答なので理由を分ける意味が無い。
#:
#: **`ReasoningRules` の並びから引く。** 指示行を持つ型と書ける型は同じものなので、
#: 別々に列挙すると片方だけ増えたときに、指示行の無い型で理由を書かせられてしまう。
REASONING_QUESTION_TYPES: tuple[QuestionType, ...] = tuple(
    QuestionType(name) for name in ReasoningRules.__dataclass_fields__
)


@dataclass(frozen=True)
class PromptConfig:
    """本調査のプロンプト組み立て設定（`main_survey.prompt`、§6.1）。

    版管理は行わない（`template_version` は廃止）。生成AIの回答には再現性が無く、
    版で過去と比較する前提が成立しにくいうえ、利用者がプロンプトを自由に書き換える
    運用を想定している。

    ペルソナカードの中身は持たない（`main_survey.persona_card`）。スクリーニングの
    プロンプトも持たない（`screening.prompt`）。
    """

    #: `systems` を指さない設問に使う既定の `[system]`。
    system: str = DEFAULT_SYSTEM_PROMPT
    #: 設問ごとに使い分ける `[system]` を名前で並べたもの（`questions[].system` から引く）。
    #:
    #: **文面を設問側に書かせない。** 設問はコンセプトの数だけ `slot` 展開されるので
    #: （§3）、直書きすると同じ長文が展開数ぶん複製され、直すときに1箇所でも取りこぼすと
    #: 案によって違うプロンプトで聞いたことになる。名前で参照させれば実体は1つに保てる。
    systems: Mapping[str, str] = field(default_factory=dict)
    #: 前提ブロックの見出し（`ask_premise` 等）もここ。スクリーニング由来の名前だが、
    #: 描かれるのは本調査のペルソナカード末尾なので本調査側に置く（§6.1）。
    headings: PromptHeadings = field(default_factory=PromptHeadings)
    rules: PromptRules = field(default_factory=PromptRules)

    def system_for(self, key: str | None) -> str:
        """設問に対応する `[system]` を返す。`key` が None なら既定（`system`）。

        未定義の名前は読み込みの時点で止めてある（`panel/loader.py` の
        `_reject_unknown_system_prompts()`）ので、ここでは素引きしてよい。
        既定へ黙って落とすと、書き分けたつもりで既定のまま聞くことになる。
        """
        if key is None:
            return self.system
        return self.systems[key]


@dataclass(frozen=True)
class ScreeningPromptConfig:
    """スクリーニング判定のプロンプト設定（`screening.prompt`、§4.2）。

    本調査の `prompt` と分けているのは、回答生成と判定で役割が違うため。
    なりきらせるのではなく外から見て判断させる。
    """

    system: str = DEFAULT_INFER_SYSTEM_PROMPT
    #: 判定の指示行。1本しか無いので単数形。
    rule: str = DEFAULT_INFER_RULE


#: 判定に見せるペルソナカードの既定（§4.2）。ナラティブ列は**載せない**。
#: 回答生成用のカードをそのまま流用すると1回の判定プロンプトが長くなり、
#: この方式の費用面の利点が消えてしまうため、既定は属性行と総括だけ。
def _default_screening_persona_card() -> PersonaCardConfig:
    return PersonaCardConfig(persona_fields=())


@dataclass(frozen=True)
class ScreeningConfig:
    """スクリーニング定義（`screening:`、§4.2）。

    **方式ごとに必要な形が違うので、書く場所を分けている。**

    - `ask` … `questions`。本人に選択肢を見せて答えさせるので、選択肢と通過判定が要る
    - `assume` / `infer` … `conditions`。自然言語の対象者条件だけでよい。
      `assume` はそれを前提として与え、`infer` はそれを判定用 LLM に見せる。
      どちらも選択肢を提示しないので、`options` や `pass_if` を書かせる意味がない

    並びは本調査（`main_survey`）と対称に model → prompt → persona_card → その他。
    """

    #: 判定に使うモデル。省略したキーは `main_survey.model` を引き継ぐ（`infer` のみ）。
    model: InferModelOverrides = field(default_factory=lambda: InferModelOverrides())
    #: 判定プロンプト（`infer` のみ）。
    prompt: ScreeningPromptConfig = field(default_factory=ScreeningPromptConfig)
    #: 判定に見せるペルソナ像（`infer` のみ）。
    persona_card: PersonaCardConfig = field(default_factory=_default_screening_persona_card)
    mode: ScreenerMode = ScreenerMode.ASK
    #: `ask` のスクリーナー設問。他の方式では空。
    questions: tuple[ScreenerQuestion, ...] = ()
    #: `assume` / `infer` の対象者条件（自然言語）。`ask` では空。
    conditions: tuple[str, ...] = ()
    #: `ask` 専用。複数の設問の通過判定をどう結合するか。`infer` では判定の指示を
    #: `prompt.rule` に一本化しているので効かない（loader が書くことを止める）。
    logic: ScreenerLogic = ScreenerLogic.ALL
    #: `ask` / `infer` のときのみ意味を持つ。何倍の候補を抽出して判定するか。
    oversample_factor: int = 4
    #: 1回の判定に渡すペルソナ数（`infer` のみ）。
    batch_size: int = 20

    def condition_texts(self) -> tuple[str, ...]:
        """対象者条件の文言。方式によらずここから取る。

        `ask` は設問から組み立てる（`premise` があればそれ、無ければ設問文と通過選択肢）。
        `assume` / `infer` は `conditions` をそのまま使う。**言い換えはしない。**
        """
        if not self.asks:
            return self.conditions
        lines = []
        for question in self.questions:
            if question.premise:
                lines.append(question.premise)
                continue
            passing = [
                question.options[code - 1]
                for code in question.pass_if
                if 1 <= code <= len(question.options)
            ]
            label = question.card_label()
            lines.append(f"{label}: {'、'.join(passing)} のいずれか" if passing else label)
        return tuple(lines)

    @property
    def asks(self) -> bool:
        """本人に聞くか。インシデンスを実測できるのはこの方式だけ。"""
        return self.mode is ScreenerMode.ASK

    @property
    def infers(self) -> bool:
        return self.mode is ScreenerMode.INFER

    @property
    def calls_llm(self) -> bool:
        """判定に LLM を呼ぶか。`assume` だけが呼ばない。"""
        return self.mode in (ScreenerMode.ASK, ScreenerMode.INFER)


@dataclass(frozen=True)
class PanelConfig:
    """パネル構築の設定（`panel:`）。

    スクリーニングは最上位の `screening:` に置く（`SurveyDefinition.screening`）。
    抽出倍率はここに影響するが、判定のモデル・プロンプト・ペルソナカードと同じ
    ブロックにまとめておかないと、スクリーニングの設定が2箇所に散る。
    """

    size: int
    seed: int
    quotas: Quotas
    filters: PersonaFilter = field(default_factory=PersonaFilter)


@dataclass(frozen=True)
class Stimulus:
    id: str
    name: str
    text: str
    #: 画像の置き場所。フェーズ1の実行環境は Databricks なので
    #: Unity Catalog Volumes のパス（`/Volumes/{catalog}/{schema}/{volume}/...`）で書く。
    image_uri: str | None = None
    image_mode: ImageMode = ImageMode.NONE


@dataclass(frozen=True)
class Question:
    """1設問。**実行時にも1回しか聞かれない**（§3.1）。

    設問はコンセプトごとに繰り返されるテンプレートではなく、`slot` で
    「そのペルソナが何番目に見るコンセプトについて聞くか」に紐づく。だから
    `id` は調査定義の中でも1ペルソナの回答履歴の中でも一意で、`remember` から
    設問IDだけで曖昧さなく参照できる。
    """

    id: str
    text: str
    type: QuestionType
    options: tuple[str, ...] = ()
    randomize_options: bool = False
    top_box: tuple[int, ...] | None = None
    max_length: int | None = None
    #: 何番目に提示するコンセプトについて聞くか（1始まり）。実行時に
    #: `panels.assigned_stimuli[slot - 1]` を引く。**コンセプトIDではなく提示順の位置**
    #: なので、パネルの割り当てが変わっても設問側を書き換えずに済む。
    slot: int = 1
    #: コンセプト横断で「同じ設問」として束ねるキー（§8）。省略時は `id` と同じ。
    #: slot ごとに設問を展開すると `id` が別物になるので、コンセプト比較表を組むには
    #: 何と何が同じ問いなのかを別に持つ必要がある。
    measure: str | None = None
    #: どの設問の記憶を持ったままこの設問に入るか（§5.1）。既定は何も持たない。
    remember: Remember = NO_MEMORY
    #: この設問を聞くときの `[system]`。`main_survey.prompt.systems` の**名前**であって
    #: 文面ではない（§6.1）。省略すると `main_survey.prompt.system` で聞く。
    #: 購入意向と新規性で指示を変えたい、のように設問ごとに役割が違う場合に使う。
    system: str | None = None
    #: 構造化出力に `reasoning` を足し、理由を書かせてから番号を選ばせるか（§6.3）。
    #: 選択式（single / scale / multi）でのみ有効。思考モード（`model.thinking`）とは
    #: 別物で、**書かれた理由は `responses.answer_reasoning` に残る**。
    reasoning: bool = False
    #: 理由の字数。`prompt.rules.reasoning` の**設問タイプ側**の `{max_length}` に入る。
    #: JSON Schema には入れない（`maxLength` の strict 対応がエンドポイント依存で、
    #: 拒否されると `structured_output: always` の下で調査が止まる）。
    reasoning_max_length: int = DEFAULT_REASONING_MAX_LENGTH

    @property
    def measure_key(self) -> str:
        """集計上の同一性キー。`measure` 未指定なら `id` そのもの。"""
        return self.measure or self.id

    def is_ordinal(self) -> bool:
        """順序尺度とみなすか。

        非順序かどうかは選択肢の文字列からは自動判定できない。そこで
        「`scale` 型である」「`top_box` が定義されている」のどちらかを満たすものを
        順序尺度とみなし、選択肢のシャッフルを禁止する（`AGENTS.md` 不変条件）。
        """
        return self.type is QuestionType.SCALE or bool(self.top_box)


@dataclass(frozen=True)
class ModelConfig:
    """本調査のモデル設定（`main_survey.model`、§3）。

    **`max_tokens_*` は設問側の字数（`max_length` / `reasoning_max_length`）と別物。**
    こちらはトークンで、エンドポイント側の絶対の打ち切り。あちらは文字数で、
    モデルへ渡す指示文に入る目安。文字とトークンの比はモデルとトークナイザで変わるので、
    片方から他方を導出しない——係数を実装に埋めると、ずれたときに設定から読み取れなくなる。
    噛み合っていない組み合わせは `validate` が `W_OUTPUT_BUDGET` で警告する。
    """

    endpoint: Endpoint
    deployment: str
    thinking: bool = False
    #: 選択式設問の上限。番号だけ返させるので小さくてよい。
    max_tokens: int = 8
    #: 自由回答（`type: open`）はこちらを使う（§3）。
    max_tokens_open: int = 512
    #: `reasoning: true` の選択式設問はこちらを使う（§6.3）。理由の散文を書かせるので
    #: 番号だけの `max_tokens` では必ず足りない。
    max_tokens_reasoning: int = 512
    concurrency: int = 32
    structured_output: StructuredOutput = StructuredOutput.AUTO
    #: 1リクエストのタイムアウト（秒）。`DEFAULT_REQUEST_TIMEOUT_SEC` を参照。
    request_timeout_sec: int = DEFAULT_REQUEST_TIMEOUT_SEC


@dataclass(frozen=True)
class OutputConfig:
    segments: tuple[str, ...] = ("total",)
    #: 既定でファイルを出す。集計結果はテーブルに保存しないので、書かないと何も残らない。
    formats: tuple[str, ...] = ("csv", "xlsx")


@dataclass(frozen=True)
class SurveyDefinition:
    """調査定義の全体。システムへの唯一の入力（§3）。"""

    survey_id: str
    name: str
    panel: PanelConfig
    stimuli: tuple[Stimulus, ...]
    questions: tuple[Question, ...]
    #: 本調査のモデル（`main_survey.model`）。
    model: ModelConfig
    output: OutputConfig
    #: 調査の種類（`survey.type`）。既定は `concept`。
    #: `survey_id` / `name` の隣に置きたいが、既定値を持つフィールドは
    #: 既定値の無いフィールドより後ろにしか置けない（dataclass の制約）。
    survey_type: SurveyType = SurveyType.CONCEPT
    #: 本調査のプロンプト（`main_survey.prompt`）。
    prompt: PromptConfig = field(default_factory=PromptConfig)
    #: 本調査のペルソナカード（`main_survey.persona_card`）。
    persona_card: PersonaCardConfig = field(default_factory=PersonaCardConfig)
    #: スクリーニング（`screening:`）。無い場合は None。
    screening: ScreeningConfig | None = None
    #: 実行記録から**集計に要る範囲だけ**復元したものか（`loader.survey_from_record()`）。
    #:
    #: True のとき `model` / `prompt` / `persona_card` は記録を読んでおらず
    #: 既定値のまま入っている。**この定義で実行してはならないし、何をどう聞いたかの
    #: 根拠にもしてはならない。** 実際の設定は `raw`（＝定義の全文）と
    #: `runs.metadata_json` に残っている。
    from_record: bool = False
    #: 読み込んだ元の内容。`runs` への記録用に全文を保持する（§9）。
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def stimuli_count(self) -> int:
        return len(self.stimuli)

    @property
    def stimuli_per_persona(self) -> int:
        """1ペルソナが評価するコンセプト数。**全案を1件ずつ順に評価する**（§5）。

        提示設計は反実仮想モナディック固定で、設定では変えられない。同じ人に同じ
        コンセプトが2回当たることはなく（§5.0）、コンセプトごとの評価者数は揃う。
        """
        return len(self.stimuli)
