"""Persona Lab フェーズ1のパイプライン（`SPEC.md`）。

調査定義 YAML を入力に、パネル構築 → 回答生成 → 集計表出力までを担う。
`SPEC.md` §12 の M1〜M7 を実装済みで、その上に
コンセプト調査 Web UI（`app/`、`docs/SPEC_UI.md`）が載る。
"""

#: パッケージの版。**ここが唯一の出どころ。** `pyproject.toml` は
#: `dynamic = ["version"]` でこの値を読む（`[tool.setuptools.dynamic]`）。
#:
#: 二重に持つと食い違う（実際に 0.1.0 と 0.3.0 でずれていた）。
#: `run_metadata.json` の `persona_sim_version` は
#: この値を書くので、ずれると「どの版のコードで走ったのか」が後から判別できない
#: （`SPEC.md` §9.1 の「各バージョンが揃えば入力が再現できる」の根拠が崩れる）。
#:
#: **`importlib.metadata` から引かないこと。** リポジトリを未インストールのまま使う経路
#: （CI の `pythonpath = ["."]`、ノートブックの sys.path 追加）で `PackageNotFoundError`
#: になり、版が丸ごと失われる。記録が要るのはまさにそういう経路なので、素の定数で持つ。
__version__ = "0.3.0"

# CC BY 4.0 の帰属表示は義務（SPEC.md §15.3）。レポート出力・About 表示から外さないこと。
DATASET_ATTRIBUTION = (
    "本システムは NVIDIA が公開する Nemotron-Personas-Japan (CC BY 4.0) を使用しています。\n"
    "https://huggingface.co/datasets/nvidia/Nemotron-Personas-Japan"
)

# 出力に必ず添える注記（SPEC.md §15.1）。
OUTPUT_DISCLAIMER = (
    "本ツールの出力はAIによるシミュレーションであり、実在する生活者の回答ではありません。"
    "意思決定の根拠として単独で用いず、仮説生成・優先順位付け・調査設計の目的で使用してください。"
)

def attribution_text() -> str:
    """出力に必ず添える帰属表示と免責をまとめて返す（`SPEC.md` §15.1・§15.3）。

    **2つを別々に運ばない。** 別々にすると片方だけ付いた出力ができる——実際、
    CLI の標準出力・`report.xlsx`・`responses_raw.csv` には帰属表示しか付いておらず、
    義務である免責（§15.1）は Web UI のフッタにしか出ていなかった。
    配られるのはむしろファイルのほうなので、1つにまとめて付け忘れを構造的に防ぐ。
    """
    return f"{DATASET_ATTRIBUTION}\n{OUTPUT_DISCLAIMER}"


__all__ = [
    "__version__",
    "DATASET_ATTRIBUTION",
    "OUTPUT_DISCLAIMER",
    "attribution_text",
]
