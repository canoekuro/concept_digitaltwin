#!/usr/bin/env bash
set -euo pipefail

# Persona Sim テスト実行スクリプト（非Sparkテスト既定）
# 依存パッケージが導入された uv 環境で pytest を呼び出します。

uv run --with pytest --with pydantic --with pyyaml --with requests pytest -m "not spark" "$@"
