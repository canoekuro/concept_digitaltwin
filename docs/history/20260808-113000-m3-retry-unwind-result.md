# M3 の多重再試行をほどく（結果）

- **日付**: 2026/08/08 11:30
- **計画**: [20260808-113000-m3-retry-unwind-plan.md](20260808-113000-m3-retry-unwind-plan.md)
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)（M3）

## 実施内容

**SDK に再試行を寄せ、自前の層を「5xx 専用の薄い層」に整理した。**
あわせて **M3 の記述を2度目の訂正**をしている——「掛け算になる」も誤りだった。

## 記述の訂正（2度目）

前サイクルの docstring・所見・CHANGELOG に書いた
「両者は掛け算になる」「最悪 `5 × 300秒 ≒ 25分`」は**誤り**。
`databricks-sdk` の実装を読み直すと、2つの層は重なっておらず担当が分かれていた。

| 失敗 | SDK 側 | 自前の層 |
|---|---|---|
| 429 / 503 | `Retry-After` を尊重して再試行（300秒まで） | **到達しない** |
| 500 / 502 / 504 | 再試行しない | **ここが担当** |
| 接続リセット・socket タイムアウト | 再試行（300秒まで） | **到達しない** |
| 400 系 | 再試行しない | 分類して送出 |

429 / 503 に必ず `retry_after_secs` を付けるのは SDK 既定の `_RetryAfterCustomizer` で、
`retried()` はそれがあれば必ず投げ直す。だから**生の 429 は自前の層に出てこない**。
SDK が予算を使い切ると `TimeoutError("Timed out after 0:05:00") from 元の例外` を投げるが、
この名前は `_RETRYABLE_ERROR_NAMES` に無く `status_code` も持たないので、
`_is_retryable()` が偽を返して**その場で打ち切られていた**。掛け算になりようがない。

## 各変更

### 1. SDK の枯渇シグナルを開く（`_unwrap_sdk_retry_timeout()`）

`TimeoutError` の `__cause__` を**1段だけ**開いてから分類する。開かないと
`status_code` が消え、**最も知りたい失敗（429 で詰まっていた）が「タイムアウト」としか
読めなくなる**。`run_metadata.json` の `endpoint_retries.reasons` にも
`TimeoutError` としか残らない。

1段だけにしたのは、`__cause__` を辿り続けると元の失敗のさらに下にある無関係な例外まで
拾いうるため。

### 2. 開いたものは投げ直さない（これが「ほどく」の実体）

SDK が既に300秒使った後なので、こちらで投げ直すのは二重に待つだけで通る見込みが無い。
ただし **`LLMError`（再試行しても無駄）ではなく `RetryableLLMError`** にした。
今までは「再試行できない失敗」だと嘘の分類をしていて、記録に残るコードも
`LLM` になっていた。

レート制限（429 / 503）だった場合は、文面に
「`model.concurrency` を下げるか、エンドポイント側の割り当てを確認すること」を添える。
**`W_ENDPOINT_RETRY` は 429 を数えられないので、ここが唯一の伝達経路になる。**

### 3. `max_attempts` を 5 → 3

自前の層が担当するのは 5xx（503を除く）だけ。3回続くならエンドポイント側の障害で、
回数を増やしても通らない。早く失敗させたほうが E5（`CONSECUTIVE_FAILURE_LIMIT = 20`）に
早く届き、詰まったまま課金され続ける時間が短い。設定には出さない。

### 4. `_RETRYABLE_ERROR_NAMES` の 429 系は残した

SDK 経由では届かないが、**消さない**理由をコメントにした。

- `workspace_client` を注入する経路（テスト・将来のアダプタ）では素通りして来る
- 将来 SDK が 429 を customizer から外したときに、消していると**無言で1回で捨てる**側に倒れる

「死んだ分岐を放置している」のではなく「安全側に倒して残している」と読めるようにした。

### 5. 見えない穴を明記した

一本化を採らなかったので、429・503・接続エラーの再試行は `RetryStats` に載らない。
`llm/client.py` の `RetryStats` と `retry_warning()` の docstring に
**「0 は詰まっていないの意味ではない」**と書いた。
出ない警告を「出ていないから健全」と読まれるのが一番まずい。

`llm/budget.py` の「先頭2つは入れ子・待ち時間は掛け算」も
「担当が分かれている・掛け算にならない」に直した。

## 変更しなかったもの

- **`retry_timeout_seconds`** … SDK の `Retry-After` 尊重は自前の指数バックオフより正確。
  下げても `retried()` は諦める前に必ず1回 `Retry-After` 秒眠る実装
  （`retries.py` の `clock.sleep()` は deadline の再確認より前にある）なので得が無い
- **`_is_retryable()` への接続エラー追加** … 一本化を採らないので不要。SDK が持っている
- **`http_timeout_seconds` / `request_timeout_sec`** … M3 前半で入れたもの。既定も挙動も同じ

## 追加したテスト（`tests/test_databricks_client.py`）

| テスト | 何を固定するか |
|---|---|
| `test_an_exhausted_sdk_retry_is_reported_as_the_original_failure` | 文面と `reasons` に **429** が残る（`TimeoutError` で潰さない）／打ち手を添える |
| `test_an_exhausted_sdk_retry_is_not_retried_again` | `api.calls == 1`・眠らない（**二重に待たない**） |
| `test_a_wrapped_connection_error_is_also_reported_as_itself` | 接続エラーも開く。レート制限ではないので並列度の話は出さない |
| `test_a_bare_timeout_without_a_cause_is_left_alone` | 包みでない `TimeoutError` を勝手に再試行可へ格上げしない |
| `test_the_default_attempt_count_is_three` | 既定の試行回数 |

既存テストは `max_attempts` を明示しているので既定変更の影響を受けなかった。

**修正前のコードで落ちること**を、対象ファイルを退避 → `git checkout` → 実行 → 復元で
確認した（5件中4件が落ちる。`test_a_bare_timeout_without_a_cause_is_left_alone` は
過剰な unwrap を防ぐ網なので修正前でも通る）。

## 検証

```
$ /tmp/ci-venv/bin/python -m pytest tests/ -q -m "not spark"
602 passed, 82 deselected

$ /tmp/ci-venv/bin/python -m ruff check .
All checks passed!
```

## 未対応事項

- **SDK の挙動はソースを読んで確認したもので、実測ではない。**
  `databricks-sdk` のバージョン（`.venv` に入っているもの）に対する読解であり、
  実エンドポイントに 429 を出させて確かめたわけではない
- **429・503・接続エラーの再試行は `RetryStats` に載らないまま。**
  利用者の判断（SDK に寄せる）を踏まえた既知の穴で、docstring に明記した
- **`request_timeout_sec` は `run_metadata.json` の `model` ブロックに入っていない**
  （調査定義に明記した場合のみ `survey_definition` から読める）。今回の範囲外として残す
- **Databricks 実環境での確認は未実施**（既存の history と同じ）
