# コードベース全体レビュー（計画）

- **日付**: 2026/08/07 13:36
- **依頼**: 「コード全体を見てバグ、改善点を洗い出してほしい」

## 目的

`persona_sim` パイプラインと Web UI の全ソースを通読し、バグと改善点を洗い出して
`docs/issues/` に記録する。

## スコープ

**読み取り専用のレビューに限る。** 利用者の指示により、この作業では
**コードを一切変更しない**。修正は `/pdca` で別サイクルに引き継ぐ。
リポジトリの `/codebase-review`（`.claude/commands/codebase-review.md`）と同じ扱い。

読む範囲:

- `persona_sim/` 全モジュール（`panel` / `run` / `llm` / `aggregate` / `storage` /
  `personas` / `uiconfig` / `cli.py` / `metadata.py` / `config.py` / `spark.py` / `errors.py`）
- `app/`（`app.py` / `lib/` / `views/`）
- `scripts/` / `.github/workflows/ci.yml` / `pyproject.toml` / `app.yaml`
- `canoekuro/retrospective` の `hooks/` / `scripts/`

テストは実装の裏取りに使う（実行はしない）。

## 対象ファイル（変更するもの）

| ファイル | 内容 |
|---|---|
| `docs/issues/20260807002.md` | 新規。所見の本体 |
| `docs/history/20260807-133614-codebase-review-plan.md` | 本ファイル |
| `docs/history/20260807-133614-codebase-review-result.md` | 結果 |
| `CHANGELOG.md` | 履歴一覧に1エントリ追記 |

**`persona_sim/` `app/` `scripts/` `hooks/` `pyproject.toml` `app.yaml` には触らない。**
retrospective リポジトリも変更しない。

## 所見ドキュメントの構成

既存の `docs/issues/*.md`（`20260805004.md` / `20260807001.md`）の書き方に合わせ、
所見ごとに「現象 → 該当箇所（`file.py:行`）→ なぜそうなるか → 修正方針」で書く。

- 冒頭に一覧表（`/pdca` 側が着手順を決められるように）
- 重大度で章立て（高／中／低／retrospective 側）
- 末尾に「見て問題が無かったもの」を残す（同じ場所を再度調べる手間を省くため）

修正方針は書くが**実装はしない**。判断が要るもの（複数案があるもの）は
案を並べて判断材料を書き、決めない。

## 検証方法

コード変更が無いので、確認するのは文書の整合性だけ。

1. 所見に書いた行番号・シンボルが実在することを、`sed -n` で1件ずつ引いて確認する
2. 「未使用」と書いたシンボルが本当に参照されていないことを `grep` で再確認する
   （テストからの参照は「production 未使用」と区別して書く）
3. `git status` / `git diff --stat origin/main` で、ドキュメントしか触っていないことを確認する
4. `ruff check .` と `./scripts/run-tests.sh` を流し、壊していないことを確認する

## ブランチ

`claude/code-review-bugs-improvements-y7e7wl`（`origin/main` から）。
push 後に PR を作成する。
