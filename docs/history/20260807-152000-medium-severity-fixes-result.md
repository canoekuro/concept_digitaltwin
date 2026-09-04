# コードベースレビューの中優先度所見（M1〜M8）の修正（結果）

- **日付**: 2026/08/07 15:20
- **計画**: [20260807-152000-medium-severity-fixes-plan.md](20260807-152000-medium-severity-fixes-plan.md)
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)

## 実施内容

**中（M1〜M8）の8件すべてを対応した。** うち M3 は所見の記述が事実として誤っていたため、
問題を再定義したうえで**挙動を変えない範囲**（明示＋文書化）で対応した。

## 各所見の対応

### M1. straightline が `multi` を先頭コードだけで見ていた

判定対象の決め方を `flags.straightline_question_ids()` に切り出し、
**single / scale だけ**を返すようにした。`countDistinct(answer_codes[0])` はそのまま——
対象が single / scale に限られたので先頭要素＝唯一の要素になり、コメントと実装が一致する。

純関数に切り出したのは**テストのため**でもある。判定本体は Spark でしか動かないが、
「どの設問を対象にするか」はこれで非 Spark に固定できる。

### M2. `_looks_like_output_limit()` が設定不正まで拾っていた

`_OUTPUT_LIMIT_HINTS` を打ち切りを示す語だけに絞り（`reached` / `output limit` /
`max output` / `truncat`）、`_INVALID_VALUE_HINTS`（`must be` / `invalid` /
`expected` / `out of range` / `not allowed`）に一致するものを除外した。

主経路が `_to_completion()` の `finish_reason == "length"` である旨も docstring に明記した。

### M3. 再試行が SDK 内部と二重になっている（**記述を訂正**）

所見の「タイムアウトが無い」「無期限に占有」「無言で停止」は**誤りだった**。
`databricks-sdk` の `_BaseClient` は既定で
`http_timeout_seconds=60` / `retry_timeout_seconds=300` を適用している。

実際の問題は**再試行の層が2つ重なっている**こと。外側（`_invoke_with_backoff`、5回）が
「1回失敗した」と数えるものは、内側（SDK の `@retried`、300秒）で既に投げ直された後の結果で、
最悪 `5 × 300秒 ≒ 25分`を1回の `complete()` に費やす。しかも
`RetryStats` は外側しか数えず、`llm/budget.py` の「再試行の軸は4つ」にも SDK 分が無い。

利用者の判断により**挙動は変えず、見えるようにした**。

1. `ModelConfig.request_timeout_sec`（既定 60 ＝ SDK の既定と同じ）を追加し、
   `loader` → `registry` → `DatabricksServingClient` →
   `WorkspaceClient(config=Config(http_timeout_seconds=...))` と渡す
2. `llm/budget.py` の「再試行が4つ」を**5つ**に直し、先頭2つが入れ子である旨を追記
3. `llm/databricks.py` のモジュール docstring に多重化と `RetryStats` の範囲を明記

**`retry_timeout_seconds` は触っていない。** 小さくすると、こちらの
`_RETRYABLE_ERROR_NAMES` が `ConnectionError` / `TimeoutError` を含まないため、
今 SDK が吸収している接続リセットが致命傷になる。ほどくなら
`_is_retryable()` の拡張とセットで行う必要があり、**別サイクルに回した（未対応）**。

### M4. E5 中断時に実行中セッションの結果を捨てていた

`cancel()` が偽を返した future（＝既に走っている）を `wait()` で待って `_collect()` する。
`aborted_reason` は最初の理由を保つので中断理由は上書きされない。

### M5. `finalize_panel()` が候補ゼロのセルを不足に数えなかった（**到達性を訂正**）

所見の「E1 を握りつぶす経路から到達しうる」は**誤りだった**。`select_members()` が
全セルで `achieved >= needed` を保証してから `panels` を書くので、
`requested` にあるセルは必ず候補を持つ。**防御的な修正**として実施した。

