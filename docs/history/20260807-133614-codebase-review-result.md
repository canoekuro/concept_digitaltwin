# コードベース全体レビュー（結果）

- **日付**: 2026/08/07 13:36
- **計画**: [20260807-133614-codebase-review-plan.md](20260807-133614-codebase-review-plan.md)

## 実施内容

`persona_sim/` 全モジュール・`app/`・`scripts/`・`hooks/`・`pyproject.toml` / CI 定義を通読し、
**25件**（当リポジトリ 22件 ＋ retrospective 3件）の所見を
`docs/issues/20260807002.md` に記録した。

**コードは1行も変更していない**（計画どおり、読み取り専用のレビュー）。

## 変更したファイル

| ファイル | 内容 |
|---|---|
| `docs/issues/20260807002.md` | 新規（所見の本体） |
| `docs/history/20260807-133614-codebase-review-plan.md` | 新規 |
| `docs/history/20260807-133614-codebase-review-result.md` | 本ファイル |
| `CHANGELOG.md` | 履歴一覧に項目29を追記 |

## 所見の要約

### 高（5件）— 結果の意味が変わる / 実行が壊れる

- **H1** `__version__ = "0.1.0"` と `pyproject.toml` の `0.3.0` が食い違う。
  `run_metadata.json` の `persona_sim_version` が実装を指さず、§9.1 の再現性記録が誤る
- **H2** `simultaneous` × `memory:none` × `rotation:random` で、
  **設問1だけがペルソナごとの提示順、設問2以降は全員同じ定義順**になる。
  `_fallback_stimuli()` の `ALL_STIMULI` 分岐が `ctx.stimuli`（調査定義順）を返しており、
  提示順のずれはどのテーブルにも残らないので事後に気づけない
- **H3** `infer` 判定で `LLMError` 以外の例外が出ると、`run_inference()` の裸の
  `future.result()` から伝播して**完了済みバッチの結果ごと**失われる。
  同じ立場の `executor._collect()` は `except Exception` で吸収している
- **H4** SQL Warehouse 接続を `@st.cache_resource` で全 Streamlit セッションが共有している。
  コネクタはスレッドセーフでなく、同時アクセスで結果が混ざりうる
- **H5** ジョブ投入の冪等トークンが `f"persona-sim-{survey_id}"` で永続。
  実態は「同じ `survey_id` は約64日間、二度と実行できない」で、
  かつ**成功と区別がつかない**（既存 run の ID が返る）

### 中（8件）

M1 straightline が `multi` を先頭コードだけで判定 ／
M2 `_looks_like_output_limit()` が設定不正まで拾い予算を3倍に広げる ／
M3 推論リクエストにタイムアウトが無くハングを E5 が検知できない ／
M4 E5 中断時に実行中セッションの結果を捨てている ／
M5 `finalize_panel()` が候補ゼロのセルを不足に数えない ／
M6 集計が O(コンセプト × 設問 × 全回答) ／
M7 `infer` の `sessions_skipped` と `sessions_total` の単位が違う ／
M8 `OUTPUT_DISCLAIMER` が CLI 成果物に出ていない

### 低（9件）

L1 proportion 合計が1超のときの誤配分 ／ L2 到達しないコードと二重実装（8シンボル ＋
効かない設定 `scale_points`）／ L3 既定値の二重定義 ／ L4 ロール名のリテラル直書き ／
L5 Web UI が操作のたびに xlsx を再生成 ／ L6 `WorkspaceClient` 遅延生成のロック欠如 ／
L7 細かいもの4件 ／ L8 `app.yaml` のカタログ名直書き ／ L9 型チェッカが CI に無い

### retrospective（3件）

R1 SessionStart フックの cwd 依存 ／ R2 `link-repo.sh` の冪等性 ／
R3 `git pull` のタイムアウト欠如

## 検証結果

| 項目 | 結果 |
|---|---|
| 所見の行番号・シンボルの実在確認 | 全件 `sed -n` で確認済み |
| 「未使用」の再確認（`grep`） | 確認済み。テストからのみ参照されるものは区別して記載 |
| `ruff check .` | パス |
| `./scripts/run-tests.sh`（非 Spark） | **565件全件パス**（変更前と同数。コード未変更のため当然だが、環境が壊れていないことの確認） |
| Spark テスト | **未実行**（この環境に Java が無い。コード未変更なので影響は無い） |
| `git diff --stat origin/main` | `docs/` と `CHANGELOG.md` のみ |

H1 だけは記録の正確さに関わるので、`.venv` 上で実測して所見に載せた。

```
$ uv run python -c "import persona_sim, importlib.metadata as m; \
    print(persona_sim.__version__, m.version('persona-sim'))"
0.1.0 0.3.0
$ uv run persona-sim --version
persona-sim 0.1.0
```

## レビュー中に訂正した判断

`app.yaml` の `PERSONA_SIM_CATALOG: 'research_system'` を、当初は
「`AGENTS.md` が許容するサービングエンドポイント名と同じ扱い」として
**問題無しの側に分類していた**。しかし `AGENTS.md:93` は
「カタログ名・スキーマ名は書かない」と明示しており、エンドポイント名の例外は
`CHANGELOG.md` 項目9 で**エンドポイント名についてだけ**カーブアウトされたもので、
同項目は「カタログ名・資格情報は従来どおり環境変数からのみ読む」と据え置いている。
`app.yaml` はその据え置きと整合していないので、**L8 として所見に格上げした**。

ただしこれはバグではなく解釈のずれである。`research_system` / `default` は
顧客名でも内部ホスト名でもないので実害は小さい。
所見には「ルールを実態に合わせる（案A）」「実態をルールに合わせる（案B）」を並べ、
**どちらを採るかは判断材料だけ書いて決めていない**。

## 未対応事項

- **修正は一切行っていない。** 全25件を `/pdca` に引き継ぐ
- **H2 の修正方針は決めていない。** 所見に2案（提示順を全ユニットに持たせる案A ／
  fallback に `assigned_stimuli` を渡す案B）を併記した。推奨は案A
  （推測で埋める経路そのものを無くすため）だが、利用者の指定は無かった
- **L8 は判断待ち。** 上記のとおり
- **H4 / H5 は実機確認ができていない。** Streamlit の同時アクセスと
  Databricks Jobs の冪等トークンの挙動は、この環境から再現できない。
  いずれも API の仕様とコードの読みからの指摘で、
  修正時には実機での確認が要る（既存の history と同じく、
  Databricks 実環境での確認は未実施）
- **Spark テストは未実行**（Java が無い環境のため）。コード未変更なので影響は無いが、
  `/pdca` で修正に入る際は M5 / M6 の検証に必要になる
