# 結果: リポジトリ基盤とエージェント実行環境の立ち上げ

- **日時**: 2026/07/27 00:15
- **計画**: [20260727-001500-repo-bootstrap-plan.md](20260727-001500-repo-bootstrap-plan.md)

## 1. 変更内容

計画どおり実施。空リポジトリに以下を追加した。

### 仕様書
- `SPEC.md` — 親仕様（添付をそのまま配置）
- `SPEC.md` — フェーズ1構築仕様。冒頭の親仕様参照のみ `SPEC.md` へのリンクに修正

### エージェント環境
- `.agents/rules/` 7件 — 共通6件は `retrospective/agents-rules/` から複製（SSoT 一致を `diff` で確認）、
  `review-deliverables.md` は `shelf_gaze_ml` から複製（repo 固有・同期対象外）
- `.agents/workflows/` 8件 — `ask` / `commit` / `commit-ja` / `discard` / `explain` / `grasp` / `plan` / `review`
- `.agents/skills/` 3件 — `git-push` / `indexing-awareness` / `knowledge-cutoff-awareness`
- `.claude/agents/` — `implementer-sonnet` / `implementer-opus`
- `.claude/commands/` — `pdca` / `codebase-review`
- `.claude/hooks/auto-open-review.sh` ＋ `.claude/settings.json`（PostToolUse(Write) 登録）
- `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`

`AGENTS.md` に、仕様書2点の優先順位（矛盾時はフェーズ1仕様が優先）、実装上の不変条件、
Out of Scope、データ・法務の順守事項を明記した。

### 開発基盤
- `.gitignore` / `pyproject.toml`（ruff・pytest 設定のみ）/ `requirements-ci.txt`
- `.github/workflows/ci.yml`（Python 3.12 / ruff → `bash -n` → pytest。`main` と `claude/**` を対象）
- `tests/test_repo_layout.py` — 必須ファイル・共通ルール・`CLAUDE.md` の import・
  フックの参照先・シェルスクリプトの実行権限を検証
- `README.md` / `CHANGELOG.md` / `docs/history/` / `docs/issues/`

### 計画からの逸脱
- なし。計画 §4 に挙げた `shelf_gaze_ml` からの3点の意図的な差分は、そのまま実施した。
  - `.claude/settings.json` をコミット対象にする（個人設定は `settings.local.json` へ分離）
  - `safe-push.sh` は `main` 固定をやめ、現在のブランチを push（`main` にいる場合はエラー停止）。
    `SKILL.md` の記述も合わせて更新した
  - Python 3.12（`SPEC.md` §13 準拠）

## 2. 検証結果

| 検証 | 結果 |
|---|---|
| `ruff check .` | pass（All checks passed） |
| `python -m pytest tests/ -q` | pass（22 passed） |
| 全 `*.sh` の `bash -n` | pass |
| `.claude/settings.json` の JSON 妥当性・フック参照先 | pass（テストで担保） |

## 3. 未対応事項

- **実装は未着手。** `SPEC.md` §12 の M1（`personas_base` 構築）から開始する。
  `[project]` / `[build-system]` と実行時依存（`requirements.txt`）は M1 着手時に追加する。
- **既定ブランチが未確定。** リモートにコミットが1つも無い状態から作業ブランチを push したため、
  `main` の扱い（作成・既定化）は利用者の判断が必要。
- **未決事項は仕様書側に残ったまま**（`SPEC.md` §17）。特に R1（キャリブレーション用の
  過去実査データ確保）はフェーズ1の実装内容には影響しないが、利用側の前提として要確認。
- `retrospective` の共通ルールはコピーのため、更新時は `sync-agent-rules.sh` の実行が必要。
