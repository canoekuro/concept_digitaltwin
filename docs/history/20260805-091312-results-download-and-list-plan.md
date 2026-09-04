# 結果閲覧ページのダウンロード失敗を直し、一覧の表示を整理・高速化する（計画）

- 日時: 2026-08-05 09:13:12
- 起票: `docs/issues/20260805004.md`
- 対象: 結果閲覧ページ（`app/views/results.py`）と、その材料になる SQL Warehouse 経由の読み取り

## 1. 目的

起票の3件に対応する。

1. ダウンロードが `TypeError: Excel does not support timezones in datetimes` で失敗する
2. 「実行セッション数」「成功」「失敗」「完了日時」は読み手に意味が伝わらないので出さない。
   最初に見えるのは調査の選択と取得ボタンだけにする
3. 調査一覧が出るまで待たされる。速くできるなら速くする

## 2. 原因

| # | 原因 |
|---|---|
| 1 | ローデータに含めている `responses.ts` は `ts timestamp` 列（`run/run.py:50`）で、databricks-sql-connector が**タイムゾーン付き** `datetime` で返す。openpyxl は tz 付き datetime をセルに書けず `workbook.save()` で落ちる（`aggregate/export.py:233`）。集計表シート側に datetime は無いので、原因はローデータシートだけ |
| 2 | `app/views/results.py:82-86` の `st.metric` 4枚。ここが `RUN_COLUMNS` の `sessions_*` を使う唯一の箇所 |
| 3 | `fetch_runs()` にキャッシュが無い。Streamlit はウィジェットを触るたびにページ全体を再実行するので、調査を選び直すだけで毎回 SQL Warehouse を叩いている（`results.py:51`）。初回取得中のスピナーも無く、画面が固まって見える |

## 3. 決めたこと（ユーザー確認済み）

- **`ts` 列は落とす。** tz を外して書く／JST に換算する／ISO 文字列にする案も出したうえで、
  列ごと外す判断。よって `export.py` に「datetime が来たら tz を外す」汎用ガードは入れない
  （黙ってラベル無しの UTC を書くことになり、どのタイムゾーンで出すかを決めないまま
  値だけ出す状態に戻るため）。
- **調査選択プルダウンの表示名は現状のまま。** 完了時刻と `survey_id` は同名の別実行を
  見分けるためのもの（`docs/SPEC_UI.md` §4.1）。不要だったのはメトリクス行だけと解釈する。
- **`aborted_reason` の警告は残す。** 起票に含まれておらず、データが欠けているサインなので、
  黙って消すと不完全な集計を完全なものとして読んでしまう。

## 4. 変更対象ファイル

| ファイル | 変更内容 |
|---|---|
| `persona_sim/storage/warehouse.py` | `RAW_RESPONSE_COLUMNS` から `ts` を外す / `RUN_COLUMNS` を画面が使う4列に絞る / `runs_query()` に `QUALIFY` を足す |
| `app/lib/context.py` | 調査一覧の取得にキャッシュ付きラッパ `runs()` を追加 |
| `app/views/results.py` | メトリクス4枚を削除 / 一覧取得を `context.runs()` へ |
| `docs/SPEC_UI.md` | §4.1（取得列・キャッシュ・重複排除）、§4.4（ローデータに時刻を載せない理由） |
| `tests/test_warehouse.py` / `tests/test_results_page.py` | 回帰テストを追加 |

### 付随して直すもの

`runs_query()` に `QUALIFY ROW_NUMBER() OVER (PARTITION BY survey_id ORDER BY finished_at DESC) = 1`
を足し、`survey_id` ごとに最新1件へ畳む。行数が減るだけでなく、次の不整合が直る。

> `results.py:76` の `labels = {run["survey_id"]: ...}` は、同じ `survey_id` が複数回
> 実行されていると後勝ちで上書きされる。`ORDER BY finished_at DESC` なので**最古の行**が
> 残り、ラベルの完了時刻が実際に取得するデータ（`survey_definition_query()` は最新1件を
> 読む）と食い違う。

### 設計方針

- **CLI の出力は触らない。** `responses_raw.csv` は `frame.stream_responses_raw()`
  （Spark 経由）を通る別経路で、CSV はタイムゾーンを扱えるので落ちない。
- **キャッシュは `app/lib/context.py` に置く。** 「環境変数の解決と重い資源のキャッシュだけを
  持つ。ロジックは置かない」という位置づけ（冒頭 docstring）に合う。
- 更新ボタンは足さない。起票の「最初に見えるのは選択と取得ボタンのみ」を崩すため、
  TTL（60秒）で代替する。実行自体が数分かかるので体感の問題にならない。

## 5. 検証方法

- `./scripts/run-tests.sh`（非 Spark、現状499件が緑）+ `ruff check .`
- 追加するテスト
  - `responses_query()` が `ts` を引かないこと、`latency_ms` / `attempt` は残ること
  - tz 付き datetime を1セル渡すと `write_ui_workbook()` が `TypeError` で落ちること
    （列を外した理由そのものを固定する）
  - `runs_query()` が `sessions_*` を引かないこと、`survey_id` ごとに1件へ畳むこと
- 新規テストが変更前のコードで落ち、変更後に通ることを確認する
- Streamlit ページはテストから import しないので、`ast.parse` で構文を確認する

## 6. 記録

`docs/history/20260805-091312-results-download-and-list-result.md` に結果を残し、
`CHANGELOG.md` に1行追記する。
