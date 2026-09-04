<!-- SSoT: canoekuro/retrospective の agents-rules/run-tests.md。
     各repoの .agents/rules/ 配下はこのファイルの同期コピー。編集は retrospective 側で行い、
     scripts/sync-agent-rules.sh で各repoへ再同期すること。 -->
# Test Execution Rules (テスト実行規則)

- **直接の `pytest` コマンド呼び出しの禁止 / 抑制:** 
  環境（PATH、仮想環境の差異）による `command not found` (exit code 127) や失敗・再試行を回避するため、直に `pytest` コマンドを推測して実行してはいけません。

- **標準テストスクリプトの優先利用:**
  テストを実行する際は、各プロジェクトの `scripts/run-tests.sh` または `.agents/skills/run-tests/SKILL.md` で定められた標準テスト実行手段を優先的に使用してください。
  ```bash
  ./scripts/run-tests.sh
  ```

- **環境指定コマンドへのフォールバック:**
  専用スクリプトが存在しない場合、直に `pytest` を打つのではなく、明示的に `uv run` や `poetry run` などを経由して実行してください。
