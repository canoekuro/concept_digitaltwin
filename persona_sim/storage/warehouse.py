"""SQL Warehouse 経由の読み取り（`docs/SPEC_UI.md` §4）。

Web UI（Databricks Apps）は Spark ドライバを持たない軽量コンテナで動くため、
`persona_sim.storage.delta` の経路は使えない。同じテーブルを SQL Warehouse から読む。

**このモジュールは pyspark に依存しない。** `databricks-sql-connector` の import も
接続関数の中でだけ行うので、クエリの組み立てだけならコネクタ無しでテストできる。

設計上の決めごとが3つある。

- **配列カラムは SQL 側で `to_json` して文字列で受け取る。** コネクタが `ARRAY<...>` を
  何で返すか（numpy 配列か JSON 文字列か）は `_use_arrow_native_complex_types` という
  先頭アンダースコアの私的な引数に依存する。公式 API ではないものに挙動を預けたくないので、
  ワイヤ形式を `STRING` に固定する
- **値は必ず名前付きパラメータで渡す。** 文字列連結で SQL を組み立てない
- **行を `Answer` に変換するのは `aggregate.frame.answers_from_rows`。** ここでは変換しない。
  Spark 経由と同じ純関数を通すことで、読み方が違っても同じ数字になることを保証する
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from persona_sim.aggregate import segments as segment_axes
from persona_sim.config import StorageConfig
from persona_sim.errors import PersonaSimError
from persona_sim.panel.sampling import ROLE_MAIN
from persona_sim.panel.schema import SurveyDefinition, SurveyType
from persona_sim.storage.locator import PANELS, PERSONAS_BASE, RESPONSES, RUNS, locator

#: `to_json` で文字列にして受け取る配列カラム。
JSON_COLUMNS = ("answer_codes", "options_order", "flags")

#: 一覧に出す `runs` の列（`metadata_json` は重いので含めない）。
#:
#: **画面が使う列だけを引く。** 実行セッション数・成功・失敗・完了日時のメトリクスは
#: 読み手に意味が伝わらないので結果閲覧ページから外した（`docs/issues/20260805004.md`）。
#: 残しているのは、調査選択プルダウンの表示名に要る3列（`docs/SPEC_UI.md` §4.1）と、
#: 中断を知らせる `aborted_reason`。
RUN_COLUMNS = (
    "survey_id",
    "survey_name",
    "finished_at",
    "aborted_reason",
)


def table(name: str, storage: StorageConfig) -> str:
    """`catalog.schema.table` 形式のテーブル名。

    SQL Warehouse からはカタログ経由でしか引けないので、パス指定
    （`PERSONA_SIM_WAREHOUSE`）のモードでは使えない。
    """
    loc = locator(name, storage)
    if not loc.uses_catalog:
        raise PersonaSimError(
            "SQL Warehouse から読むには Unity Catalog の設定が要る。"
            "PERSONA_SIM_CATALOG と PERSONA_SIM_SCHEMA を設定すること"
            "（PERSONA_SIM_WAREHOUSE のパス指定は CLI・ローカル専用）"
        )
    return loc.identifier


# --------------------------------------------------------------------------- #
# クエリの組み立て（純関数）
# --------------------------------------------------------------------------- #


def runs_query(
    storage: StorageConfig, survey_type: SurveyType | str = SurveyType.CONCEPT
) -> tuple[str, dict[str, Any]]:
    """完走した調査の一覧。新しい順。`survey_id` ごとに最新の1件だけ返す。

    種別で絞るのは、ほかの調査種別が同じテーブルに入っても画面に混ざらないようにするため。

    **同じ `survey_id` の再実行を SQL 側で1件に畳む。** 畳まないと、画面が
    `survey_id` をキーに辞書へ入れ直すところで後勝ちの上書きが起き、
    `ORDER BY finished_at DESC` の並びゆえに**最古の行**が残る。一方
    `survey_definition_query()` は最新1件を読むので、一覧に出る完了時刻と
    実際に取得するデータが食い違う。
    """
    columns = ", ".join(RUN_COLUMNS)
    query = (
        f"SELECT {columns} FROM {table(RUNS, storage)} "
        "WHERE survey_type = :survey_type "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY survey_id ORDER BY finished_at DESC) = 1 "
        "ORDER BY finished_at DESC"
    )
    return query, {"survey_type": str(survey_type)}


def survey_definition_query(storage: StorageConfig) -> tuple[str, dict[str, Any]]:
    """1調査ぶんの調査定義。`metadata_json` は重いので選んだ調査だけ引く。

    同じ `survey_id` で複数回実行されていることがあるので、最新の1件に絞る。
    """
    query = (
        f"SELECT metadata_json FROM {table(RUNS, storage)} "
        "WHERE survey_id = :survey_id "
        "ORDER BY finished_at DESC LIMIT 1"
    )
    return query, {}


def answers_query(
    storage: StorageConfig, survey: SurveyDefinition
) -> tuple[str, dict[str, Any]]:
    """集計対象の回答（`responses ⋈ panels ⋈ personas_base`）。

    `aggregate.frame._joined` と同じ結合を SQL で書いたもの。本調査に進んだ
    `role=main` のペルソナだけを対象にする（`reserve` / `screened_out` /
    `candidate` は本調査を実行していない）。
    """
    persona_columns = _persona_columns(survey)
    selected = [
        "r.persona_uuid",
        "r.stimulus_id",
        "r.question_id",
        "r.answer_text",
        *(f"to_json(r.{name}) AS {name}" for name in JSON_COLUMNS),
        "p.cell_id",
        "p.weight",
        *(f"b.{name}" for name in persona_columns),
    ]
    query = (
        f"SELECT {', '.join(selected)}\n"
        f"FROM {table(RESPONSES, storage)} AS r\n"
        f"JOIN {table(PANELS, storage)} AS p\n"
        "  ON p.survey_id = r.survey_id AND p.persona_uuid = r.persona_uuid\n"
        f" AND p.role = :role\n"
        f"LEFT JOIN {table(PERSONAS_BASE, storage)} AS b ON b.uuid = r.persona_uuid\n"
        "WHERE r.survey_id = :survey_id"
    )
    return query, {"role": ROLE_MAIN}


def panel_rows_query(storage: StorageConfig) -> tuple[str, dict[str, Any]]:
    """パネル構成表の材料。全ロールを取る（非通過・予備も構成表に出す）。"""
    query = (
        f"SELECT cell_id, role, weight FROM {table(PANELS, storage)} "
        "WHERE survey_id = :survey_id"
    )
    return query, {}


def _persona_columns(survey: SurveyDefinition) -> tuple[str, ...]:
    """`personas_base` から取る属性列。

    `cell_id` は `panels` 側にあるので外す。両方から取ると SQL があいまいになる。
    """
    wanted = segment_axes.required_columns(survey.output.segments)
    return tuple(name for name in wanted if name not in segment_axes.PANEL_AXES)


# --------------------------------------------------------------------------- #
# 行の後処理（純関数）
# --------------------------------------------------------------------------- #


def parse_answer_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """`to_json` で文字列にした配列カラムを list に戻す。

    `answers_from_rows` は配列として読むので、ここで形を揃えてから渡す。
    """
    parsed = dict(row)
    for name in JSON_COLUMNS:
        parsed[name] = _json_list(parsed.get(name))
    return parsed


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(loaded) if isinstance(loaded, list) else []
    # コネクタが配列のまま返した場合（設定次第でありうる）もそのまま扱える。
    return list(value)


# --------------------------------------------------------------------------- #
# 接続（薄い層。ここだけが databricks-sql-connector に触る）
# --------------------------------------------------------------------------- #


def http_path(warehouse_id: str) -> str:
    """SQL Warehouse の HTTP パス。

    Databricks Apps のリソース注入で渡ってくるのは**ウェアハウス ID** なので、
    パスはこちらで組み立てる。
    """
    return f"/sql/1.0/warehouses/{warehouse_id}"


def connect(warehouse_id: str, config: Any = None):
    """SQL Warehouse への接続を開く。

    認証はアプリのサービスプリンシパル。Databricks Apps では `DATABRICKS_HOST` /
    `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` が注入されるので、
    `Config()` が何も渡さずに解決する。
    """
    from databricks import sql
    from databricks.sdk.core import Config

    cfg = config or Config()
    host = str(cfg.host).removeprefix("https://").removeprefix("http://")
    return sql.connect(
        server_hostname=host,
        http_path=http_path(warehouse_id),
        credentials_provider=lambda: cfg.authenticate,
    )


def fetch(connection: Any, query: str, parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """クエリを実行して行を dict の並びで返す。"""
    with connection.cursor() as cursor:
        cursor.execute(query, parameters=dict(parameters))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def fetch_answers(
    connection: Any, storage: StorageConfig, survey: SurveyDefinition
) -> list[Any]:
    """回答を取得して `Answer` の並びにする。

    変換は `aggregate.frame.answers_from_rows` に任せる。Spark 経由と同じ純関数を
    通すので、読み方が違っても同じ数字になる。
    """
    from persona_sim.aggregate.frame import answers_from_rows

    query, parameters = answers_query(storage, survey)
    rows = fetch(connection, query, {**parameters, "survey_id": survey.survey_id})
    return answers_from_rows((parse_answer_row(row) for row in rows), survey)


def fetch_panel_rows(
    connection: Any, storage: StorageConfig, survey_id: str
) -> list[dict[str, Any]]:
    query, parameters = panel_rows_query(storage)
    return fetch(connection, query, {**parameters, "survey_id": survey_id})


def fetch_runs(
    connection: Any,
    storage: StorageConfig,
    survey_type: SurveyType | str = SurveyType.CONCEPT,
) -> list[dict[str, Any]]:
    query, parameters = runs_query(storage, survey_type)
    return fetch(connection, query, parameters)


def fetch_survey(connection: Any, storage: StorageConfig, survey_id: str) -> SurveyDefinition:
    """`runs.metadata_json` から調査定義を復元する。

    調査定義が永続化されているのはここだけ。YAML ファイルを読みに行く必要はない。

    **復元するのは集計が読む範囲だけ**（`survey_from_record()`）。記録には実行当時の
    書き方がそのまま残っているので、その後で書き方を変えた block まで読もうとすると、
    過去の調査の結果が**聞き方の文言を理由に**読めなくなる（`prompt.rules.reasoning`
    を設問タイプごとの入れ子にしたときに実際に起きた）。返る定義は集計専用で、
    実行には使えない（`SurveyDefinition.from_record`）。
    """
    from persona_sim.panel.loader import survey_from_record

    query, parameters = survey_definition_query(storage)
    rows = fetch(connection, query, {**parameters, "survey_id": survey_id})
    if not rows:
        raise PersonaSimError(f"survey_id={survey_id} の実行記録が無い")
    metadata = json.loads(rows[0]["metadata_json"])
    return survey_from_record(metadata["survey_definition"])


def build_result(
    connection: Any, storage: StorageConfig, survey: SurveyDefinition
) -> Any:
    """1調査ぶんの集計結果を SQL Warehouse から作る。

    取得は1回きり。集計表もグラフもローデータのダウンロードも、この結果から作る。
    """
    from persona_sim.aggregate.aggregate import build_result as build

    answers = fetch_answers(connection, storage, survey)
    panel_rows = fetch_panel_rows(connection, storage, survey.survey_id)
    return build(survey, answers, panel_rows)


#: ローデータに載せる `responses` の列（配列は `to_json` するので個別に扱う）。
#:
#: **`ts`（回答時刻）は載せない。** `responses.ts` は `timestamp` 列で、コネクタが
#: タイムゾーン付きの `datetime` で返す。openpyxl は tz 付き datetime をセルに書けず、
#: ダウンロードが `TypeError: Excel does not support timezones in datetimes` で丸ごと
#: 失敗していた（`docs/issues/20260805004.md`）。tz を落として書くこともできるが、
#: どのタイムゾーンで出すかを決めないまま値だけ出すことになるので載せない。
#: 診断に要る `latency_ms` / `attempt` は残してある。CLI の `responses_raw.csv`
#: （`aggregate.frame.stream_responses_raw`）は別経路で、そちらは時刻を落としていない。
RAW_RESPONSE_COLUMNS = (
    "survey_id",
    "persona_uuid",
    "stimulus_id",
    "question_id",
    "sequence",
    "answer_raw",
    "answer_codes",
    "answer_text",
    "answer_reasoning",
    "options_order",
    "flags",
    "latency_ms",
    "attempt",
)


def responses_query(storage: StorageConfig) -> tuple[str, dict[str, Any]]:
    """ローデータのダウンロード用。属性を付けた全件（§7.3 `responses_raw.csv` 相当）。

    列を明示するのは、配列カラムを `to_json` で文字列にして受け取るため（モジュール冒頭の
    決めごと）。`r.*` だとコネクタが `ARRAY<...>` を何で返すかに挙動を預けることになり、
    選択肢ラベルを引くために番号を読もうとしたときに形が定まらない。
    """
    selected = [
        f"to_json(r.{name}) AS {name}" if name in JSON_COLUMNS else f"r.{name}"
        for name in RAW_RESPONSE_COLUMNS
    ]
    query = (
        f"SELECT {', '.join(selected)},"
        " p.cell_id, p.weight, b.sex, b.age, b.age_band_10, b.prefecture\n"
        f"FROM {table(RESPONSES, storage)} AS r\n"
        f"JOIN {table(PANELS, storage)} AS p\n"
        "  ON p.survey_id = r.survey_id AND p.persona_uuid = r.persona_uuid\n"
        " AND p.role = :role\n"
        f"LEFT JOIN {table(PERSONAS_BASE, storage)} AS b ON b.uuid = r.persona_uuid\n"
        "WHERE r.survey_id = :survey_id\n"
        "ORDER BY r.persona_uuid, r.stimulus_id, r.question_id"
    )
    return query, {"role": ROLE_MAIN}


def fetch_responses_raw(
    connection: Any, storage: StorageConfig, survey: SurveyDefinition
) -> tuple[tuple[str, ...], list[list[Any]]]:
    """ダウンロード用のローデータ（見出しと行）。

    コンセプト名と選択肢ラベルを足すのは `aggregate.rawdata`。`responses` は
    `stimulus_id` と**提示順**の選択肢番号しか持っておらず、そのままでは
    どのコンセプトに何を答えたのか読めない。
    """
    from persona_sim.aggregate.rawdata import labelled_csv_rows

    query, parameters = responses_query(storage)
    rows = fetch(connection, query, {**parameters, "survey_id": survey.survey_id})
    return labelled_csv_rows(survey, [parse_answer_row(row) for row in rows])
