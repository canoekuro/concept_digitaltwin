"""ローデータを人が読める形にする（`docs/SPEC_UI.md` §4.4）。

`responses` が持っているのは `stimulus_id` と**提示順**の選択肢番号だけで、
どのコンセプトを見た回答なのか、何を選んだのかがそのままでは読めない。
調査定義（`SurveyDefinition`）から名前とラベルを引いて列を足す。

**このモジュールは pyspark に依存しない。** Web UI（SQL Warehouse 経由）から使うため。

決めごとが1つある。

- **ラベルを引く前に `to_defined_codes()` で定義順へ戻す。** `answer_codes` は提示順の
  番号なので、そのまま `options[code - 1]` を引くと、選択肢をシャッフルした設問で
  別の選択肢の名前が付く（§2.3）。番号もラベルも定義順で出す
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from persona_sim.aggregate.crosstab import to_defined_codes
from persona_sim.panel.schema import SurveyDefinition

#: 足す列と、その挿入位置の基準になる元の列。
STIMULUS_NAME_COLUMN = "stimulus_name"
ANSWER_LABELS_COLUMN = "answer_labels"

#: 複数選択のラベルをつなぐ区切り。配列カラムの潰し方（`|`）に合わせる。
LABEL_SEPARATOR = "|"

#: ラベルを引けなかったときの表示。空欄にすると「無回答」と読み違える。
UNKNOWN_LABEL = "(不明)"


def labelled_csv_rows(
    survey: SurveyDefinition, rows: Sequence[Mapping[str, Any]]
) -> tuple[tuple[str, ...], list[list[Any]]]:
    """ローデータの見出しと行を、名前・ラベル付きで返す。

    `stimulus_id` の直後に `stimulus_name`、`answer_codes` の直後に `answer_labels` を
    差し込む。元の列は消さない（`stimulus_id` で突き合わせる用途が残るため）。
    配列は `|` 区切りに潰す。
    """
    if not rows:
        return (), []

    columns = _columns(tuple(rows[0]))
    questions = {question.id: question for question in survey.questions}
    names = {stimulus.id: stimulus.name for stimulus in survey.stimuli}

    body = []
    for row in rows:
        enriched = dict(row)
        enriched[STIMULUS_NAME_COLUMN] = names.get(row.get("stimulus_id"), UNKNOWN_LABEL)
        enriched[ANSWER_LABELS_COLUMN] = answer_labels(
            questions.get(row.get("question_id")),
            row.get("answer_codes"),
            row.get("options_order"),
        )
        body.append([scalar_cell(enriched.get(name)) for name in columns])
    return columns, body


def answer_labels(
    question: Any, answer_codes: Sequence[int] | None, options_order: Sequence[int] | None
) -> str:
    """回答を「1. 買いたいと思う」の形にする。複数選択は `|` でつなぐ。

    番号は**定義順**。`options_order` を使って提示順から戻してから引く。
    選択肢の無い設問（`open` / `numeric`）や、読めなかった番号は空欄・`(不明)` になる。
    """
    if question is None or not getattr(question, "options", None):
        return ""
    options = list(question.options)
    labels = []
    for code in to_defined_codes(answer_codes, options_order):
        if 1 <= code <= len(options):
            labels.append(f"{code}. {options[code - 1]}")
        else:
            # 範囲外（`out_of_range`）。番号は残す。何が返ってきたか追えなくなるため。
            labels.append(f"{code}. {UNKNOWN_LABEL}")
    return LABEL_SEPARATOR.join(labels)


def _columns(original: tuple[str, ...]) -> tuple[str, ...]:
    """足した列を、元になった列の直後に差し込んだ見出し。"""
    inserted = {"stimulus_id": STIMULUS_NAME_COLUMN, "answer_codes": ANSWER_LABELS_COLUMN}
    columns: list[str] = []
    for name in original:
        columns.append(name)
        added = inserted.get(name)
        if added is not None and added not in original:
            columns.append(added)
    # 元データに `stimulus_id` / `answer_codes` が無い場合でも列だけは落とさない。
    for added in inserted.values():
        if added not in columns:
            columns.append(added)
    return tuple(columns)


def scalar_cell(value: Any) -> Any:
    """CSV・xlsx に書ける1セルの値へ。配列は `LABEL_SEPARATOR` 区切りに潰す。

    ローデータ（`labelled_csv_rows`）と `responses_raw.csv`（`frame.stream_responses_raw`）
    の両方が通る。**潰し方を二重に実装しない**——区切りがずれると、同じ回答が
    ダウンロードした2つのファイルで違う文字列になる。
    """
    if isinstance(value, (list, tuple)):
        return LABEL_SEPARATOR.join(str(item) for item in value)
    return value
