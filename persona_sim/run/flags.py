"""品質フラグ（`SPEC.md` §8）。

**フラグは立てるだけで、レコードを除外しない。** 集計側で含める／除くを選べるようにするため。
`AGENTS.md` の不変条件。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from persona_sim.panel.schema import QuestionType

#: 選択肢番号を抽出できなかった（リトライ後も）。
PARSE_ERROR = "parse_error"
#: 抽出できたが選択肢の範囲外だった。
OUT_OF_RANGE = "out_of_range"
#: 1回以上リトライした。
RETRIED = "retried"
#: 拒否・回答保留・説教的応答と判定された。
REFUSAL = "refusal"
#: 当該ペルソナが閉じた設問すべてで同一位置の選択肢を選んだ。
STRAIGHTLINE = "straightline"
#: 出力が予算（`max_tokens`）に達し、**広げても完了しなかった**。回答は欠落か切り詰め。
#: 広げて通った場合には立てない（`retried` と同じで、それは経緯であって回答の質ではない）。
#: 引き上げが起きた回数そのものは実行メタデータの `model.output_limit` で数える。
OUTPUT_LIMIT = "output_limit"
#: 本人に聞かず、別の LLM が条件合致の蓋然性から判断した（`screener.mode: infer`、§4.2）。
#: `screener_responses` にのみ立つ。実回答と取り違えないための目印。
INFERRED = "inferred"
#: 判定の呼び出しそのものが失敗し、**このペルソナは判定できていない**（`screener.mode: infer`）。
#: `screener_responses` にのみ立つ。`parse_error`（返ってきたが番号が取れなかった）とは別物で、
#: こちらは非通過ではなく未判定。行は証跡として残し（`answer_raw` にエラー文が入る）、
#: `read_screener_codes` が判定済みから除くので再実行で聞き直される。
JUDGE_ERROR = "judge_error"

#: `responses` に立ちうるフラグ。集計と実行メタデータの集計対象。
#: `inferred` はスクリーニング専用で `responses` には現れないため含めない
#: （含めると実行メタデータに常に 0 の項目が増える）。
ALL_FLAGS = (PARSE_ERROR, OUT_OF_RANGE, RETRIED, REFUSAL, STRAIGHTLINE, OUTPUT_LIMIT)

#: 回答内容の質を疑わせるフラグ。集計表の「フラグ除外後の n」はこれで数える（§7.1・§8）。
#: `retried` は含めない。リトライは経緯であって、最終的に得られた回答の質ではない。
#: `output_limit` は含める。こちらは予算を広げても完了しなかった場合にだけ立つので、
#: 経緯ではなく回答そのものの欠落・切り詰めを表す。
QUALITY_FLAGS = (PARSE_ERROR, OUT_OF_RANGE, REFUSAL, STRAIGHTLINE, OUTPUT_LIMIT)

#: straightline を判定する最小の設問数。2問の一致は偶然が多すぎて意味を持たない。
STRAIGHTLINE_MIN_ANSWERS = 3


def straightline_question_ids(questions: Iterable[Any]) -> list[str]:
    """straightline の判定対象になる設問の id（§8）。

    対象は **single / scale だけ**。`answer_codes` が1要素に定まるものに限る。

    `multi` は外す。複数選択は「同一位置を選んだ」に潰せないため
    （`aggregate.crosstab._mean_score()` が複数回答に平均を出さないのと同じ理由）。
    判定は `answer_codes` の先頭要素で行うので、`multi` を混ぜると
    `[1, 2, 3]` と `[1, 5]` が「どちらも 1」として同一視され、
    違う回答をした人に straightline が立つ。

    `open` / `numeric` は選択肢が無いのでそもそも対象外。
    """
    return [
        question.id
        for question in questions
        if question.type in (QuestionType.SINGLE, QuestionType.SCALE)
    ]
