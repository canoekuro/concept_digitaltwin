# 計画: リポジトリ基盤とエージェント実行環境の立ち上げ

- **日時**: 2026/07/27 00:15
- **対象**: `research_system`（新規・空リポジトリ）
- **依頼**: 添付の仕様書2点をこのリポジトリに配置し、`shelf_gaze_ml` / `retrospective` に
  ならって Claude Code の実行環境（`.claude/`、`.agents/`、スキル等）を立ち上げる。

## 1. 目的

実装（`SPEC.md` §12 M1）に着手できる状態まで、リポジトリの土台を作る。
既存2リポジトリで確立している3層知識設計・PDCA運用・記録規約をそのまま引き継ぎ、
このリポジトリだけ別流儀にならないようにする。

## 2. 現状分析

| 参照元 | 何を持っているか |
|---|---|
| `retrospective` | 共通ルールの SSoT（`agents-rules/` 6件）、共通スキル、`link-repo.sh` / `sync-agent-rules.sh`、SessionStart フック |
| `shelf_gaze_ml` | 上記を取り込んだ repo 側の具体形（`AGENTS.md` + `CLAUDE.md` + `GEMINI.md`、`.agents/{rules,workflows,skills}`、`.claude/{agents,commands,hooks}`、`docs/history` + `CHANGELOG.md`、CI） |
| `research_system` | 空（コミットなし） |

`shelf_gaze_ml` の `.agents/rules/` 6件は `retrospective/agents-rules/` と完全一致することを
`diff` で確認済み。したがって共通ルールは SSoT 側から複製する。

## 3. 提案変更

### 3.1 仕様書の配置
- `SPEC.md` ← 親仕様（添付 `spec.md`）
- `SPEC.md` ← フェーズ1構築仕様（添付）。冒頭の親仕様参照を `SPEC.md` へのリンクに修正

### 3.2 エージェント環境
- `.agents/rules/` — `retrospective/agents-rules/*.md` 6件 ＋ repo 固有の `review-deliverables.md`
- `.agents/workflows/` — `shelf_gaze_ml` から8件（内容は repo 非依存であることを grep で確認）
- `.agents/skills/` — `git-push` / `indexing-awareness` / `knowledge-cutoff-awareness`
- `.claude/agents/` — `implementer-sonnet` / `implementer-opus`
- `.claude/commands/` — `/pdca` / `/codebase-review`
- `.claude/hooks/auto-open-review.sh` ＋ `.claude/settings.json`（PostToolUse 登録）
- 入口ファイル: `AGENTS.md`（SSoT）/ `CLAUDE.md` / `GEMINI.md`

`AGENTS.md` には、このプロジェクト固有の**実装上の不変条件**（thinking OFF、順序尺度を
シャッフルしない、monadic のセル内均等割り当て、`screened_out` 非削除、seed 再現性）と
**禁止事項**（`SPEC.md` §2.2）、**データ・法務の順守事項**（CC BY 4.0 帰属、社内データの集約）を
明記する。仕様書2点の優先順位（矛盾時はフェーズ1仕様が優先）もここで定義する。

### 3.3 開発基盤
- `.gitignore`（Python / Claude / 認証情報 / データ生成物）
- `pyproject.toml`（ruff・pytest 設定のみ。`[project]` は M1 着手時に追加）
- `requirements-ci.txt`、`.github/workflows/ci.yml`（Python 3.12、ruff → `bash -n` → pytest）
- `tests/test_repo_layout.py`（基盤の健全性検証）
- `README.md`、`CHANGELOG.md`、`docs/history/`、`docs/issues/`

## 4. `shelf_gaze_ml` からの意図的な差分

| 項目 | shelf_gaze_ml | 本リポジトリ | 理由 |
|---|---|---|---|
| `.claude/settings.json` | gitignore | **コミット** | PostToolUse フックの登録を全員で共有するため。個人設定は `settings.local.json` 側に分離 |
| `safe-push.sh` の push 先 | `main` 固定 | 現在のブランチ | 共通 git ルール「main へ直接 push しない」との整合 |
| Python | 3.11 | 3.12 | `SPEC.md` §13 の技術スタック指定 |

## 5. 検証計画

- `ruff check .` / `python -m pytest tests/ -q` がローカルで通ること
- 全シェルスクリプトが `bash -n` を通ること
- `.claude/settings.json` が妥当な JSON で、フックが実在スクリプトを指すこと（テストで担保）
- 仕様書内の相互参照リンクが解決すること

## 6. リスク

- 共通ルール6件はコピーであり、`retrospective` 側の更新が自動反映されない
  → `sync-agent-rules.sh` の実行手順を `README.md` に明記して緩和する。
- リモートリポジトリにコミットが1つも無く既定ブランチが未確定
  → 作業ブランチを push した後、既定ブランチの扱いを利用者に確認する。
