"""調査の投入（`docs/SPEC_UI.md` §6）。

画面1 が組み立てた調査定義を Unity Catalog Volumes に YAML として置き、
**事前定義した Databricks ジョブ**を `run_now` で起動する。

アプリ内のバックグラウンドスレッドで回さない。Databricks Apps のコンテナは再起動しうるし、
`run_survey()` は完走まで `responses` を書かないので、途中で落ちるとその実行分が丸ごと消える。

ジョブを毎回 `submit` せず事前定義のものを起動するのは、クラスタ構成・権限・再実行を
運用側で管理できるようにするため。アプリはジョブ ID とパラメータだけを持つ。

**このモジュールは pyspark に依存しない。** SDK の import も関数の中でだけ行うので、
パス組み立てと YAML 生成はコネクタ無しでテストできる。
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

import yaml

from persona_sim.uiconfig.schema import UIConfigError

#: ジョブに渡すパラメータ名。事前定義ジョブ側の `{{job.parameters.survey}}` と対応する。
SURVEY_PARAMETER = "survey"


class _SurveyDumper(yaml.SafeDumper):
    """複数行の文字列をリテラルブロック（`|`）で書き出す Dumper。

    **PyYAML は自分からはブロックスタイルを選ばない。** 既定では plain →
    single-quoted → double-quoted の順にしか試さないので、プロンプトのような
    改行入りの文字列は必ずクォートされる。さらに `  - ` のように改行の直後が
    空白で始まる行があると plain も single-quoted も使えなくなり、`\\n` エスケープと
    行継続の `\\` が混じった1本の長い double-quoted になる。

    読み直せば元の文字列に戻るので実害は見た目だけだが、この YAML は
    ジョブが読むだけでなく人が「何を聞いたのか」を確認する対象なので、
    読める形で出す（`ui_config.yaml` に `|` で書いた見た目を保つ）。
    """


def _literal_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """改行を含む文字列だけ `|` にする。

    `|` を指定しても使えない文字列（行末に空白がある、`\\r` を含む）は
    PyYAML が double-quoted に戻すので、強制しても壊れない。
    """
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_SurveyDumper.add_representer(str, _literal_str)

#: 折り返さない幅。既定（80桁）だと1行に収まる文字列——コンセプト文や設問文——が
#: 途中で折り返され、複数行を `|` にした意味が薄れる。
_NO_WRAP_WIDTH = 4096


def survey_yaml(survey_dict: Mapping[str, Any]) -> bytes:
    """調査定義を YAML のバイト列にする。

    日本語をエスケープしない（`allow_unicode`）。ジョブが読むだけでなく、
    あとから人が「何を聞いたのか」を確認する対象になるため。
    キーの並びは組み立てた順を保つ（`sort_keys=False`）。
    複数行の文字列は `|` で出す（`_SurveyDumper`）。
    """
    text = yaml.dump(
        dict(survey_dict),
        Dumper=_SurveyDumper,
        allow_unicode=True,
        sort_keys=False,
        width=_NO_WRAP_WIDTH,
    )
    return text.encode("utf-8")


def survey_id_of(survey_dict: Mapping[str, Any]) -> str:
    """調査定義から `survey_id` を取り出す。

    置き場所のパスも冪等トークンも、**投入する調査定義そのもの**から引く。
    呼び出し側が別に持っている ID を渡せるようにしておくと、YAML のファイル名と
    中身の `survey.id` がずれたまま投入できてしまい、ジョブは中身の ID で
    結果を書くのに画面はファイル名の ID を表示する、という食い違いが起きる。
    """
    survey = survey_dict.get("survey")
    value = survey.get("id") if isinstance(survey, Mapping) else None
    if not isinstance(value, str) or not value:
        raise UIConfigError("調査定義に survey.id が無い（投入先も冪等トークンも決められない）")
    return value


def volume_path(volume: str, survey_id: str) -> str:
    """調査定義を置く Volumes 上のパス。

    `volume` は `/Volumes/{catalog}/{schema}/{volume}` の形（Databricks Apps の
    リソース注入でこの形が渡ってくる）。
    """
    if not volume.startswith("/Volumes/"):
        raise UIConfigError(
            f"Volumes のパスは /Volumes/ で始まる必要がある（実際: {volume!r}）"
        )
    return f"{volume.rstrip('/')}/{survey_id}.yaml"


def idempotency_token(survey_id: str) -> str:
    """二重投入を防ぐトークン。

    Streamlit はウィジェット操作のたびにスクリプト全体を再実行するので、これが無いと
    ボタンの二度押しや再実行で同じ調査が複数回走る。

    **`survey_id` が投入ごとに一意であることに依存している。** Databricks は
    一致するトークンに対して新しい run を作らず既存 run の ID を返す（保持は約64日）。
    つまり同じ `survey_id` で投げ直すと、**成功と区別がつかないまま空振りする**
    （画面には run ID が出るが、新しい実行は始まっていない）。

    現状これが成立しているのは、画面が `uiconfig.build.survey_id()` で
    `{slug}_{時刻}_{乱数}` を毎回作り、投入が成功したら捨てているため
    （`app/views/survey_design.py`）。**ID を固定して投入する経路を足すときは、
    ここも併せて見直すこと**（`SurveyForm.survey_id` は固定できる）。
    """
    return f"persona-sim-{survey_id}"


def upload_survey(client: Any, volume: str, survey_dict: Mapping[str, Any]) -> str:
    """調査定義を Volumes に置き、そのパスを返す。

    Apps のコンテナからは `/Volumes` を直接 `open()` できないので Files API を使う
    （ジョブ側は計算資源上で動くので、渡したパスを普通に開ける）。

    ファイル名は中身の `survey.id` から作る。別経路の ID を受け取らない。
    """
    path = volume_path(volume, survey_id_of(survey_dict))
    client.files.upload(path, io.BytesIO(survey_yaml(survey_dict)), overwrite=True)
    return path


def run(client: Any, job_id: int | str, survey_path: str, survey_id: str) -> int:
    """事前定義ジョブを起動し、run ID を返す。

    完了は待たない。待つと Streamlit のスクリプト実行がそのままブロックされる。
    進捗は追わず、完走した調査が画面2 の一覧に出てくるのを待つ（`docs/SPEC_UI.md` §4.2）。
    """
    wait = client.jobs.run_now(
        job_id=int(job_id),
        job_parameters={SURVEY_PARAMETER: survey_path},
        idempotency_token=idempotency_token(survey_id),
    )
    return wait.response.run_id


def submit_survey(
    client: Any,
    job_id: int | str,
    volume: str,
    survey_dict: Mapping[str, Any],
) -> tuple[str, int]:
    """調査定義を置いてジョブを起動する。`(置いたパス, run ID)` を返す。

    置き場所も冪等トークンも `survey_dict` の `survey.id` から引くので、
    ジョブが結果を書き込む `survey_id` と必ず一致する。
    """
    survey_id = survey_id_of(survey_dict)
    path = upload_survey(client, volume, survey_dict)
    return path, run(client, job_id, path, survey_id)
