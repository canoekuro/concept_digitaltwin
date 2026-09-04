# 高優先度の所見（H1〜H4）の修正（計画）

- **日付**: 2026/08/07 14:15
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)
- **依頼**: 「じゃあ優先度高から修整をお願い」

## 目的

コードベース全体レビュー（`docs/issues/20260807002.md`）で洗い出した25件のうち、
**高**に分類した所見を修正する。中・低・retrospective 側は所見に残したまま `/pdca` に引き継ぐ。

## スコープの変更（設計中に判明）

当初 H1〜H5 の5件を修正する予定だったが、**H5 は重大度の判定が誤っていた**ことが
設計中に分かった。

`submit_survey()` の呼び出し元は1箇所で、渡る `survey_id` は必ず
`{slug}_{時刻}_{secrets.token_hex(3)}` ＝投入ごとに一意。したがって冪等トークンは
実質「投入1回につき1つ」で、docstring が言う二度押し防止は実際に効いている。
所見に書いた「同じ `survey_id` は64日間実行できない／失敗した投入し直しで確実に踏む」は誤り。

さらに当初案（nonce を混ぜる）は、nonce の寿命を誤ると今効いている二度押し防止を壊す。

利用者に確認したうえで、**H5 はコードを変えず所見を訂正する**方針とした。
したがってコードの修正は **H1〜H4 の4件**。

## 対象ファイル

| ファイル | 変更 |
|---|---|
| `persona_sim/__init__.py` | H1: 版を `importlib.metadata` から引く。docstring の実装範囲を更新 |
| `persona_sim/run/session.py` | H2: `_unit_groups()` が履歴なしなら全ユニットに提示物を持たせる。`_fallback_stimuli()` の `ALL_STIMULI` 分岐を削除 |
| `persona_sim/panel/infer.py` | H3: `run_inference()` で `future.result()` を包む |
| `app/lib/context.py` | H4: `_QUERY_LOCK` と `query()` を追加。`runs()` から `_connection` 引数を落とす |
| `app/views/results.py` | H4: 取得3箇所を `context.query()` 経由にする |
| `persona_sim/uiconfig/jobs.py` | H5: docstring に前提を明記（**挙動は変えない**） |
| `tests/test_repo_layout.py` | H1 のテスト |
| `tests/test_session.py` | H2 のテスト |
| `tests/test_infer.py` | H3 のテスト |
| `tests/test_results_page.py` | H4 のテスト |
| `docs/issues/20260807002.md` | H5 を L10 へ訂正。H2 の発現条件に `balanced` を追記。H1〜H4 に「対応」を追記 |
| `CHANGELOG.md` | 項目30を追記 |

## 変更内容

### H1. 版の二重定義を消す

`__version__ = "0.1.0"` のリテラルを消し、`importlib.metadata.version("persona-sim")` から引く。
未インストール時は `0+unknown` に落とす。`pyproject.toml` が版の SSoT になる。

### H2. 提示順を全ユニットに持たせる（所見の案A）

`_unit_groups()` に `keeps_history = survey.design.memory is not Memory.NONE` を持たせ、
`stimuli_to_present` を `index == 0 or not keeps_history` で入れる。
同時提示・逐次提示の両方の分岐に同じ条件を適用する。

`_fallback_stimuli()` の `ALL_STIMULI` 分岐は**削除**する（利用者の指定により、例外は投げず
分岐ごと消す。到達すれば `KeyError: '*'` になる）。推測で定義順を返す経路を残さないことが目的。

### H3. 想定外例外をバッチの失敗として吸収する

`run_inference()` の受け取りを `_result_or_failure(group, future)` に切り出し、
`except Exception` で `InferBatchResult(error=...)` に落とす。
`error` が入れば `failed` が真になり、既存の `judge_error` 経路にそのまま乗る。

### H4. 共有接続の利用を直列化する

`app/lib/context.py` に `_QUERY_LOCK` と `query(fetcher, *args, **kwargs)` を足し、
接続を渡す取得関数をすべてこれ経由にする。`persona_sim/storage/warehouse.py` は変更しない
（CLI・ノートブックは単一スレッドで、ロックを持ち込む理由が無い）。

## 検証方法

1. `ruff check .` と `./scripts/run-tests.sh`（非 Spark）を通す
2. **新規テストが修正前のコードで落ちること**を、対象ファイルを一時的に戻して確認する
3. H1 は `.venv` 上で `persona-sim --version` を実測する
4. `git diff --stat` で変更範囲を確認する

Spark テストはこの環境に Java が無いため実行できない。H2 の `assigned_stimuli` との
突き合わせは、定義順と違う並びを持つパネル行を直接渡す純関数テストで代替する。

H4 の同時アクセスは Streamlit のランタイムを模さないと検証できないため、
ソースの形（`context.query()` を経由していること）を固定する方式にする。

## ブランチ

`claude/code-review-bugs-improvements-y7e7wl`（PR #26 に積む）。
