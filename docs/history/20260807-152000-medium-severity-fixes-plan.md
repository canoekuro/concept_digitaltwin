# コードベースレビューの中優先度所見（M1〜M8）の修正（計画）

- **日付**: 2026/08/07 15:20
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)
- **依頼**: 「次、中を対応してください」

## 目的

コードベース全体レビューで**中**に分類した8件（M1〜M8）を修正する。
低（L1〜L10）と retrospective（R1〜R3）は所見に残し `/pdca` へ引き継ぐ。

## 設計中に判明したこと（利用者に確認済み）

### M3 は所見の記述が事実として誤っていた

「推論リクエストにタイムアウトが無い」「無期限に占有」「無言で停止」と書いたが、
`databricks-sdk` は既定でタイムアウトを掛けている（per-request 60秒 /
再試行予算 300秒）。実際の問題は**再試行の層が2つ重なっている**ことで、
`llm/budget.py` の「再試行の軸は4つ」に SDK 分が書かれておらず、
`RetryStats` にも数えられていない。

利用者の判断により **案A（明示＋文書化）** を採る。挙動は変えない。
`retry_timeout_seconds` は触らない——小さくすると、こちらの
`_RETRYABLE_ERROR_NAMES` が `ConnectionError` / `TimeoutError` を含まないため、
今 SDK が吸収している接続リセットが致命傷になる。

### M5 は「到達しうる」が誤り

`select_members()` が全セルで必要数を保証してから `panels` を書くので、
候補ゼロのセルは存在しえない。**防御的な修正**として実施し、所見の表現を訂正する。

### Spark テストがこの環境で実行できない

M5 / M7 は Spark 経路にしかない。利用者の判断により
**両方修正し、CI の `spark-test` ジョブで確認する**。

なお M1 / M7 は「判定対象の選び方」「単位の換算」を**純関数に切り出す**ことで、
中身だけは非 Spark テストで固定できるようにする。

## 対象ファイル

| ファイル | 対応 |
|---|---|
| `persona_sim/run/flags.py` | M1: `straightline_question_ids()` を追加 |
| `persona_sim/run/run.py` | M1: 対象の決め方を差し替え |
| `persona_sim/llm/databricks.py` | M2: 文言マッチを絞る／M3: タイムアウト明示・SDK 再試行を明記 |
| `persona_sim/llm/budget.py` | M3: 「再試行が4つ」を5つに |
| `persona_sim/panel/schema.py` | M3: `DEFAULT_REQUEST_TIMEOUT_SEC` と `request_timeout_sec` |
| `persona_sim/panel/loader.py` | M3: 同上の読み取り |
| `persona_sim/llm/registry.py` | M3: クライアントへ渡す |
| `persona_sim/run/executor.py` | M4: 中断時に走行中の future を回収 |
| `persona_sim/panel/build.py` | M5: 割り付けにあるセルを必ず1周する |
| `persona_sim/panel/screening.py` | M7: `skipped_session_count()` を追加 |
| `persona_sim/aggregate/crosstab.py` | M6: 索引を導入 |
| `persona_sim/__init__.py` | M8: `attribution_text()` |
| `persona_sim/cli.py` / `aggregate/export.py` / `metadata.py` | M8: 出力先に免責を載せる |
| `tests/test_flags.py` / `tests/test_executor.py` | 新規 |
| `tests/test_databricks_client.py` / `test_screening.py` / `test_export.py` / `test_results_page.py` / `test_repo_layout.py` / `test_screening_spark.py` | 追記 |
| `docs/issues/20260807002.md` | M3・M5 の訂正、M1〜M8 の対応状況 |
| `CHANGELOG.md` | 項目31 |

## 検証方法

1. `ruff check .` と `./scripts/run-tests.sh`（非 Spark）
2. **新規テストが修正前のコードで落ちること**を、対象ファイルを退避 →
   `git checkout` → 実行 → 復元 の手順で確認する
3. M6 は挙動不変。既存の集計テストが同じ数字を出し続けることで担保する
4. M3 / M8 は実測（タイムアウトが設定から読めること・免責が出ること）
5. Spark テストは**この環境で実行できない**ので CI の `spark-test` ジョブに委ねる

## ブランチ

`claude/code-review-bugs-improvements-y7e7wl`（PR #26 に積む）。
