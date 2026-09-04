"""`mode: infer` の判定（`persona_sim.panel.infer`）。

Spark を使わない純関数を対象にする。判定に見せる情報の制御と、
推定値を実測値と取り違えないための記録の形を重視する。
"""

from __future__ import annotations

import pytest

from persona_sim.llm.client import ChatMessage, Completion, LLMError, OutputLimitExceeded
from persona_sim.panel.infer import (
    batches,
    build_judge_messages,
    condition_lines,
    infer_session_count,
    judge_batch,
    judge_model,
    parse_judgement,
    persona_card_for_judge,
    run_inference,
)
from persona_sim.panel.schema import (
    Endpoint,
    InferModelOverrides,
    ModelConfig,
    PersonaCardConfig,
    PersonaField,
    ScreenerLogic,
    ScreeningConfig,
    ScreeningPromptConfig,
)
from persona_sim.panel.screening import INFER_QUESTION_ID
from persona_sim.run import flags as flag_names
from persona_sim.run.progress import ProgressUpdate

_PERSONAS = {
    "u1": {
        "uuid": "u1",
        "sex": "女",
        "age": 34,
        "prefecture": "東京都",
        "marital_status": "未婚",
        "education_level": "大卒",
        "occupation_raw": "会社員",
        "persona": "都内で働く会社員。",
        "cultural_background": "アニメ・サブカル好き",
        "culinary_persona": "自炊派で健康志向",
    },
    "u2": {
        "uuid": "u2",
        "sex": "男",
        "age": 41,
        "prefecture": "大阪府",
        "persona": "外食が多い営業職。",
        "cultural_background": "スポーツ観戦",
        "culinary_persona": "外食中心",
    },
}


def _screener(**overrides) -> ScreeningConfig:
    base = dict(
        conditions=("缶チューハイを月1回以上飲む",),
        mode="infer",
        logic=ScreenerLogic.ALL,
    )
    base.update(overrides)
    return ScreeningConfig(**base)


def _model(**overrides) -> ModelConfig:
    base = dict(endpoint=Endpoint.FAKE, deployment="base-endpoint", concurrency=1)
    base.update(overrides)
    return ModelConfig(**base)


# --------------------------------------------------------------------------- #
# バッチ分割
# --------------------------------------------------------------------------- #


def test_batches_split_without_reordering():
    """並べ替えるとハッシュ順が崩れ、再実行で同じ組み合わせにならない。"""
    uuids = [f"u{i}" for i in range(1, 8)]
    assert batches(uuids, 3) == [("u1", "u2", "u3"), ("u4", "u5", "u6"), ("u7",)]


def test_batches_is_stable_across_calls():
    uuids = [f"u{i}" for i in range(1, 8)]
    assert batches(uuids, 3) == batches(uuids, 3)


def test_batches_rejects_zero_size():
    with pytest.raises(ValueError, match="1以上"):
        batches(["u1"], 0)


@pytest.mark.parametrize(
    ("candidates", "batch_size", "expected"), [(0, 20, 0), (1, 20, 1), (40, 20, 2), (41, 20, 3)]
)
def test_infer_session_count(candidates, batch_size, expected):
    assert infer_session_count(candidates, ScreeningConfig(batch_size=batch_size)) == expected


# --------------------------------------------------------------------------- #
# 判定に見せる情報（issue 202607281420 項目7）
# --------------------------------------------------------------------------- #


def test_card_defaults_to_attributes_and_summary():
    card = persona_card_for_judge(_PERSONAS["u1"], ScreeningConfig().persona_card)
    assert "女・34歳・東京都在住・未婚・大卒・会社員" in card
    assert "都内で働く会社員。" in card
    # 既定ではナラティブ列は載せない（プロンプトが長くなると費用面の利点が消える）。
    assert "アニメ・サブカル好き" not in card


def test_card_can_add_persona_fields():
    config = PersonaCardConfig(
        persona_fields=(PersonaField(field="culinary_persona", label="食まわり"),)
    )
    card = persona_card_for_judge(_PERSONAS["u1"], config)
    assert "【食まわり】自炊派で健康志向" in card


