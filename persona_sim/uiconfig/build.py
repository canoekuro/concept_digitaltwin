"""画面入力と UI 設定から調査定義を組み立てる（`docs/SPEC_UI.md` §4）。

```
config/ui_config.yaml ─┐
                       ├→ build_survey() → 調査定義（§3）→ panel → screen → run → aggregate
画面入力（SurveyForm）─┘
```

**組み立てた dict は `survey_from_dict()` に通す。** 検証を自前で書かず、CLI・ノートブックと
同じ経路に乗せるため。UI 経由と調査定義ファイル経由で通る検証が違ってはいけない。
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from persona_sim.panel.loader import survey_from_dict
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.uiconfig.allocation import build_quotas
from persona_sim.uiconfig.census import CensusRow
from persona_sim.uiconfig.schema import SurveyForm, UIConfig, UIConfigError

#: コンセプトの `stimulus_id`。画面では名称しか入力しないので連番で振る。
_STIMULUS_PREFIX = "c"

#: `survey_id` に使える文字。Delta のパーティション値やファイル名に入るため絞る。
_ID_SAFE = re.compile(r"[^a-z0-9_]+")

#: `survey_id` 末尾の識別子のバイト数（16進で倍の桁になる）。
_ID_SUFFIX_BYTES = 3


def build_survey(
    ui: UIConfig,
    form: SurveyForm,
    census: Sequence[CensusRow] | None = None,
) -> SurveyDefinition:
    """設定と画面入力を合成し、検証済みの調査定義を返す。"""
    return survey_from_dict(build_survey_dict(ui, form, census))


def build_survey_pair(
    ui: UIConfig,
    form: SurveyForm,
    census: Sequence[CensusRow] | None = None,
) -> tuple[dict[str, Any], SurveyDefinition]:
    """調査定義を dict と検証済みオブジェクトの**両方**で返す。

    `form.survey_id` が未指定のとき `build_survey_dict()` は呼ぶたびに現在時刻から
    `survey_id` を作り直す。画面用と投入用で別々に呼ぶと、利用者に見せた ID と
    ジョブが結果を書き込む ID が食い違い、結果閲覧で自分の調査を特定できなくなる。
    **同じ dict から両方を作る**ことでその食い違いを構造的に防ぐ。
    """
    built = build_survey_dict(ui, form, census)
    return built, survey_from_dict(built)


def build_survey_dict(
    ui: UIConfig,
    form: SurveyForm,
    census: Sequence[CensusRow] | None = None,
) -> dict[str, Any]:
    """調査定義の dict。検証前の姿を見たい場面（見積もり表示・保存）で使う。"""
    if not form.concepts:
        raise UIConfigError("コンセプトが1件も無い")
    if form.panel_size < 1:
        raise UIConfigError("総有効サンプル数（N数）は1以上")

    if not form.sex_ranges:
        raise UIConfigError("対象の性別を1つ以上選ぶこと")

    pattern = ui.pattern(form.allocation_pattern)

    built: dict[str, Any] = {
        "survey": {
            "id": form.survey_id or survey_id(form.name),
            "name": form.name,
            # 実行後にその調査が何だったのかを判別できるようにする（§3.0）
            "type": ui.survey_type,
        },
        "panel": {
            "size": form.panel_size,
            "seed": form.seed,
            "quotas": build_quotas(pattern, form.sex_ranges, census),
            "filters": _global_filters(form.sex_ranges),
        },
    }

    screening = build_screening(ui, form.condition)
    if screening is not None:
        built["screening"] = screening

    built["main_survey"] = {
        key: dict(value)
        for key, value in (
            ("model", ui.main_survey.model),
            ("prompt", ui.main_survey.prompt),
            ("persona_card", ui.main_survey.persona_card),
        )
        # 空の block は書かない。調査定義側の既定がそのまま効く。
        if value
    }
    built["stimuli"] = [
        {"id": f"{_STIMULUS_PREFIX}{index}", "name": concept.name, "text": concept.text}
        for index, concept in enumerate(form.concepts, start=1)
    ]
    # 反実仮想モナディック（§5.3）。UI からは変えられない。
    # 全員が全案を独立したセッションで評価するので、案どうしを直接比べられる。
    built["design"] = {
        "sample_overlap": "same",
        "presentation": "sequential",
        "rotation": "none",
    }
    built["questions"] = _expand_questions(
        form.questions or ui.default_questions, len(form.concepts)
    )
    # formats は書かない。結果は画面で見て、ダウンロードは押されたその場で生成するので、
    # ジョブ側でファイルを書き出す必要が無い（調査定義側の既定 delta のみになる）。
    built["output"] = {"segments": list(ui.segments_for(pattern.band))}
    return built


def _expand_questions(
    questions: Sequence[Mapping[str, Any]], concept_count: int
) -> list[dict[str, Any]]:
    """画面の設問リストを、コンセプトの数だけ `slot` 展開する（§3.1）。

    調査定義の設問は「何番目に提示するコンセプトについて聞くか」に紐づくので、
    全案を評価する設計（`sample_overlap: same`）では案の数だけ設問が要る。
    画面はコンセプトごとに設問を作らせないため、ここで機械的に展開する。

    展開後の `id` は一意（`{元のid}_s{slot}`）にし、元の `id` を `measure` に残す。
    残さないとコンセプト比較表が組めない——どの設問とどの設問が同じ問いなのかは、
    展開した側にしか分からない（§8）。

    `remember` は書かない。UI は反実仮想モナディック固定（`memory: none`）で、
    設問どうしが独立していることが案の直接比較を成り立たせているため。
    """
    return [
        {**dict(question), "id": f"{question['id']}_s{slot}", "slot": slot, "measure": question["id"]}
        for slot in range(1, concept_count + 1)
        for question in questions
    ]


def _global_filters(sex_ranges: Mapping[str, tuple[int, int]]) -> dict[str, Any]:
    """割り付けセルとは別に掛ける、全体向けの安全網。

    セル条件（性別・年齢とも）だけに頼ると、`filters` 側の抜けで割り付け対象外の
    ペルソナが混じりうる。性別を1つしか選んでいなければ従来どおり性別・年齢とも絞る。
    複数性別（年齢範囲が性別ごとに違う場合を含む）を選んだときは、性別はセル条件に
    任せ、年齢は選ばれた範囲をすべて覆う envelope（最小の下限〜最大の上限）にする。
    """
    if len(sex_ranges) == 1:
        (sex, (age_min, age_max)), = sex_ranges.items()
        return {"sex": sex, "age_min": age_min, "age_max": age_max}
    return {
        "age_min": min(age_min for age_min, _ in sex_ranges.values()),
        "age_max": max(age_max for _, age_max in sex_ranges.values()),
    }


def build_screening(ui: UIConfig, condition: str) -> dict[str, Any] | None:
    """対象者条件からスクリーニング定義を作る。空欄ならスクリーニングを行わない。

    条件は自然言語のまま `conditions` に入る（§4.2）。選択肢や `pass_if` を
    こちらで捏造しない。実際に提示しないものを調査定義に残すと、何を聞いたのか
    読み取れなくなる。

    並びは調査定義と同じ model → prompt → persona_card → その他。
    """
    text = condition.strip()
    if not text:
        return None

    defaults = ui.screening
    screening: dict[str, Any] = {}
    # `infer` 以外では判定の LLM を呼ばないので、モデル・プロンプト・カードは書かない。
    # 使われない設定が調査定義に残ると「設定したつもり」の記録になる。
    if defaults.mode == "infer":
        for key, value in (
            ("model", defaults.model),
            ("prompt", defaults.prompt),
            ("persona_card", defaults.persona_card),
        ):
            if value:
                screening[key] = dict(value)

    screening["mode"] = defaults.mode
    screening["conditions"] = [text]
    if defaults.mode != "infer":
        # `infer` の判定指示は screening.prompt.rule に一本化されており、logic は効かない。
        # 書くと調査定義の読み込みで停止する（`UNUSED_INFER_SCREENER_KEYS`）。
        screening["logic"] = defaults.logic
    if defaults.mode != "assume":
        screening["oversample_factor"] = defaults.oversample_factor
    if defaults.mode == "infer" and defaults.batch_size is not None:
        screening["batch_size"] = defaults.batch_size
    return screening


def survey_id(name: str, now: datetime | None = None) -> str:
    """調査名から `survey_id` を作る。

    日本語の調査名がそのまま ID になるとテーブルのパーティション値やファイル名で
    扱いにくいので、英数字だけを残し、実行時刻とランダムな識別子を足して一意にする。

    **時刻だけでは足りない。** 日本語の調査名は slug が空になって `survey_{時刻}` に
    潰れるため、同じ秒に2件投入されると ID が衝突する。衝突すると後続の実行が
    `delete_survey_rows()`（`persona_sim.panel.build`）で先行実行の `panels` /
    `responses` を消してしまい、別の調査の結果を自分の調査として読むことになる。
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _ID_SAFE.sub("_", ascii_name.lower()).strip("_")
    suffix = secrets.token_hex(_ID_SUFFIX_BYTES)
    return f"{slug}_{stamp}_{suffix}" if slug else f"survey_{stamp}_{suffix}"


def default_form_questions(ui: UIConfig) -> tuple[Mapping[str, Any], ...]:
    """画面の設問フォームの初期値。編集させるので複製を返す。"""
    return tuple(dict(question) for question in ui.default_questions)
