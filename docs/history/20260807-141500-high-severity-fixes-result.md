# 高優先度の所見（H1〜H4）の修正（結果）

- **日付**: 2026/08/07 14:15
- **計画**: [20260807-141500-high-severity-fixes-plan.md](20260807-141500-high-severity-fixes-plan.md)
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)

## 実施内容

レビューで**高**に分類した所見のうち **H1〜H4 を修正**した。
**H5 は重大度の判定が誤っていたため取り下げ、低（L10）へ移して所見を訂正した**（後述）。

## 変更したファイル

| ファイル | 変更 |
|---|---|
| `persona_sim/__init__.py` | H1: 版を `importlib.metadata` から引く。docstring の実装範囲を更新 |
| `persona_sim/run/session.py` | H2: `_unit_groups()` が履歴なしなら全ユニットに提示物を持たせる。`_fallback_stimuli()` の `ALL_STIMULI` 分岐を削除 |
| `persona_sim/panel/infer.py` | H3: `_result_or_failure()` を新設し `future.result()` を包む |
| `app/lib/context.py` | H4: `_QUERY_LOCK` と `query()` を追加。`runs()` から `_connection` 引数を削除 |
| `app/views/results.py` | H4: 取得3箇所を `context.query()` 経由に。不要になった `connection` 変数を削除 |
| `persona_sim/uiconfig/jobs.py` | H5: docstring に前提を明記（**挙動は変えていない**） |
| `tests/test_repo_layout.py` | +1件 |
| `tests/test_session.py` | +3件 |
| `tests/test_infer.py` | +2件 |
| `tests/test_results_page.py` | +2件 |
| `docs/issues/20260807002.md` | 対応状況・H5 の訂正・H2 の発現条件の追記 |
| `CHANGELOG.md` | 項目30 |

## 各所見の対応

### H1. 版の二重定義

`__version__ = "0.1.0"` のリテラルを消し、`importlib.metadata.version("persona-sim")` から引くようにした
（未インストール時は `0+unknown`）。`pyproject.toml` が版の SSoT になる。
docstring の「現在の実装範囲は M1 と M2」も実態（M1〜M7 ＋ Web UI）に直した。

実測で確認した。

```
$ uv run persona-sim --version
persona-sim 0.3.0          # 修正前は 0.1.0
```

### H2. 同時提示 × `memory: none` の提示順

`_unit_groups()` に `keeps_history` を持たせ、`stimuli_to_present` を
`index == 0 or not keeps_history` で入れるようにした。同時提示・逐次提示の
両方の分岐に同じ条件を適用している。

`_fallback_stimuli()` の `ALL_STIMULI` 分岐は**削除**した。
利用者の指定により例外は投げず分岐ごと消してあるので、万一到達すれば `KeyError: '*'` になる。
「なぜ補わないか」（補うと `ctx.stimuli` の並び＝調査定義順になり、ペルソナごとの
提示順を取り違える）は関数の docstring に残した。

発現条件は所見に `rotation: random` としか書いていなかったが、
**`balanced` でも起きる**ので所見側に追記した（`assignment_table()` が
`cell_rank` ごとに巡回窓を作るため、やはり定義順とずれる）。

### H3. `infer` 判定の想定外例外

`run_inference()` の受け取りを `_result_or_failure(group, future)` に切り出し、
`except Exception` で `InferBatchResult(error=...)` に落とすようにした
（`group` が要るので `zip(groups, futures, strict=True)` で回す）。

`error` が入るので `failed` が真になり、既存の経路がそのまま働く。
`batches_failed` に計上され、`_collect()` が `codes` に入れず `judge_error` を立て、
`read_screener_codes()` が未判定として除くので**再実行で聞き直される**。

### H4. 共有接続の直列化

`app/lib/context.py` に `_QUERY_LOCK` と `query(fetcher, *args, **kwargs)` を足し、
接続を渡す取得関数（`fetch_survey` / `build_result` / `fetch_responses_raw` / `fetch_runs`）を
すべてこれ経由にした。

副次的に `runs()` の `_connection` 引数を落とした。`query()` が `connection()` から
接続を取るので受け取る必要が無く、残すと「渡しているのに使っていない」引数になるため。
`results.py` の `connection = context.connection()` も不要になったので削除した。

`persona_sim/storage/warehouse.py` は**変更していない**。CLI・ノートブックは単一スレッドで、
ロックを持ち込む理由が無い（アプリ側の都合をパイプラインに漏らさない）。

## H5 の取り下げについて

**修正に着手した段でコードを追い直したところ、レビュー時の重大度判定が誤っていた。**

