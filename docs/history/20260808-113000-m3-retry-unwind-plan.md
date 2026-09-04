# M3 の多重再試行をほどく（計画）

- **日付**: 2026/08/08 11:30
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)（M3）
- **依頼**: 「M3 の多重再試行をほどく」

## 目的

中優先度の M3 で「別サイクルに回す」と書いた**再試行の層をほどく**件を片付ける。

## 設計中に判明したこと（前サイクルの記述はまた誤っていた）

インストール済み `databricks-sdk` の `_base_client.py` / `retries.py` /
`errors/customizer.py` を読み直したところ、**2つの層は重なっていなかった**。

| 失敗 | SDK 側 | 自前の層 |
|---|---|---|
| 429 / 503 | `Retry-After` を尊重して再試行（300秒まで） | **到達しない** |
| 500 / 502 / 504 | 再試行しない | **ここが担当**（5回） |
| 接続リセット・socket タイムアウト | 再試行（300秒まで） | **到達しない** |
| 400 系 | 再試行しない | 分類して送出 |

SDK が予算を使い切ると `TimeoutError(...) from 元の例外` を投げるが、この名前は
`_RETRYABLE_ERROR_NAMES` に無く `status_code` も持たないので `_is_retryable()` が
偽を返して打ち切られる。**「最悪 5 × 300秒 ≒ 25分」は起きない。**

### 残っていた実害

1. **SDK の枯渇が恒久エラーに化ける。** 429 の嵐が300秒続くと
   `TimeoutError: Timed out after 0:05:00` という、レート制限だと読み取れない文言で
   `LLMError`（再試行しても無駄）になる
2. `_reason_key()` が `TimeoutError` を記録するので、429 だったことが記録に残らない
3. 自前の層は 5xx 専用なのに5回・最大30秒のバックオフを持っており過剰

## 利用者の判断

| 論点 | 判断 |
|---|---|
| 再試行の持ち主 | **SDK に寄せて自前を薄く**（自前への一本化は採らない） |
| 試行回数 | **減らす** |

一本化を採らないので 429・503・接続エラーの再試行は `RetryStats` に載らないまま残る。
**受け入れたうえで、載らないことを明記する。**

## 対象ファイル

| ファイル | 対応 |
|---|---|
| `persona_sim/llm/databricks.py` | `_unwrap_sdk_retry_timeout()` 新設・枯渇は投げ直さず `RetryableLLMError`・`max_attempts` 5→3・docstring 書き直し |
| `persona_sim/llm/client.py` | `RetryStats` / `retry_warning()` に「5xx しか数えられない」と明記 |
| `persona_sim/llm/budget.py` | 「入れ子で掛け算」の記述を「担当が分かれている」に訂正 |
| `tests/test_databricks_client.py` | 枯渇シグナルの扱い5件 |
| `docs/issues/20260807002.md` / `CHANGELOG.md` | 記録と**再訂正** |

## 変更しないもの

- `Config(retry_timeout_seconds=...)` … SDK の `Retry-After` 尊重は自前の指数バックオフより
  正確で、下げても `retried()` は諦める前に必ず1回眠るので得が無い
- `_is_retryable()` への接続エラー追加 … 一本化を採らないので不要
- `max_attempts` を設定に出す … 「調整したつもり」の旋回を増やさない

## 検証

```bash
python3 -m venv /tmp/ci-venv && /tmp/ci-venv/bin/pip install -r requirements-ci.txt
rm -rf persona_sim.egg-info
/tmp/ci-venv/bin/python -m pytest tests/ -q -m "not spark"
/tmp/ci-venv/bin/python -m ruff check .
```

`databricks-sdk` は `requirements-ci.txt` に無い（CI に入らない）。今回の変更は
SDK を import しない経路に閉じるので、注入クライアントで全て固定できる。

## ブランチ

`claude/code-review-bugs-improvements-y7e7wl`（PR #26 に積む）。
