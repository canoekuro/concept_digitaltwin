---
name: run-tests
description: Run automated test suite for persona_sim repository reliably without command errors or environment issues.
---

# Run Tests Skill

このスキルは、Persona Lab（`persona_sim`）のテストスイートを確実かつエラーなく実行するための標準手順を提供します。

## 実行手順

テストを実行する際は、環境依存（`pytest` コマンドが PATH に通っていない、依存ライブラリの不足など）による失敗を避けるため、直に `pytest` を呼び出さず、必ずプロジェクト専用のヘルパースクリプト `scripts/run-tests.sh` を利用してください。

### 1. 非Sparkテストの実行（基本）
```bash
./scripts/run-tests.sh
```

### 2. 個別テストファイルの指定・引数パススルー
```bash
./scripts/run-tests.sh tests/test_validate.py -k "test_image_mode"
```

### 3. スクリプトが使えない場合（`uv` 直接呼び出し）
```bash
uv run --with pytest --with pydantic --with pyyaml --with requests pytest -m "not spark" "$@"
```