所見には「同じ `survey_id` は約64日間、二度と実行できない」「失敗した実行の投入し直しで
**確実に踏む**」と書いたが、`submit_survey()` の呼び出し元は
`app/views/survey_design.py` の1箇所だけで、渡る `survey_id` は必ず
`uiconfig.build.survey_id()` が作る `{slug}_{時刻}_{secrets.token_hex(3)}`。
投入成功時に session_state から捨てられるので、**次の投入にはまた新しい ID が振られる**。

つまりトークンは実質「投入1回につき1つ」で、docstring が言う二度押し防止は
**実際に成立している**（投入が成功するまで `survey_id` が固定されるため）。

さらに、所見が提案した「nonce を混ぜる」修正は、nonce の寿命を誤ると
**今効いている二度押し防止を壊す**。現行の `survey_id` が既に「投入1回＝1値」なので、
nonce を足しても得るものが無い。

残る本当のリスクは「呼び出し側が `SurveyForm.survey_id` を固定した場合に
2回目が無言で空振りする」という狭い罠だけ。**live なバグではなく latent な罠**である。

利用者に確認したうえで、**コードの挙動は変えず**、
`idempotency_token()` の docstring に「`survey_id` が投入ごとに一意であることに
依存している」ことと、ID を固定する経路を足すときは併せて見直す旨を明記した。
所見は L10 へ移し、当初の記述の何が誤りだったかを書いた。

## 検証結果

| 項目 | 結果 |
|---|---|
| `ruff check .` | パス |
| `./scripts/run-tests.sh`（非 Spark） | **573件全件パス**（変更前 565件、8件追加） |
| 新規テストが修正前のコードで落ちること | **確認済み**（下記） |
| H1 の実測 | `persona-sim --version` が `0.3.0` を出すことを確認 |
| Spark テスト | **未実行**（この環境に Java が無い） |

対象ファイルを一時的に修正前へ戻して新規テストを走らせ、5件が落ちることを確認した。

```
FAILED tests/test_session.py::test_simultaneous_without_memory_keeps_the_assigned_order_on_every_question
FAILED tests/test_session.py::test_sequential_without_memory_presents_on_every_question
FAILED tests/test_infer.py::test_run_inference_survives_an_unexpected_exception_in_one_batch
FAILED tests/test_infer.py::test_run_inference_survives_a_persona_missing_from_the_mapping
FAILED tests/test_repo_layout.py::test_version_matches_pyproject - AssertionError: assert '0.1.0' == '0.3.0'
```

残る3件は回帰テスト（`test_memory_keeps_presenting_only_at_the_group_head`）と
H4 のソース固定テスト2件で、これらは性質上、修正前後で意味が変わらないか
`app/` の変更が前提になる。

### テストの書き方について補足

- **H2**: Spark を使わずに `assigned_stimuli` の順序と突き合わせるため、
  定義順と違う並び（`["c3", "c1", "c2"]`）を持つパネル行を直接渡している。
  `apply_assignment()` が実際にこの形を作ることは既存の Spark テストが押さえている
- **H3**: `_PERSONAS` は u1/u2 しか持たないので、`RuntimeError` の経路を試すテストでは
  u3/u4 を足した `_PERSONAS_4` を使う。そうしないと `personas[uuid]` の `KeyError` が
  先に出て、試したい経路に届かない。その `KeyError` 自体は別テストで押さえてある
- **H4**: この環境に streamlit が入っておらず `app/` を import できないため、
  **ソースの形で固定する**方式にした（`context.query(` があり
  `warehouse.fetch_*(connection` が無いこと、`with _QUERY_LOCK:` があること）

## 未対応事項

- **中（M1〜M8）・低（L1〜L10）・retrospective（R1〜R3）は未着手。**
  `docs/issues/20260807002.md` に残してあり、`/pdca` に引き継ぐ
- **L8（`app.yaml` のカタログ名直書き）は判断待ち。**
  バグではなくルールと実態のずれで、`AGENTS.md` 側と `app.yaml` 側のどちらを直すかは
  利用者の判断が要る
- **L10（旧 H5）の固定 ID の空振りは塞いでいない。**
  塞ぐなら `jobs.run()` に `nonce` 引数を足し、画面が session_state の値を渡す形になる
- **Spark テストは未実行**（Java の無い環境）。H2 は Spark 経路を変えていないが、
  `-m spark` の全件パスは確認できていない
- **H4 の同時アクセスは実機確認していない。** Streamlit のランタイムを模さないと
  意味のある並行テストにならない。**Databricks Apps 実環境での確認は未実施**
