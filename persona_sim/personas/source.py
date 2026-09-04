"""Nemotron-Personas-Japan の取得（`SPEC_PHASE1.md` §2.1, §9）。

リビジョンを固定してシャード（parquet）をダウンロードする。`huggingface_hub` は
モジュールとしては未インストールの環境もあるため、関数内で import して
モジュール import 自体は壊れないようにする。
"""

from __future__ import annotations

#: 取得元データセット。
DATASET_ID = "nvidia/Nemotron-Personas-Japan"

#: 検証済みリビジョン（`docs/schema/occupation-parsing.md` の検証対象）。
DEFAULT_REVISION = "f1f37019d8497143c507b3deb547e65646de2ab7"

#: 総シャード数。
TOTAL_SHARDS = 8


def shard_filename(index: int) -> str:
    """シャード番号（0始まり）からリポジトリ内のファイル名を作る。"""
    return f"data/train-{index:05d}-of-{TOTAL_SHARDS:05d}.parquet"


def source_version(revision: str = DEFAULT_REVISION) -> str:
    """`run_metadata.json` の `data_versions.source_dataset` 用の文字列（§9）。"""
    return f"{DATASET_ID}@{revision}"


def download_shards(
    shards: int | None = None,
    revision: str = DEFAULT_REVISION,
    cache_dir: str | None = None,
) -> list[str]:
    """先頭 `shards` 個（`None` なら全 `TOTAL_SHARDS` 個）をダウンロードする。

    リビジョンを固定して取得することで、`source_version` と実データが対応することを
    保証する。返り値はローカルにキャッシュされた parquet ファイルのパス一覧。
    """
    count = TOTAL_SHARDS if shards is None else shards
    if not 1 <= count <= TOTAL_SHARDS:
        raise ValueError(f"shards は1〜{TOTAL_SHARDS}の範囲で指定する: {shards!r}")

    from huggingface_hub import hf_hub_download

    return [
        hf_hub_download(
            repo_id=DATASET_ID,
            repo_type="dataset",
            filename=shard_filename(index),
            revision=revision,
            cache_dir=cache_dir,
        )
        for index in range(count)
    ]
