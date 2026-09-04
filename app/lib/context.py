"""アプリの実行環境（`docs/SPEC_UI.md` §6.3）。

環境変数の解決と、重い資源（設定・接続・クライアント）のキャッシュだけを持つ。
**ロジックは置かない。** 集計も調査定義の組み立ても `persona_sim` 側にある。

Databricks Apps では次が注入される。

| 変数 | 出どころ |
|---|---|
| `DATABRICKS_HOST` / `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` | ランタイムが自動で入れる |
| `DATABRICKS_WAREHOUSE_ID` | `app.yaml` の `valueFrom: sql-warehouse` |
| `PERSONA_SIM_JOB_ID` | `app.yaml` の `valueFrom: persona-sim-job` |
| `PERSONA_SIM_SURVEY_VOLUME` | `app.yaml` の `valueFrom: survey-volume` |
| `PERSONA_SIM_CATALOG` / `PERSONA_SIM_SCHEMA` | `app.yaml` の `value:` |
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from persona_sim.config import StorageConfig, storage_config
from persona_sim.uiconfig import UIConfig, load_census, load_ui_config
from persona_sim.uiconfig.census import CensusRow

ENV_WAREHOUSE_ID = "DATABRICKS_WAREHOUSE_ID"
ENV_JOB_ID = "PERSONA_SIM_JOB_ID"
ENV_SURVEY_VOLUME = "PERSONA_SIM_SURVEY_VOLUME"
ENV_UI_CONFIG = "PERSONA_SIM_UI_CONFIG"


@dataclass(frozen=True)
class Missing:
    """設定が足りないときの説明。画面に出して、落とさずに案内する。"""

    variable: str
    purpose: str


#: 画面設定の既定の場所。**cwd 相対にしない。** Databricks Apps はリポジトリ直下を
#: 作業ディレクトリにして `app/app.py` を起動するので、cwd 相対だと解決できない。
DEFAULT_UI_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ui_config.yaml"


@st.cache_resource
def ui_config() -> UIConfig:
    override = os.environ.get(ENV_UI_CONFIG)
    return load_ui_config(Path(override) if override else DEFAULT_UI_CONFIG_PATH)


@st.cache_resource
def census() -> tuple[CensusRow, ...] | None:
    """国勢調査の参照テーブル。無ければ `None`（当該割り付けを選べなくする）。"""
    return load_census()


def storage() -> StorageConfig | None:
    """テーブルの置き場所。カタログ未設定なら `None`（画面に案内を出す）。"""
    try:
        config = storage_config()
    except Exception:
        return None
    return config if config.uses_catalog else None


def warehouse_id() -> str | None:
    return os.environ.get(ENV_WAREHOUSE_ID) or None


def job_id() -> str | None:
    return os.environ.get(ENV_JOB_ID) or None


def survey_volume() -> str | None:
    return os.environ.get(ENV_SURVEY_VOLUME) or None


def missing_for_submit() -> list[Missing]:
    """調査を投入するのに足りない設定。"""
    checks = [
        (job_id(), Missing(ENV_JOB_ID, "調査を実行する Databricks ジョブの ID")),
        (survey_volume(), Missing(ENV_SURVEY_VOLUME, "調査定義を置く Volumes のパス")),
    ]
    return [missing for value, missing in checks if not value]


def missing_for_results() -> list[Missing]:
    """結果を読むのに足りない設定。"""
    missing = []
    if not warehouse_id():
        missing.append(Missing(ENV_WAREHOUSE_ID, "結果を読む SQL Warehouse の ID"))
    if storage() is None:
        missing.append(
            Missing("PERSONA_SIM_CATALOG / PERSONA_SIM_SCHEMA", "テーブルのカタログとスキーマ")
        )
    return missing


@st.cache_resource
def connection():
    """SQL Warehouse への接続。セッションをまたいで使い回す。

    `@st.cache_resource` はプロセス全体で1つを配るので、**利用者ごとには分かれない**。
    同時に使うと壊れるため、呼び出しは必ず `query()` を通すこと。
    """
    from persona_sim.storage.warehouse import connect

    return connect(warehouse_id())


#: SQL Warehouse への問い合わせを直列化する錠。
#:
#: `connection()` は `@st.cache_resource` なので**全利用者で1本の接続**を共有する。
#: databricks-sql-connector の Connection は複数スレッドから同時に使えず、
#: Streamlit は接続中のブラウザセッションごとに別スレッドでスクリプトを再実行するので、
#: 直列化しないと2人が同時に取得したときにリクエストが混ざる（壊れ方は運次第で、
#: 例外になるか、**片方の結果がもう片方に返る**）。
_QUERY_LOCK = threading.Lock()


def query(fetcher, *args, **kwargs):
    """`persona_sim.storage.warehouse` の取得関数を直列化して呼ぶ。

    第1引数に接続を渡す約束の関数（`fetch_runs` / `fetch_survey` / `build_result` /
    `fetch_responses_raw`）をそのまま受ける。取得は「データを取得」押下時にまとまって
    走るだけなので、直列化しても待ち時間は変わらない。

    ロックはここに置く。`persona_sim.storage.warehouse` は CLI・ノートブックからも
    使われ、あちらは単一スレッドでロックを持ち込む理由が無い（アプリ側の都合を
    パイプラインに漏らさない）。

    SQL Warehouse が自動停止から復帰した場合、キャッシュ済みの接続が stale になり
    「Error during request to server」が返る。初回失敗時にキャッシュを破棄し
    新しい接続で1回だけリトライする。
    """
    with _QUERY_LOCK:
        try:
            return fetcher(connection(), *args, **kwargs)
        except Exception:
            connection.clear()
            return fetcher(connection(), *args, **kwargs)


@st.cache_resource
def workspace_client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


#: 調査一覧のキャッシュ保持時間（秒）。実行は数分かかるので、この程度なら
#: 「完了したのに一覧に出てこない」体感にはならない。
RUNS_TTL = 60


@st.cache_data(ttl=RUNS_TTL, show_spinner="調査一覧を取得しています…")
def runs(_storage: StorageConfig, survey_type: str) -> list[dict]:
    """調査一覧。**ウェアハウスを叩くのはキャッシュが切れたときだけ。**

    Streamlit はウィジェットを触るたびにページ全体を再実行するので、素で呼ぶと
    調査を選び直すだけで毎回 SQL Warehouse へ問い合わせが飛ぶ。一覧が出るまで
    待たされる原因だった（`docs/issues/20260805004.md`）。

    設定はハッシュできないので `_` 始まりの名前にしてキーから外す
    （Streamlit の規約）。キーになるのは `survey_type` だけで、これで足りる。

    接続は受け取らない。`query()` が `connection()` から取り、共有接続を
    同時に使わないよう錠の下で呼ぶ。
    """
    from persona_sim.storage.warehouse import fetch_runs

    return query(fetch_runs, _storage, survey_type)


@st.cache_data(show_spinner="ダウンロード用のファイルを作成しています…")
def workbook_bytes(
    survey_id: str,
    _survey,
    _table,
    *,
    _notes,
    _raw_columns,
    _raw_rows,
) -> bytes:
    """ダウンロード用の xlsx。**組み立てるのはキャッシュが切れたときだけ。**

    `st.download_button` は押される前からデータの実体を要求するので、素で書くと
    表を切り替えるたび・チェックを1つ触るたびに、回答数千行の Excel を作り直す
    （`runs()` と同じ、Streamlit の再実行にそのまま乗ってしまう形）。

    キーにするのは `survey_id` だけ。中身は取得結果に対して一意に決まり、その取得結果は
    `survey_id` で選ばれているため。ハッシュできない引数は `_` 始まりの名前でキーから外す。

    返すのは**バイト列**でパスではない。`tempfile.TemporaryDirectory()` は関数を抜けた
    時点で消えるので、パスをキャッシュすると2回目以降に読めなくなる。
    """
    from persona_sim.aggregate.export import write_workbook

    with tempfile.TemporaryDirectory() as directory:
        path = write_workbook(
            Path(directory) / f"{survey_id}.xlsx",
            _survey,
            [_table],
            notes=_notes,
            raw_columns=_raw_columns,
            raw_rows=_raw_rows,
        )
        return path.read_bytes()


def show_missing(missing: list[Missing]) -> None:
    """足りない設定を画面に出す。落とさずに、何を設定すればよいかを見せる。"""
    st.warning("この画面を使うには、アプリの設定が足りていません。")
    for item in missing:
        st.markdown(f"- `{item.variable}` … {item.purpose}")
    st.caption("設定方法は `deploy/README.md` を参照してください。")