def test_card_can_drop_attributes_and_summary():
    """属性行は attributes: [] で落とす（include_attributes は廃止）。"""
    config = PersonaCardConfig(
        attributes=(),
        include_summary=False,
        persona_fields=(PersonaField(field="culinary_persona", label="食まわり"),),
    )
    card = persona_card_for_judge(_PERSONAS["u1"], config)
    assert "34歳" not in card
    assert "都内で働く会社員。" not in card
    assert "自炊派で健康志向" in card


def test_card_skips_missing_columns():
    """欠けている列は行ごと落とす。空の【見出し】を出さない。"""
    config = PersonaCardConfig(
        persona_fields=(PersonaField(field="sports_persona", label="運動"),)
    )
    assert "【運動】" not in persona_card_for_judge(_PERSONAS["u2"], config)


# --------------------------------------------------------------------------- #
# 判定プロンプト
# --------------------------------------------------------------------------- #


def test_judge_messages_number_personas_and_state_conditions():
    messages = build_judge_messages(
        [_PERSONAS["u1"], _PERSONAS["u2"]], _screener()
    )
    assert messages[0].role == "system"
    assert messages[0].content == ScreeningPromptConfig().system

    user = messages[1].content
    assert "1. " in user and "2. " in user
    assert "缶チューハイを月1回以上飲む" in user
    assert ScreeningPromptConfig().rule in user


def test_condition_lines_use_the_survey_definition_wording():
    """調査定義に書かれた条件文をそのまま使う。言い換えを挟まない。"""
    assert condition_lines(_screener()) == ["缶チューハイを月1回以上飲む"]


def test_condition_lines_keep_every_condition_in_order():
    screener = _screener(conditions=("ビールを月1回以上飲む", "自宅で晩酌する"))
    assert condition_lines(screener) == ["ビールを月1回以上飲む", "自宅で晩酌する"]


def test_judge_messages_do_not_inject_a_logic_sentence():
    """`logic` 由来の文をプロンプトに割り込ませない。

    以前は「すべての条件を満たす人物を選んでください」を `prompt.rule` の直前に
    差し込んでいた。設定から触れない文が最も効く位置に入るため、`prompt` を
    除外型に書き換えても判定が変わらなかった（docs/issues/screeningの問題.md）。
    """
    for logic in (ScreenerLogic.ALL, ScreenerLogic.ANY):
        user = build_judge_messages([_PERSONAS["u1"]], _screener(logic=logic))[1].content
        assert "すべての条件" not in user
        assert "いずれかの条件" not in user


def test_judge_messages_end_with_the_configured_rule():
    """判定の指示は `prompt.rule` が最後に出す。後ろに何も足さない。"""
    screener = _screener(prompt=ScreeningPromptConfig(rule="条件に反する人だけ除いてください。"))
    user = build_judge_messages([_PERSONAS["u1"]], screener)[1].content
    assert user.endswith("条件に反する人だけ除いてください。")


# --------------------------------------------------------------------------- #
# 判定結果のパース
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,3", (1, 3)),
        ("1、3", (1, 3)),
        ("1 と 3", (1, 3)),
        ("２", (2,)),
        ("なし", ()),
    ],
)
def test_parse_judgement_reads_numbers(raw, expected):
    codes, _ = parse_judgement(raw, 3)
    assert codes == expected


def test_parse_judgement_drops_out_of_range():
    codes, out_of_range = parse_judgement("1,9", 3)
    assert codes == (1,)
    assert out_of_range


# --------------------------------------------------------------------------- #
# 判定モデルの合成
# --------------------------------------------------------------------------- #


def test_judge_model_inherits_survey_model_by_default():
    model = judge_model(_model(concurrency=4), ScreeningConfig())
    assert model.deployment == "base-endpoint"
    assert model.concurrency == 4


def test_judge_model_applies_only_written_keys():
    screening = ScreeningConfig(
        model=InferModelOverrides(deployment="judge-endpoint", max_tokens=256)
    )
    model = judge_model(_model(concurrency=4, max_tokens=8), screening)

    assert model.deployment == "judge-endpoint"
    assert model.max_tokens == 256
    # 書かなかったキーは本体から引き継ぐ。
    assert model.concurrency == 4
    assert model.endpoint is Endpoint.FAKE


