"""コマンドラインインターフェース（`SPEC_PHASE1.md` §10.1）。

```
persona-sim validate  survey.yaml     # スキーマ検証・抽出可能性の事前チェック
persona-sim panel     survey.yaml     # パネル構築のみ（割り付けが埋まるか確認）
persona-sim screen    survey.yaml     # スクリーニングとパネル確定
persona-sim run       survey.yaml     # 回答生成（再開可能）
persona-sim aggregate survey.yaml     # 集計（§7）
persona-sim export    survey.yaml     # 集計結果のファイル出力（§7.4）
persona-sim personas build         # personas_base の構築（M1）
```
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from persona_sim import __version__, attribution_text
from persona_sim.config import StorageConfig, storage_config
from persona_sim.errors import PersonaSimError
from persona_sim.panel.loader import load_survey
from persona_sim.panel.schema import SurveyDefinition
from persona_sim.panel.validate import ValidationReport, validate_static
from persona_sim.textwidth import display_width


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return args.handler(args)
    except PersonaSimError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="persona-sim", description="Persona Lab フェーズ1")
    parser.add_argument("--version", action="version", version=f"persona-sim {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="調査定義を検証する")
    _add_survey_arguments(validate_parser)
    validate_parser.add_argument(
        "--skip-feasibility",
        action="store_true",
        help="personas_base を読まず、Spark 不要の検証だけ行う",
    )
    validate_parser.set_defaults(handler=_handle_validate)

    panel_parser = subparsers.add_parser("panel", help="パネルを構築する")
    _add_survey_arguments(panel_parser)
    panel_parser.add_argument(
        "--dry-run", action="store_true", help="構成を表示するだけで panels に書き込まない"
    )
    panel_parser.set_defaults(handler=_handle_panel)

    screen_parser = subparsers.add_parser(
        "screen", help="スクリーニングを実行してパネルを確定する"
    )
    _add_survey_arguments(screen_parser)
    screen_parser.add_argument(
        "--force",
        action="store_true",
        help="判定済みの結果を無視して全候補を判定し直す"
        "（プロンプト・モデルを変えた場合は指定しなくても自動で判定し直す）",
    )
    screen_parser.set_defaults(handler=_handle_screen)

    run_parser = subparsers.add_parser("run", help="回答を生成する（再開可能）")
    _add_survey_arguments(run_parser)
    run_parser.add_argument("--output-dir", default=None, help="run_metadata.json の出力先")
    run_parser.set_defaults(handler=_handle_run)

    aggregate_parser = subparsers.add_parser("aggregate", help="集計する（§7）")
    _add_survey_arguments(aggregate_parser)
    aggregate_parser.add_argument("--output-dir", default=None, help="出力先ディレクトリ")
    aggregate_parser.set_defaults(handler=_handle_aggregate)

    export_parser = subparsers.add_parser(
        "export", help="集計結果からファイルを書き出す（§7.4）"
    )
    _add_survey_arguments(export_parser)
    export_parser.add_argument("--output-dir", default=None, help="出力先ディレクトリ")
    export_parser.add_argument(
        "--xlsx", action="store_true", help="report.xlsx だけを書き出す"
    )
    export_parser.set_defaults(handler=_handle_export)

    personas_parser = subparsers.add_parser("personas", help="personas_base の管理")
    personas_sub = personas_parser.add_subparsers(dest="personas_command", required=True)
    build_parser = personas_sub.add_parser("build", help="personas_base を構築する")
    build_parser.add_argument("--shards", type=int, default=1, help="取り込むシャード数（既定 1）")
    build_parser.add_argument("--revision", default=None, help="Hugging Face のリビジョン")
    build_parser.add_argument("--warehouse", default=None, help="テーブルを置くベースパス")
    build_parser.add_argument(
        "--local-path",
        action="append",
        default=None,
        help="ダウンロードせずに読む parquet のパス（複数指定可）",
    )
    build_parser.set_defaults(handler=_handle_personas_build)

    return parser


def _add_survey_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("survey", help="調査定義 YAML のパス")
    parser.add_argument("--warehouse", default=None, help="テーブルを置くベースパス")


# --------------------------------------------------------------------------- #
# 各コマンド
# --------------------------------------------------------------------------- #


def _handle_validate(args: argparse.Namespace) -> int:
    survey = load_survey(args.survey)
    report = validate_static(survey)

    if report.ok and not args.skip_feasibility:
        report.merge(_feasibility(survey, storage_config(args.warehouse)))

    _print_report(survey, report)
    return 0 if report.ok else 1


def _handle_panel(args: argparse.Namespace) -> int:
    from persona_sim.panel.build import build_panel, composition_lines
    from persona_sim.spark import get_spark

    survey = load_survey(args.survey)
    report = validate_static(survey)
    if not report.ok:
        _print_report(survey, report)
        return 1

    storage = storage_config(args.warehouse)
    spark = get_spark()
    result = build_panel(spark, survey, storage, write=not args.dry_run)

    print(f"調査: {survey.survey_id}（{survey.name}）")
    print()
    for line in composition_lines(survey, result.selection):
        print(line)
    print()
    for line in report.estimate.lines() if report.estimate else []:
        print(line)
    print()
    for warning in report.warnings:
        print(f"警告 {warning}", file=sys.stderr)
    if result.personas_version is not None:
        print(f"personas_base の Delta バージョン: {result.personas_version}")
    if args.dry_run:
        print("--dry-run のため panels には書き込んでいない")
    print()
    print(attribution_text())
    return 0


def _handle_screen(args: argparse.Namespace) -> int:
    from persona_sim.panel.screening import screen_survey
    from persona_sim.run.progress import console_reporter
    from persona_sim.spark import get_spark

    survey = load_survey(args.survey)
    report = validate_static(survey)
    if not report.ok:
        _print_report(survey, report)
        return 1

    screener = survey.screening
    if screener is None:
        print("スクリーナーが定義されていないので実行不要。そのまま `run` に進める")
        return 0
    if not screener.calls_llm:
        print(
            f"screener.mode が {screener.mode} なので実行不要。"
            "聞かずに前提として与えるため、判定するものが無い。そのまま `run` に進める"
        )
        return 0

    storage = storage_config(args.warehouse)
    spark = get_spark()
    result = screen_survey(
        spark, survey, storage, progress=console_reporter(), force=args.force
    )

    print()
    print(f"調査: {survey.survey_id}（{survey.name}）")
    print(f"方式  : {result.method}（オーバーサンプル {result.oversample_actual} 倍 / 試行 {result.attempts} 回）")
    print(f"判定設定: {result.config_hash}")
    if result.refreshed:
        print(
            "  --force が指定された"
            if args.force
            else "  判定設定が前回と違うので、確定済みのパネルを作り直して判定し直した"
        )
    unit = "バッチ" if screener.infers else "セッション"
    print(f"{unit}: {result.sessions_ok} 完了 / {result.sessions_total} 中")
    if result.sessions_skipped:
        print(f"  うち判定済みでスキップ: {result.sessions_skipped}")
    print()
    print(f"{'セル':<16}{'確定':>8}{'予備':>8}{'非通過':>10}{'通過率':>10}")
    print("-" * 52)
    for cell in survey.panel.quotas.cells:
        cell_id = cell.cell_id
        rate = result.incidence.get(cell_id)
        print(
            f"{cell_id:<16}{result.achieved.get(cell_id, 0):>8}"
            f"{result.reserve.get(cell_id, 0):>8}{result.screened_out.get(cell_id, 0):>10}"
            f"{(f'{rate:.1%}' if rate is not None else '-'):>10}"
        )
    print("-" * 52)
    total = result.incidence.get("total")
    if total is None:
        print("全体の通過率: -")
    elif screener.infers:
        # 聞いていないので実測値ではない。同じ言葉で呼ぶと取り違えられる。
        print(f"全体の通過率（推定・本人には聞いていない）: {total:.1%}")
    else:
        print(f"全体の通過率（インシデンス）: {total:.1%}")

    for warning in result.warnings:
        print(f"警告 {warning}", file=sys.stderr)

    if result.aborted_reason:
        print(f"警告 中断した: {result.aborted_reason}", file=sys.stderr)
        return 1
    return 0


def _handle_run(args: argparse.Namespace) -> int:
    from persona_sim.config import output_dir
    from persona_sim.metadata import write_metadata
    from persona_sim.run.executor import raise_if_unrecoverable
    from persona_sim.run.progress import console_reporter
    from persona_sim.run.run import run_survey
    from persona_sim.spark import get_spark

    survey = load_survey(args.survey)
    report = validate_static(survey)
    if not report.ok:
        _print_report(survey, report)
        return 1

    storage = storage_config(args.warehouse)
    spark = get_spark()
    result = run_survey(spark, survey, storage, progress=console_reporter())

    # 中断していてもメタデータは残す。どこまで進んだかが分からないと再開の判断ができない。
    metadata_path = write_metadata(spark, survey, result, storage, output_dir(args.output_dir))

    print()
    print(f"調査: {survey.survey_id}（{survey.name}）")
    print(f"セッション: {result.sessions_ok} 完了 / {result.sessions_total} 中")
    if result.sessions_skipped:
        print(f"  うち再開でスキップ: {result.sessions_skipped}")
    if result.sessions_failed:
        print(f"  失敗: {result.sessions_failed}")
    print(f"書き出したレコード: {result.records_written:,}")
    print(f"トークン: 入力 {result.input_tokens:,} / 出力 {result.output_tokens:,}")
    if result.flag_counts:
        summary = " / ".join(f"{flag}={count}" for flag, count in result.flag_counts.items())
        print(f"品質フラグ: {summary}")
    print(f"実行メタデータ: {metadata_path}")

    for warning in result.warnings:
        print(f"警告 {warning}", file=sys.stderr)

    print()
    print(attribution_text())

    raise_if_unrecoverable(result)
    return 0


def _handle_aggregate(args: argparse.Namespace) -> int:
    from persona_sim.aggregate.aggregate import aggregate_survey
    from persona_sim.config import output_dir
    from persona_sim.spark import get_spark

    survey = _survey_or_none(args)
    if survey is None:
        return 1

    result = aggregate_survey(
        get_spark(), survey, storage_config(args.warehouse), output_dir(args.output_dir)
    )
    _print_aggregate(survey, result)
    return 0


def _handle_export(args: argparse.Namespace) -> int:
    from persona_sim.aggregate.aggregate import result_from_aggregates
    from persona_sim.aggregate.export import FORMAT_CSV, FORMAT_XLSX, write_outputs
    from persona_sim.config import output_dir
    from persona_sim.spark import get_spark

    survey = _survey_or_none(args)
    if survey is None:
        return 1

    spark = get_spark()
    storage = storage_config(args.warehouse)
    # 集計はやり直さず `aggregates`（§2.5）から組み直す。
    result = result_from_aggregates(spark, survey, storage)
    formats = (FORMAT_XLSX,) if args.xlsx else (FORMAT_CSV, FORMAT_XLSX)
    result.written = write_outputs(
        spark, survey, storage, result, output_dir(args.output_dir), formats=formats
    )

    print(f"調査: {survey.survey_id}（{survey.name}）")
    for path in result.written:
        print(f"  {path}")
    print()
    print(attribution_text())
    return 0


def _survey_or_none(args: argparse.Namespace) -> SurveyDefinition | None:
    """調査を読み、静的検証に落ちたら報告して None を返す。"""
    survey = load_survey(args.survey)
    report = validate_static(survey)
    if not report.ok:
        _print_report(survey, report)
        return None
    return survey


def _pad(value: object, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - display_width(text))


def _print_table(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    widths = [
        max(display_width(str(column)), *(display_width(str(row[index])) for row in rows))
        if rows
        else display_width(str(column))
        for index, column in enumerate(columns)
    ]
    print("  ".join(_pad(column, width) for column, width in zip(columns, widths, strict=True)))
    for row in rows:
        print("  ".join(_pad(value, width) for value, width in zip(row, widths, strict=True)))


def _print_aggregate(survey: SurveyDefinition, result) -> None:
    from persona_sim.aggregate.tables import concept_summary_table

    print(f"調査: {survey.survey_id}（{survey.name}）")
    print(f"集計対象の回答: {result.answers:,} 件")
    print()

    summary = concept_summary_table(
        survey, result.topline, key="concept_summary", title="", with_segment=False
    )
    _print_table(summary.columns, summary.rows)

    print()
    for path in result.written:
        print(f"  {path}")
    for note in result.notes:
        print(f"注記 {note}", file=sys.stderr)
    print()
    print(attribution_text())


def _handle_personas_build(args: argparse.Namespace) -> int:
    from persona_sim.personas.build import build_personas_base
    from persona_sim.personas.source import DEFAULT_REVISION
    from persona_sim.spark import get_spark

    storage = storage_config(args.warehouse)
    spark = get_spark()
    result = build_personas_base(
        spark,
        storage,
        shards=args.shards,
        revision=args.revision or DEFAULT_REVISION,
        local_paths=args.local_path,
    )
    print(f"書き出し先      : {result.location}")
    print(f"行数            : {result.rows:,}")
    print(f"Delta バージョン: {result.version}")
    print(f"source_version  : {result.source_version}")
    print()
    print(attribution_text())
    return 0


def _feasibility(survey: SurveyDefinition, storage: StorageConfig) -> ValidationReport:
    from persona_sim.panel.validate import validate_feasibility
    from persona_sim.spark import get_spark
    from persona_sim.storage import delta
    from persona_sim.storage.locator import PERSONAS_BASE, locator

    spark = get_spark()
    personas_locator = locator(PERSONAS_BASE, storage)
    if not delta.table_exists(spark, personas_locator):
        report = ValidationReport()
        report.warn(
            "W_NO_PERSONAS",
            f"{personas_locator.describe()} が無いので抽出可能性を確認できない。"
            "先に `persona-sim personas build` を実行すること",
        )
        return report
    return validate_feasibility(spark, delta.read_table(spark, personas_locator), survey)


def _print_report(survey: SurveyDefinition, report: ValidationReport) -> None:
    for issue in report.errors:
        print(f"エラー {issue}", file=sys.stderr)
    for issue in report.warnings:
        print(f"警告 {issue}", file=sys.stderr)

    if report.estimate:
        print(f"調査: {survey.survey_id}（{survey.name}）")
        for line in report.estimate.lines():
            print(line)

    if report.ok:
        print("検証を通過した")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
