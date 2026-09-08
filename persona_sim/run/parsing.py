"""出力の強制とパース（`SPEC.md` §6.3、§8）。

このモジュールは **pyspark に依存しない**。純関数のみで構成し、Spark を
起動せずに単体テストできるようにする。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from persona_sim.panel.schema import Question, QuestionType

#: 拒否・回答保留・説教的応答を検出する日本語の経験則パターン（§8 の `refusal`）。
#:
#: これは**経験則**であり、実運用で観測されたモデル出力の言い回しに基づく。
#: パターンを増やすほど拒否応答の取りこぼしは減るが、「わかりません」のような
#: 正当な自由回答・不確実性の表明まで拒否と誤検出するリスクが上がる
#: （特に `open` 設問では素直な回答として現れうる）。増減の際はこのトレードオフを
#: 踏まえ、実データで誤検出率を確認すること。
REFUSAL_PATTERNS: tuple[str, ...] = (
    r"回答でき(ない|ません)",
    # 「お答えできません」と「お答えしかねます」は活用が違う。1つの (…) にまとめると
    # 「お答えしできません」という存在しない形しか拾えなくなる。
    r"お答え(できません|しかねます|いたしかねます)",
    r"わかりません",
    r"答えられません",
    r"AI(として|なので)",
    r"差し控え",
    r"特定の個人",
    r"判断できません",
)

_REFUSAL_RE = tuple(re.compile(pattern) for pattern in REFUSAL_PATTERNS)

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


@dataclass(frozen=True)
class ParsedAnswer:
    """`parse_answer` の戻り値。§2.3 の `responses` に書き込む材料。"""

    #: 選択式の番号（提示順）。パースできなければ None。
    code: int | None
    #: multi 用。single/scale では code と同じ1要素（code が None なら空）。
    codes: tuple[int, ...]
    #: open / numeric の回答本文。
    text: str | None
    #: 番号は取れたが選択肢の範囲外。
    out_of_range: bool
    #: 拒否パターンに一致。
    refusal: bool
    #: `reasoning: true` の設問で、モデルが書いた理由（§6.3）。それ以外は None。
    reasoning: str | None = None


def _is_refusal(raw: str) -> bool:
    return any(pattern.search(raw) for pattern in _REFUSAL_RE)


def _normalize_digits(text: str) -> str:
    return text.translate(_FULLWIDTH_DIGITS)


def _try_json_object(text: str) -> dict | None:
    """text 中から JSON オブジェクトを探して dict にする。取れなければ None。

    全体が JSON ならそれを使い、駄目なら最初の `{` から**1つぶんだけ**読む。
    正規表現で `{...}` を切り出すのはやめた——貪欲 `.*` は
    「`{"answer": 1}` のあとに続く説明文の中の `}`」まで飲み込んで壊し、
    非貪欲 `.*?` は入れ子の途中で切る。`raw_decode()` は JSON の文法どおりに
    対応する `}` で止まるので、どちらの取りこぼしも起きない。
    """
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    else:
        if isinstance(parsed, dict):
            return parsed

    start = stripped.find("{")
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_single_code(raw: str, *, allow_digit_scan: bool = True) -> int | None:
    normalized = _normalize_digits(raw)
    obj = _try_json_object(normalized)
    if obj is not None and "answer" in obj:
        try:
            return int(obj["answer"])
        except (TypeError, ValueError):
            pass
    if not allow_digit_scan:
        return None
    match = re.search(r"\d+", normalized)
    if match:
        return int(match.group(0))
    return None


def _extract_multi_codes(raw: str, *, allow_digit_scan: bool = True) -> tuple[int, ...]:
    normalized = _normalize_digits(raw)
    obj = _try_json_object(normalized)
    if obj is not None and isinstance(obj.get("answers"), list):
        codes: list[int] = []
        for item in obj["answers"]:
            try:
                codes.append(int(item))
            except (TypeError, ValueError):
                continue
        if codes:
            return tuple(sorted(set(codes)))
    if not allow_digit_scan:
        return ()
    matches = re.findall(r"\d+", normalized)
    return tuple(sorted({int(m) for m in matches}))


def _extract_reasoning(raw: str) -> str | None:
    """`{"reasoning": …}` の中身。取れなければ None。

    数字の正規化は掛けない。理由は人が読む本文であって、番号として解釈しないため。
    """
    obj = _try_json_object(raw)
    if obj is None:
        return None
    value = obj.get("reasoning")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _refusal_target(raw: str, reasoning: str | None) -> str:
    """拒否判定に掛ける文字列。**`reasoning` の本文は除く**（§6.3・§8）。

    理由の散文はこちらが書かせたものなので、「わかりません」「判断できません」が
    そのまま現れる（購入意向の選択肢に「わからない」がある以上、言い換えとして
    高頻度で出る）。これを拒否と数えると `refusal` が誤って立ち、`QUALITY_FLAGS` に
    含まれるぶん「フラグ除外後の n」が実態より小さくなる。

    本当の拒否は番号を返せないので `parse_error` で表面化する。理由の外側に書かれた
    前置き・説教は判定対象に残す（JSON の外に本文を垂れ流す応答がありうるため）。
    """
    if not reasoning:
        return raw
    # 生出力では JSON 文字列としてエスケープされている場合がある。素の形と
    # エスケープ後の形の両方を落とす。
    encoded = json.dumps(reasoning, ensure_ascii=False)[1:-1]
    return raw.replace(reasoning, "").replace(encoded, "")


def _extract_numeric_text(raw: str) -> str | None:
    normalized = _normalize_digits(raw)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", normalized)
    if match:
        return match.group(0)
    return None


def parse_answer(raw: str, question: Question, option_count: int) -> ParsedAnswer:
    """モデル出力を設問タイプに応じて解釈する（§6.3）。

    single / scale: JSON 構造化出力（{"answer": 3} 形式）を先に試し、駄目なら
    正規表現で最初に現れる数値を採用する。全角数字は半角に正規化してから処理する。
    1 <= code <= option_count でなければ out_of_range=True（code は取れた値のまま残す）。

    multi: JSON の {"answers": [1,3]} 形式、または "1,3" "1、3" "1 と 3" を拾う。
    重複は除き昇順に整える。1件でも範囲外があれば out_of_range=True。

    open: text に raw を strip して入れる。code は None。

    numeric: 数値（小数・カンマ区切りを含む）を1つ抽出して text に文字列で入れる。
    取れなければ None。

    どのタイプでも、raw が REFUSAL_PATTERNS のいずれかに一致すれば refusal=True にする
    （番号が取れていても立てる。集計側で選べるようにするため）。

    `reasoning: true` の設問（§6.3）では2点だけ変わる。

    - 番号は **JSON の `answer` からしか取らない**。理由の散文には数字が混ざるので、
      正規表現で最初の数字を拾うと理由の中の数字を回答にしてしまう
    - 拒否判定から `reasoning` の本文を外す（`_refusal_target()`）
    """
    reasoning = _extract_reasoning(raw) if question.reasoning else None
    refusal = _is_refusal(_refusal_target(raw, reasoning))
    # 理由を書かせる設問では、数字の走査で理由の中の数字を拾ってしまう。
    # `structured_output: always` を必須にしてあるので JSON は必ず返る（`validate`）。
    digit_scan = not question.reasoning

    if question.type in (QuestionType.SINGLE, QuestionType.SCALE):
        code = _extract_single_code(raw, allow_digit_scan=digit_scan)
        codes = (code,) if code is not None else ()
        out_of_range = code is not None and not (1 <= code <= option_count)
        return ParsedAnswer(
            code=code,
            codes=codes,
            text=None,
            out_of_range=out_of_range,
            refusal=refusal,
            reasoning=reasoning,
        )

    if question.type is QuestionType.MULTI:
        codes = _extract_multi_codes(raw, allow_digit_scan=digit_scan)
        out_of_range = any(not (1 <= c <= option_count) for c in codes)
        return ParsedAnswer(
            code=None,
            codes=codes,
            text=None,
            out_of_range=out_of_range,
            refusal=refusal,
            reasoning=reasoning,
        )

    if question.type is QuestionType.OPEN:
        return ParsedAnswer(
            code=None, codes=(), text=raw.strip(), out_of_range=False, refusal=refusal
        )

    if question.type is QuestionType.NUMERIC:
        text = _extract_numeric_text(raw)
        return ParsedAnswer(code=None, codes=(), text=text, out_of_range=False, refusal=refusal)

    raise ValueError(f"未知の設問タイプ: {question.type}")


def answer_schema(question: Question, option_count: int) -> dict | None:
    """構造化出力用の JSON Schema（response_format にそのまま渡せる形）。

    single / scale: {"answer": integer, minimum 1, maximum option_count}
    multi:          {"answers": array of integer(1..option_count)}
    open / numeric: None（構造化しない）

    `question.reasoning` が真なら `reasoning`（string）を**先頭のプロパティ**として足す
    （§6.3）。順序が意味を持つ——生成は前から進むので、先に置くことで「理由を書いてから
    番号を選ぶ」順になる。後ろに置くと番号を決めた後の後付けの説明になり、
    回答そのものは変わらない。

    `reasoning` に `maxLength` は入れない。strict スキーマでの対応がエンドポイント依存で、
    拒否されると `structured_output: always`（reasoning 使用時の必須設定）の下では調査が
    止まる。字数は `prompt.rules.reasoning.{設問タイプ}` の指示文と
    `model.max_tokens_reasoning` で抑える。
    """
    if question.type in (QuestionType.SINGLE, QuestionType.SCALE):
        properties = {"answer": {"type": "integer", "minimum": 1, "maximum": option_count}}
        name = "single_answer"
    elif question.type is QuestionType.MULTI:
        properties = {
            "answers": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": option_count},
            }
        }
        name = "multi_answer"
    else:
        return None

    if question.reasoning:
        properties = {"reasoning": {"type": "string"}, **properties}
        name = f"{name}_with_reasoning"

    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }

    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": schema,
            "strict": True,
        },
    }
