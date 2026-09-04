<!-- SSoT: canoekuro/retrospective の agents-rules/plan-before-modify.md。
     各repoの .agents/rules/ 配下はこのファイルの同期コピー。編集は retrospective 側で行い、
     scripts/sync-agent-rules.sh で各repoへ再同期すること。 -->

# Plan Before Modify (事前計画と確認の義務化)

**Activation:** This rule is **ALWAYS ON** for all code modifications, configuration changes, prompt changes, refactoring, documentation updates that record design decisions, and feature additions.

## 1. 修正前の計画立案 (Plan Before Action)

- コード、設定ファイル、プロンプトテンプレート、仕様書、設計文書、運用ルールを修正・変更する前に、必ず**修正計画（変更の目的、対象ファイル、具体的な変更内容、検証方法）**を作成すること。
- いきなりコードやドキュメントを修正（`replace_file_content`、`write_to_file`、スクリプトによる書き換え等）してはならない。
- 計画段階で、`docs/history/` の plan/result 記録と `CHANGELOG.md` 更新が必要かを明示すること。

## 2. ユーザーへの確認と許可 (Request User Approval)

- 立案した修正計画をユーザーに提示し、実行してもよいか**明示的に確認（許可を依頼）**すること。
- ユーザーから「OK」「進めてください」「いいでしょう」などの明確な許可（Approval）を得るまでは、実際の修正作業を行わないこと。
- ユーザーが方針や判断を説明しただけの場合、それを実装許可とみなしてはならない。実装に移る前に、必ず計画と対象ファイルを提示して確認を取ること。
- 緊急修正や小さな文言修正であっても、プロジェクトファイルを変更する場合はこの確認を省略しないこと。

## 3. 修正の実行と結果の保存 (Execute and Document Results)

- ユーザーの許可を得た後、計画に基づき修正を実行すること。
- `config/prompt_templates/`、`SPEC.md`、`docs/design/`、`docs/schema/`、アプリケーションコード、テストコード、重要な設定ファイルを変更した場合は、**計画とその結果をペアにして `docs/history/` 配下に保存**すること。
- issue 文書だけの整理など、実装・仕様・プロンプト本体を変更しない場合でも、ユーザーが記録を求めた場合は `docs/history/` に plan/result を保存すること。
- 記録は「変更後に思い出して書く」のではなく、実装計画の対象ファイルとして最初から含めること。
- **重要:** 時系列で整理しやすくするため、ファイル名には必ず日付・時刻・短いテーマを含め、原則として `YYYYMMDD-HHMMSS-theme-plan.md` と `YYYYMMDD-HHMMSS-theme-result.md` のペアにすること。
- result には、変更内容、検証結果、未対応事項を明記すること。

## 4. CHANGELOGの自動更新 (Update CHANGELOG.md)

- `docs/history/` 配下に計画・結果ドキュメントを保存した後は、**必ずプロジェクトルートにある `CHANGELOG.md` を更新**すること。
- `config/prompt_templates/`、`SPEC.md`、設計文書、スキーマ、アプリケーションコード、テストコードを変更した場合は、`CHANGELOG.md` 更新を省略してはならない。
- 追記する際は、`## [Unreleased]` の該当日付配下に、概要および保存した詳細ドキュメントへのリンク（`[詳細](docs/history/YYYYMMDD-HHMMSS-theme-plan.md)`）を1行で追加すること。
- docs/history の plan/result と CHANGELOG のリンクに不整合がないか、最終確認で `git diff` を確認すること。
