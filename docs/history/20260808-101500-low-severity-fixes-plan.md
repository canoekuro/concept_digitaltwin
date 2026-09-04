# コードベースレビューの低優先度所見（L1〜L8）の修正（計画）

- **日付**: 2026/08/08 10:15
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)
- **依頼**: 「低の件についてそれぞれ説明して」→（各件の説明のうえで）「続けてください」

## 目的

コードベース全体レビューで**低**に分類した10件のうち、利用者の判断が下りた範囲を対応する。

- **安全なもの**（差分が小さく挙動が変わらない）… L1・L3・L4・L5・L6・L7
- **判断が下りたもの** … L2 の `scale_points`（受け付けなくする）、L8（ルールを実態に合わせる）

**対応しないもの**: L9（型チェッカ導入）・L10（冪等トークン）・R1〜R3（retrospective 側）。
所見に残して `/pdca` へ引き継ぐ。

高（H1〜H4）・中（M1〜M8）は対応済みで、PR #26 の CI は全ジョブ green。
同じブランチ `claude/code-review-bugs-improvements-y7e7wl` に積む。

## 設計中に判明したこと

**M3 の修正で自分が L3 を作り込んでいた。** `DEFAULT_REQUEST_TIMEOUT_SEC` を
`llm/databricks.py` と `panel/schema.py` の両方に置いていた。L3 の一部として直す。

## 利用者の判断

| 論点 | 判断 |
|---|---|
| 対応範囲 | 安全なもの（L1・L3〜L7）をまとめて |
| `Question.scale_points` | **受け付けなくする**（使う先を作らない） |
| L8（ルールと実態のずれ） | **案A: ルールを実態に合わせる** |

`scale_points` は**破壊的変更**になる。調査定義・`examples/`・`SPEC*.md`・ノートブックの
どこにも書かれていない（参照はコードと `tests/test_validate.py` の1件だけ）ことを
確認したうえでの判断。

## 対象ファイル

| ファイル | 対応 |
|---|---|
| `persona_sim/panel/quotas.py` | L1: `max(0, ...)` |
| `persona_sim/panel/schema.py` | L2: `REMOVED_QUESTION_FIELDS` 追加・`scale_points` / `is_empty()` 削除 |
| `persona_sim/panel/loader.py` | L2: 廃止キーの拒否 ／ L3: 既定の参照化 |
| `persona_sim/panel/validate.py` | L2: 検証削除 ／ L7-1: `Counter` |
| `persona_sim/panel/screening.py` | L2: `SCREENER_COLUMNS` 削除 |
| `persona_sim/run/flags.py` | L2: 未参照の純関数2つ削除 |
| `persona_sim/errors.py` | L2: `DesignMismatchError` 削除 |
| `persona_sim/metadata.py` | L4: `ROLE_CANDIDATE` |
| `persona_sim/llm/databricks.py` | L3: 定数の重複解消 ／ L6: `_client_lock` |
| `persona_sim/textwidth.py` | L7-3: 新規（`display_width()`） |
| `persona_sim/cli.py` / `persona_sim/run/progress.py` | L7-3: 幅の計算を共有 |
| `persona_sim/run/parsing.py` | L7-4: `raw_decode()` |
| `app/lib/context.py` / `app/views/results.py` | L5: `@st.cache_data` |
| `AGENTS.md` / `app.yaml` | L8: 例外条項 |
| `tests/` | 上記に対応 |
| `docs/issues/20260807002.md` / `CHANGELOG.md` | 記録 |

## 再利用するもの（新規に書かない）

- 廃止キーの拒否作法: `panel/schema.py::REMOVED_MODEL_FIELDS` と `loader._prompt()` の検査
- 既定値の参照作法: `loader._prompt_rules()` の `defaults = PromptRules()`
- Streamlit のキャッシュ作法: `app/lib/context.py::runs()`（`_` 始まりでキーから外す）
- ロール定数: `panel/sampling.py::ROLE_CANDIDATE`
- ソース固定テストの作法: `tests/test_results_page.py`（H4 で追加したもの）

## 検証

```bash
# CI と同じ手順を再現する（H1 の失敗を踏まえ、これを既定の確認手段にする）
python3 -m venv /tmp/ci-venv && /tmp/ci-venv/bin/pip install -r requirements-ci.txt
rm -rf persona_sim.egg-info
/tmp/ci-venv/bin/python -m pytest tests/ -q -m "not spark"
ruff check .

# 新規テストが修正前のコードで落ちること（対象ファイルを退避 → git checkout → 実行 → 復元）
# 破壊的変更の実測（既存の調査定義が読めること）
# Spark（この環境では実行できないので CI の spark-test ジョブで確認する）
```

`quotas.allocate_cell_sizes()`（L1）と `screening`（L2）は Spark テストが通る経路なので、
**CI の `spark-test` ジョブの結果を必ず確認する**。

## ブランチ

`claude/code-review-bugs-improvements-y7e7wl`（PR #26 に積む）。