割り付けにあるセルを必ず1周するようにしたうえで、**割り付けに無いセルの候補も落とさない**よう
`requested ∪ by_cell` を回している——`requested` だけにすると、調査定義を変えた後に残った
候補行が書き戻されずに消える。

`_shortfall_message()` は変更していない。この修正で `result.achieved` が全セルを含むようになり、
`for cell_id in finalized.achieved` がそのまま正しくなるため。

### M6. 集計の走査

`_by_stimulus()` / `_by_question()` を足し、`crosstabs()` と `concept_summary()` の
線形走査を索引引きに置き換えた。**挙動不変のリファクタリング。**

### M7. `infer` の `sessions_skipped` の単位

換算を `screening.skipped_session_count()` に切り出し、`infer` はバッチ換算
（`infer_session_count()` を再利用）、`ask` は従来どおり `人数 × 設問数`。
M1 と同じく、Spark を要さずに中身を固定するための純関数化でもある。

### M8. `OUTPUT_DISCLAIMER` が成果物に出ていなかった

`persona_sim.attribution_text()` を追加し、帰属表示（§15.3）と免責（§15.1）を
**1つにまとめて**運ぶようにした。別々に運ぶと片方だけ付いた出力ができる、というのが
今回の事象そのものなので、呼び出し側が2つを覚えなくてよい形にした。

CLI 標準出力5箇所・`report.xlsx` の「概要」・UI のダウンロード・`run_metadata.json` に載せた。
CSV は表そのものなので載せていない。

## 検証結果

| 項目 | 結果 |
|---|---|
| `ruff check .` | パス |
| `./scripts/run-tests.sh`（非 Spark） | **590件全件パス**（変更前 573件、17件追加） |
| 新規テストが修正前のコードで落ちること | **確認済み**（下記） |
| M3 の実測 | 既定 60・上書き 120 がクライアントまで伝わることを確認 |
| M8 の実測 | `attribution_text()` が帰属表示と免責の両方を返すことを確認 |
| Spark テスト | 手元では**未実行**（Java が無い）。**CI で 82件全件パスを確認**（下記） |

対象ファイルを一時的に修正前へ戻して新規テストを走らせ、落ちることを確認した。

```
# import できない＝関数がまだ無い（M1 / M7）
ERROR tests/test_flags.py      … straightline_question_ids
ERROR tests/test_screening.py  … skipped_session_count

FAILED tests/test_executor.py::test_records_of_sessions_still_running_at_abort_are_kept
FAILED tests/test_databricks_client.py::test_an_invalid_max_tokens_400_is_not_an_output_limit[×3]
FAILED tests/test_export.py::test_the_summary_sheet_carries_the_attribution_and_the_disclaimer
FAILED tests/test_results_page.py::test_the_downloaded_workbook_carries_the_attribution_and_the_disclaimer
FAILED tests/test_repo_layout.py::test_attribution_text_carries_both_obligations
```

M6 は挙動不変のリファクタリングなので新規テストを足していない。
既存の集計テスト（`test_aggregate_pure` / `test_crosstab` / `test_export` /
`test_results_page`）が同じ数字を出し続けることで担保している。

## 未対応事項

- **Spark テストは手元で実行できていない**（この環境に Java が無い）が、
  **CI の `spark-test` ジョブで 82件全件パスを確認済み**（下記の追記を参照）。
  未検証のまま残る項目は無い
- **M3 の多重再試行はほどいていない。** `retry_timeout_seconds` を触るには
  `_is_retryable()` に `ConnectionError` / `TimeoutError` を足すのが必須で、
  変更が増えるため別サイクルに回した
- **低（L1〜L10）・retrospective（R1〜R3）は未着手。** `/pdca` に引き継ぐ。
  うち L8（`app.yaml` のカタログ名直書き）は利用者の判断待ち
- `flags.straightline_personas()` / `flags.flag_rates()` などの未使用純関数は
  L2 の扱いなので今回は触っていない