def test_judge_model_forces_thinking_off():
    """思考モードは常に OFF（AGENTS.md 不変条件）。"""
    screening = ScreeningConfig(model=InferModelOverrides(deployment="judge-endpoint"))
    assert judge_model(_model(thinking=True), screening).thinking is False


# --------------------------------------------------------------------------- #
# 判定の実行と記録
# --------------------------------------------------------------------------- #


class _ScriptedClient:
    """指定した本文を返すだけのクライアント。"""

    def __init__(self, text: str = "1", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.messages: list[list[ChatMessage]] = []

    def describe(self) -> str:
        return "scripted"

    def complete(self, messages, **kwargs) -> Completion:
        self.messages.append(list(messages))
        if self._error is not None:
            raise self._error
        return Completion(text=self._text, latency_ms=1, input_tokens=1, output_tokens=1)


def test_judge_batch_marks_selected_personas():
    result = judge_batch(
        ["u1", "u2"],
        _PERSONAS,
        _screener(),
        _ScriptedClient("1"),
        _model(),
    )
    assert result.passed == ("u1",)
    assert not result.failed


def test_judge_batch_keeps_going_on_endpoint_error():
    """1バッチの失敗で調査全体を止めない。"""
    result = judge_batch(
        ["u1", "u2"],
        _PERSONAS,
        _screener(),
        _ScriptedClient(error=LLMError("エンドポイント障害")),
        _model(),
    )
    assert result.failed
    assert result.passed == ()


class _BudgetClient:
    """予算が `succeeds_at` 未満なら出力長超過を返すクライアント。

    `_ScriptedClient` は `**kwargs` を捨てるので `max_tokens` を見られない。
    予算そのものが分岐条件のここでは別に用意する。
    """

    def __init__(self, *, succeeds_at=None, text="1") -> None:
        self._succeeds_at = succeeds_at
        self._text = text
        self.budgets: list[int] = []

    def describe(self) -> str:
        return "budget-stub"

    def complete(self, messages, *, max_tokens, response_format=None) -> Completion:
        self.budgets.append(max_tokens)
        if self._succeeds_at is None or max_tokens < self._succeeds_at:
            raise OutputLimitExceeded("上限に達した", max_tokens=max_tokens)
        return Completion(text=self._text, latency_ms=1, input_tokens=1, output_tokens=1)


def test_judge_batch_raises_the_budget_before_giving_up():
    """バッチ判定は候補人数ぶんの番号を返す。予算不足は枠を広げれば通る。

    ここで諦めると、判定できたはずの候補がまとめて未判定になる。
    """
    client = _BudgetClient(succeeds_at=32)
    result = judge_batch(["u1", "u2"], _PERSONAS, _screener(), client, _model())

    assert not result.failed
    assert result.passed == ("u1",)
    assert len(client.budgets) > 1
    assert result.budget_escalated is True


def test_an_exhausted_judge_batch_is_unjudged_not_rejected():
    """広げても通らなかった候補を非通過にしない。

    非通過として確定すると `read_screener_codes` が判定済みとして読み戻し、
    再実行しても聞き直されない（`docs/issues/20260805001.md` と同じ失敗）。
    """
    client = _BudgetClient(succeeds_at=None)
    result = judge_batch(["u1", "u2"], _PERSONAS, _screener(), client, _model())

    assert result.failed
    assert result.passed == ()
    assert result.budget_exhausted is True
    # どこを直せばよいのかが `screener_responses.answer_raw` から読めること。
    assert "max_tokens" in (result.error or "")


def test_run_inference_counts_budget_escalations():
    """広げて通った回答にはフラグが立たないので、ここで数えないと兆候が残らない。"""
    result = run_inference(
        "s1", ["u1", "u2"], _PERSONAS, _screener(), _BudgetClient(succeeds_at=32), _model()
    )

    assert result.batches_ok == 1
    assert result.budget_escalated == 1
    assert result.budget_exhausted == 0


def test_run_inference_records_are_flagged_as_inferred():
    """実回答と区別できないと、聞いたことになってしまう。"""
    result = run_inference(
        "s1", ["u1", "u2"], _PERSONAS, _screener(), _ScriptedClient("1"), _model()
    )

    assert result.batches_total == 1
    assert result.batches_ok == 1
    assert len(result.records) == 2
    for record in result.records:
        assert flag_names.INFERRED in record.flags


def test_run_inference_assigns_codes_only_to_selected():
    result = run_inference(
        "s1", ["u1", "u2"], _PERSONAS, _screener(), _ScriptedClient("1"), _model()
    )
    # 判定は候補1人につき1回。予約IDの下に通過(1,)／非通過()だけが入る。
    assert result.codes["u1"][INFER_QUESTION_ID] == (1,)
    assert result.codes["u2"][INFER_QUESTION_ID] == ()


def test_run_inference_leaves_failed_batches_unjudged():
    """呼べていない候補を非通過にしない。

    ここで空の番号列を判定結果として載せると、通信の失敗がそのまま「条件に
    合わなかった」として `screener_responses` に確定し、再実行しても聞き直されない。
    """
    result = run_inference(
        "s1",
        ["u1", "u2"],
        _PERSONAS,
        _screener(),
        _ScriptedClient(error=LLMError("エンドポイント障害")),
        _model(),
    )

    assert result.batches_failed == 1
    assert result.codes == {}
    assert result.errors == ["エンドポイント障害"]


def test_run_inference_marks_failed_records_as_judge_error():
    """`parse_error`（返ってきたが番号が取れなかった）と混ぜない。"""
    result = run_inference(
        "s1",
        ["u1", "u2"],
        _PERSONAS,
        _screener(),
        _ScriptedClient(error=LLMError("エンドポイント障害")),
        _model(),
    )

    assert len(result.records) == 2
    for record in result.records:
        assert flag_names.JUDGE_ERROR in record.flags
        assert flag_names.PARSE_ERROR not in record.flags
        # エラー文を残す。これが無いと後から原因を追えない。
        assert record.answer_raw == "エンドポイント障害"


class _FlakyClient:
    """指定した呼び出し回だけ `LLMError` **以外**の例外を投げるクライアント。

    `judge_batch()` が吸収するのは `LLMError` だけなので、それ以外は
    `run_inference()` 側で受け止めないと判定全体を巻き込む。
    """

    def __init__(self, fail_on: int) -> None:
        self._fail_on = fail_on
        self.calls = 0

    def describe(self) -> str:
        return "flaky"

    def complete(self, messages, **kwargs) -> Completion:
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError("想定外の失敗")
        return Completion(text="1", latency_ms=1, input_tokens=1, output_tokens=1)


#: 2バッチに割るための4人。中身は `_PERSONAS` の使い回しでよい（判定内容は見ない）。
_PERSONAS_4 = {
    **_PERSONAS,
    "u3": {**_PERSONAS["u1"], "uuid": "u3"},
    "u4": {**_PERSONAS["u2"], "uuid": "u4"},
}


def test_run_inference_survives_an_unexpected_exception_in_one_batch():
    """`LLMError` 以外の例外で完了済みバッチの判定結果まで失わない。

    `run_inference()` は全バッチを受け取ってから返し、`screen_survey()` が
    そこで初めて `screener_responses` へ書き出す。途中で例外が抜けると、
    成功していたバッチの判定も1件も残らず、再実行で全部呼び直しになる。
    """
    client = _FlakyClient(fail_on=2)
    result = run_inference(
        "s1",
        ["u1", "u2", "u3", "u4"],
        _PERSONAS_4,
        _screener(batch_size=2),
        client,
        _model(concurrency=1),
    )

    # 2バッチとも処理される（例外で打ち切られない）
    assert result.batches_total == 2
    assert result.batches_ok == 1
    assert result.batches_failed == 1
    assert result.errors == ["RuntimeError: 想定外の失敗"]

    # 成功したバッチの判定は残る
    assert set(result.codes) == {"u1", "u2"}
    # 失敗したバッチの候補は未判定。非通過として確定させない
    assert "u3" not in result.codes
    assert "u4" not in result.codes

    # 行は4件とも残り、失敗側だけ judge_error が立つ（再実行で聞き直される）
    assert len(result.records) == 4
    failed = [r for r in result.records if r.persona_uuid in {"u3", "u4"}]
    assert all(flag_names.JUDGE_ERROR in r.flags for r in failed)


def test_run_inference_survives_a_persona_missing_from_the_mapping():
    """`personas` に無い uuid が来ても判定全体を巻き込まない。

    `judge_batch()` は本文を組む前に `personas[uuid]` を引くので、パネルにいて
    `personas_base` から消えたペルソナがいると `KeyError` になる。
    `LLMError` ではないので `judge_batch()` は吸収しない。
    """
    result = run_inference(
        "s1",
        ["u1", "u2", "u3", "u4"],
        _PERSONAS,  # u3 / u4 が無い
        _screener(batch_size=2),
        _ScriptedClient("1"),
        _model(concurrency=1),
    )

    assert result.batches_ok == 1
    assert result.batches_failed == 1
    assert set(result.codes) == {"u1", "u2"}
    assert result.errors and result.errors[0].startswith("KeyError")


def test_run_inference_counts_tokens_per_call_not_per_record():
    """トークンは呼び出し単位。レコード単位で数えると `batch_size` 倍に膨らむ。

    `infer` は1バッチ＝1呼び出しだが、`screener_responses` には候補1人につき1行を
    書き、その行すべてに同じバッチのトークン数を載せている。行を合算して費用を
    出すと、実際には1回しか呼んでいないのに人数ぶん請求されたように見える。
    """
    result = run_inference(
        "s1", ["u1", "u2"], _PERSONAS, _screener(), _ScriptedClient("1"), _model()
    )

    # 1呼び出しで input/output とも 1 を返すクライアント。候補は2人。
    assert len(result.records) == 2
    assert result.input_tokens == 1
    assert result.output_tokens == 1


def test_run_inference_counts_tokens_of_every_batch():
    client = _ScriptedClient("1")
    screener = _screener()
    result = run_inference(
        "s1",
        list(_PERSONAS) * 3,
        _PERSONAS,
        ScreeningConfig(conditions=screener.conditions, mode="infer", batch_size=2),
        client,
        _model(),
    )

    assert result.input_tokens == len(client.messages)
    assert result.output_tokens == len(client.messages)


def test_run_inference_splits_into_batches():
    client = _ScriptedClient("1")
    screener = _screener()
    run_inference(
        "s1",
        list(_PERSONAS) * 3,
        _PERSONAS,
        ScreeningConfig(
            conditions=screener.conditions,
            mode="infer",
            batch_size=2,
        ),
        client,
        _model(),
    )
    assert len(client.messages) == 3


def test_run_inference_reports_progress_in_the_shared_shape():
    """進捗の形は本調査と同じ `ProgressUpdate`。

    ここだけ整数を渡していたため、`persona-sim screen` を infer モード
    （`ui_config.yaml` の既定）で実行すると CLI の進捗表示が AttributeError で落ちていた。
    """
    seen: list[ProgressUpdate] = []
    run_inference(
        "s1",
        ["u1", "u2"],
        _PERSONAS,
        _screener(batch_size=1),  # 1人1バッチ＝2回報せる
        _ScriptedClient("1"),
        _model(),
        progress=seen.append,
    )

    assert [(u.done, u.total, u.failed) for u in seen] == [(1, 2, 0), (2, 2, 0)]
    assert seen[-1].finished


def test_run_inference_progress_counts_failed_batches():
    seen: list[ProgressUpdate] = []
    run_inference(
        "s1",
        ["u1", "u2"],
        _PERSONAS,
        _screener(),
        _ScriptedClient(error=LLMError("エンドポイント障害")),
        _model(),
        progress=seen.append,
    )

    assert [(u.done, u.total, u.failed) for u in seen] == [(1, 1, 1)]
