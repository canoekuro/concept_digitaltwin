"""出力ファイル一式の書き出し（`SPEC_PHASE1.md` §7.4）。

```
outputs/{survey_id}/
  ├ crosstab_{stimulus_id}_{measure}.csv
  ├ concept_summary.csv
  ├ concept_summary_by_segment.csv
  ├ open_ends.csv
  ├ panel_composition.csv
  ├ responses_raw.csv
  ├ run_metadata.json      … `persona_sim.metadata` が run 時に書く
  └ report.xlsx
```

CSV は BOM 付き UTF-8 で書く。Excel が既定のエンコーディングで開くと日本語が壊れるため。
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from persona_sim import attribution_text
from persona_sim.aggregate import frame
from persona_sim.aggregate.tables import Table
from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.panel.schema import SurveyDefinition

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import SparkSession

    from persona_sim.aggregate.aggregate import AggregateResult

#: Excel で開いたときに日本語が化けないようにする。
CSV_ENCODING = "utf-8-sig"

#: xlsx のシート名に使えない文字（Excel の制約）。
_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_MAX_SHEET_NAME = 31

FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"
FORMAT_DELTA = "delta"


def write_outputs(
    spark: SparkSession,
    survey: SurveyDefinition,
    storage: StorageConfig,
    result: AggregateResult,
    output_dir: str,
    *,
    formats: Sequence[str] | None = None,
) -> list[Path]:
    """§7.4 の一式を書き出す。書いたファイルのパスを返す。"""
    selected = tuple(formats if formats is not None else survey.output.formats)
    destination = Path(output_dir) / survey.survey_id
    destination.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    if FORMAT_CSV in selected:
        for table in result.tables:
            written.append(write_csv(destination / f"{table.key}.csv", table.columns, table.rows))
        written.append(_write_open_ends(spark, survey, storage, destination))
        written.append(_write_responses_raw(spark, survey, storage, destination))

    if FORMAT_XLSX in selected:
        written.append(write_xlsx(destination / "report.xlsx", survey, result))

    return written


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    with open(path, "w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


def _write_open_ends(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig, destination: Path
) -> Path:
    columns, rows = frame.load_open_ends(spark, survey, storage)
    return write_csv(destination / "open_ends.csv", columns, rows)


def _write_responses_raw(
    spark: SparkSession, survey: SurveyDefinition, storage: StorageConfig, destination: Path
) -> Path:
    columns, rows = frame.stream_responses_raw(spark, survey, storage)
    return write_csv(destination / "responses_raw.csv", columns, rows)


# --------------------------------------------------------------------------- #
# xlsx
# --------------------------------------------------------------------------- #


def write_xlsx(path: Path, survey: SurveyDefinition, result: AggregateResult) -> Path:
    """全表を1ファイルにまとめる（§7.4 `report.xlsx`）。

    先頭に「概要」シートを置き、調査情報と注記（§11 の E3・E4 を含む）をそこに集める。
    表だけ配ると注記が読まれずに終わるため。
    """
    workbook = _new_workbook()
    summary = workbook.active
    summary.title = "概要"
    for row in _summary_rows(survey, result):
        summary.append(row)

    used: set[str] = {summary.title}
    for table in result.tables:
        sheet = workbook.create_sheet(_sheet_name(table, used))
        sheet.append([table.title])
        sheet.append([])
        sheet.append(list(table.columns))
        for row in table.rows:
            sheet.append(list(row))
        if table.notes:
            sheet.append([])
            for note in table.notes:
                sheet.append([f"注記: {note}"])

    workbook.save(path)
    return path


def _new_workbook():
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - 依存が入っていない環境向け
        raise PersonaSimError(
            "report.xlsx の書き出しには openpyxl が必要。"
            "`pip install openpyxl` するか、survey の output.formats から xlsx を外すこと"
        ) from exc
    return Workbook()


def _summary_rows(survey: SurveyDefinition, result: AggregateResult) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["調査ID", survey.survey_id],
        ["調査名", survey.name],
        ["集計対象の回答数", result.answers],
        ["コンセプト数", len(survey.stimuli)],
        ["設問数", len(survey.questions)],
        ["セグメント軸", ", ".join(survey.output.segments)],
        [],
        ["注記"],
    ]
    notes = result.notes or ["なし"]
    rows.extend([[note] for note in notes])
    rows.extend(
        [
            [],
            ["読み方"],
            ["n はウェイト適用前の実数、% はウェイト適用後（ウェイトが全て1.0なら一致する）"],
            ["品質フラグの立った回答も集計に含めている。除いた場合の n を併記してある"],
            ["平均は選択肢番号を逆順スコア化した値（順序尺度の設問のみ）"],
            ["コンセプト間の相対比較として読むこと。統計的推測の指標は算出していない"],
        ]
    )
    # 帰属表示（§15.3）と免責（§15.1）。**表だけ配られる前提で必ず載せる。**
    rows.append([])
    rows.extend([line] for line in attribution_text().splitlines())
    return rows


# --------------------------------------------------------------------------- #
# Web UI 用の xlsx（`docs/SPEC_UI.md` §4.4）
#
# CLI の `write_xlsx()` とは別にしてある。UI のダウンロードは「集計表とローデータ」を
# **1ボタン**で渡す必要があり（`st.download_button` は1ファイルしか返せない）、
# シート構成が §7.4 の `report.xlsx` と違う。共用にすると UI の都合で CLI の出力が
# 変わってしまう。
# --------------------------------------------------------------------------- #

#: UI 用ブックのシート名。
UI_SUMMARY_SHEET = "集計表"
UI_RAW_SHEET = "ローデータ"

#: Excel の1シートあたりの行数上限。
EXCEL_MAX_ROWS = 1_048_576


def write_ui_workbook(
    path: Path,
    survey: SurveyDefinition,
    table: Table,
    *,
    notes: Sequence[str] = (),
    raw_columns: Sequence[str] = (),
    raw_rows: Sequence[Sequence[Any]] = (),
) -> Path:
    """集計表1枚とローデータ1枚の xlsx を書く。

    集計表シートの**先頭に注記を置く**。§11 の E3・E4（パース失敗・拒否が閾値を超えた、
    割り付け外の回答者が混ざっている）は、集計表だけ配ると読まれずに終わる。
    シートを増やさずに表面化させるため、表の上に積む。
    """
    workbook = _new_workbook()
    sheet = workbook.active
    sheet.title = UI_SUMMARY_SHEET

    sheet.append([table.title])
    sheet.append([f"調査ID: {survey.survey_id}", f"調査名: {survey.name}"])
    sheet.append([])
    for note in notes:
        sheet.append([f"注記: {note}"])
    for note in table.notes:
        sheet.append([note])
    # 帰属表示（§15.3）と免責（§15.1）。ダウンロードされて独り歩きする前提で必ず載せる。
    for line in attribution_text().splitlines():
        sheet.append([line])
    sheet.append([])
    sheet.append(list(table.columns))
    for row in table.rows:
        sheet.append(list(row))

    raw = workbook.create_sheet(UI_RAW_SHEET)
    if raw_columns:
        raw.append(list(raw_columns))
        truncated = _truncate(raw_rows)
        for row in truncated:
            raw.append(list(row))
        if len(truncated) < len(raw_rows):
            # 黙って切らない。切られたことに気づかないまま「全件」として配られる方が害が大きい。
            raw.append(
                [
                    f"※ Excel の行数上限のため {len(truncated):,} 件で打ち切った"
                    f"（全 {len(raw_rows):,} 件）。全件が要る場合は CLI の"
                    " responses_raw.csv を使うこと"
                ]
            )

    workbook.save(path)
    return path


def _truncate(rows: Sequence[Sequence[Any]]) -> Sequence[Sequence[Any]]:
    """見出し1行と打ち切り注記1行のぶんを残して収める。"""
    limit = EXCEL_MAX_ROWS - 2
    return rows if len(rows) <= limit else rows[:limit]


def _sheet_name(table: Table, used: set[str]) -> str:
    """Excel のシート名制約（31文字・記号不可・重複不可）に収める。"""
    base = _INVALID_SHEET_CHARS.sub("_", table.key)[:_MAX_SHEET_NAME] or "sheet"
    name = base
    suffix = 2
    while name in used:
        tail = f"_{suffix}"
        name = base[: _MAX_SHEET_NAME - len(tail)] + tail
        suffix += 1
    used.add(name)
    return name