- **Databricks 実環境での確認は未実施**（既存の history と同じ）

---

## 追記: H1（版の SSoT）を CI の失敗を受けて作り直した

中優先度の作業中に、**1つ前のコミット（H1〜H4）の CI が落ちていた**ことに気づいた。

```
FAILED tests/test_repo_layout.py::test_version_matches_pyproject
E   AssertionError: assert '0+unknown' == '0.3.0'
```

### 何を間違えたか

H1 で `__version__` を `importlib.metadata.version("persona-sim")` から引く形にしたが、
**CI はパッケージをインストールしない**（`requirements-ci.txt` を入れて
pytest の `pythonpath = ["."]` で解決する）ため `PackageNotFoundError` になり、
フォールバックの `0+unknown` が入っていた。

手元では `./scripts/run-tests.sh`（＝`uv run`）がプロジェクトをビルド＆インストールするので
通ってしまい、**環境差に気づけなかった**。CI と同じ手順（venv に
`requirements-ci.txt` だけ入れて `python -m pytest`）を再現していれば手元で見つけられた。

### なぜテストだけの問題ではないか

同じ経路（未インストールで sys.path から使う）は Databricks のノートブックでも起きうる。
そのまま出していたら `run_metadata.json` の `persona_sim_version` が `0+unknown` になり、
**記録が要るまさにその経路で版が失われて**いた。元の `0.1.0`（古いが安定）より悪い。

### 直し方

**向きを逆にした。** `persona_sim/__init__.py` の `__version__` を唯一の出どころにし、
`pyproject.toml` は `dynamic = ["version"]` ＋
`[tool.setuptools.dynamic] version = {attr = "persona_sim.__version__"}` で読む。
二重定義は消えたまま、未インストールでも版が読める。

テストも「一致するか」から**「出どころが1つに保たれているか」**に変えた
（`pyproject` に版を直書きしていない・`dynamic` で解決している・
未インストールでも数字で始まる版が読める）。

`test_installed_version_matches_the_source` は**足してから外した**。
`.gitignore` された `persona_sim.egg-info` があると手元では解決し CI ではスキップするため、
実質「ローカルのビルド成果物が古い」ときにしか落ちない＝ノイズにしかならないと判断した。
配布メタデータと `__init__` が食い違わないことは `attr =` の指定で構造的に保証される。

### 検証

CI と同じ手順を手元で再現して確認した。

```
$ python3 -m venv /tmp/ci-venv
$ /tmp/ci-venv/bin/pip install -r requirements-ci.txt
$ rm -rf persona_sim.egg-info          # CI の新規チェックアウト相当
$ /tmp/ci-venv/bin/python -m pytest tests/ -q -m "not spark"
590 passed, 82 deselected
```

なお**同じコミットの Spark ジョブは成功していた**ので、H2（提示順）の
Spark 経路と M 系以前の Spark テストは通っている。


---

## 追記: CI で Spark テストの通過を確認した

手元で実行できなかった Spark 経路について、CI（`e8a006b`）の結果で確認した。

```
=============== 82 passed, 590 deselected in 1146.03s (0:19:06) ================
```

今回の修正に直接関わるものが通っている。

| テスト | 対応 |
|---|---|
| `test_a_cell_without_candidates_is_reported_as_short` | M5（今回追加した新規テスト） |
| `test_straightline_is_flagged_across_all_questions` | M1（既存。対象から multi を外しても壊れていない） |
| `test_infer_rerun_does_not_judge_again` / `test_low_incidence_stops_before_exhausting_the_retries` | M7（スキップ数の換算を通る経路） |
| `test_continuous_failure_aborts_and_keeps_completed_work` | M4（E5 中断時の回収） |
| `test_aggregates_match_the_raw_responses` / `test_top_box_equals_the_sum_of_its_options` | M6（索引化しても数字が変わらないこと） |

**非 Spark ジョブも成功**しており、`e8a006b` 時点で CI は全ジョブ green。
