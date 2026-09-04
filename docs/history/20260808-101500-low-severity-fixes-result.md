# コードベースレビューの低優先度所見（L1〜L8）の修正（結果）

- **日付**: 2026/08/08 10:15
- **計画**: [20260808-101500-low-severity-fixes-plan.md](20260808-101500-low-severity-fixes-plan.md)
- **起票**: [docs/issues/20260807002.md](../issues/20260807002.md)

## 実施内容

**低のうち L1・L3〜L8 を対応し、L2 は判断の下りた範囲（`scale_points` の廃止と
未参照シンボルの削除）まで進めた。** L9（型チェッカ導入）・L10（冪等トークン）・
R1〜R3（retrospective 側）は所見に残して引き継ぐ。

**`Question.scale_points` の廃止は破壊的変更**（既存 YAML に書いてあれば読み込みで止まる）。

## 各所見の対応

### L1. `allocate_cell_sizes()` が proportion 合計1超で誤配分する

`remaining = max(0, size - sum(allocated.values()))`。負のスライス `order[:-n]` は
「末尾 n 個を除く全部」を返すので、「余りを配る」処理が「余分に配る」処理に反転していた。

`validate._check_quotas()` が合計1.0 を止めるが、`warehouse.fetch_survey()`
（`runs.metadata_json` からの復元）は `validate_static()` を通さないので、この関数だけで守る。
反転の理由と到達経路をコメントに残した。

### L2（一部）. `Question.scale_points` を受け付けなくした

**使う先を作るのではなく廃止を選んだ。** 尺度の点数を決めているのは `options` の数そのもので、
繋ぐ先が無い。読み込みも検証（5 / 7 / 10）もしていたのにプロンプトにも集計にも効かず、
「設定したつもり」の記録だけが残る——`AGENTS.md` が繰り返し戒めている型の問題。

`panel/schema.py` に `REMOVED_QUESTION_FIELDS` を新設し、`REMOVED_MODEL_FIELDS` /
`REMOVED_PROMPT_FIELDS` と同じ作法で理由と移行方法つきに止める（`loader._question()` が検査）。
`validate` 側の検証も落とした。

**参照ゼロの5シンボルを削除した。** `flags.straightline_personas` / `flags.flag_rates()` /
`errors.DesignMismatchError` / `schema.PersonaFilter.is_empty()` /
`screening.SCREENER_COLUMNS`。`straightline_personas` は M1 で Spark 側に一本化済みなので、
残すと二重実装のまま。

**3件は残した。** `run/prompt.py::follow_up_message` と `InferResult.codes` /
`ScreeningVerdict.codes` は production からの参照こそ無いが、テストが
「質問ブロックだけを含む」「判定結果の中身が正しい」を固定する足場に使っている。
参照ゼロという理由だけでは落とさない——消すとテストごと失われる。

### L3. 既定値の二重定義

`_screening()` の `oversample_factor` と `_model()` の `max_tokens` / `max_tokens_open` /
`concurrency` / `request_timeout_sec` を dataclass の既定から引く形に揃えた。
`_model()` は必須項目である `endpoint` / `deployment` を先に解決してから
`defaults = ModelConfig(endpoint=..., deployment=...)` を作る。

**M3 の修正で自分が作り込んだ二重定義も直した。** `DEFAULT_REQUEST_TIMEOUT_SEC` を
`llm/databricks.py` と `panel/schema.py` の両方に置いていたので、`panel/schema.py` を
出どころにして `llm/databricks.py` は import する形にした。

### L4. ロール名の直書き

`metadata.py` の `row["role"] != "candidate"` を `ROLE_CANDIDATE` に置き換えた。挙動は同じ。

### L5. Web UI が操作のたびに xlsx を作り直す

`app/lib/context.py::workbook_bytes()`（`@st.cache_data`、キーは `survey_id`）に包み、
`app/views/results.py` はそれを呼ぶだけにした。ハッシュできない引数は `_` 始まりの名前で
キーから外す（`context.runs()` と同じ手法）。

**返すのはバイト列でパスではない。** `tempfile.TemporaryDirectory()` を関数の中に移した。
キャッシュがパスを返すと、ディレクトリが消えたあとの2回目以降に読めなくなる。

### L6. `WorkspaceClient` の遅延生成にロックが無い

double-checked locking にした。錠は `_stats_lock` と兼ねず `_client_lock` を別に持つ
（用途が違うものを1本の錠で兼ねると、待つ必要のない側まで待たせる）。

### L7. 細かいもの4件

1. `_check_unique_ids()` … `collections.Counter` で1回走査にした。
2. `_print_table()` の `zip(strict=True)` … `strict=True` は正しいので**残し、生成側に**
   テストを置いた。守るべきなのは表示側ではなく表を作る `aggregate/tables.py` のほう。
3. 全角幅 … `persona_sim/textwidth.py::display_width()` を新設し、`cli.py` の私的コピーを
   差し替え、`progress.py::_write()` の `ljust` をこれで置き換えた。**幅を測る場所を1つに寄せる**
   のが目的で、`cli.py` から複製すると同じずれが2箇所に残る。
4. `_try_json_object()` … `json.JSONDecoder().raw_decode()` で先頭から1つ読む。
   非貪欲 `.*?` は入れ子の `{}` を途中で切るので採らなかった。

### L8. ルールを実態に合わせた（案A）

`AGENTS.md` の「カタログ名・スキーマ名は書かない」を「**コードに**書かない」と限定し、
サービングエンドポイント名と同じ形の例外条項を足した——デプロイ設定（`app.yaml` / Bundle）に
配置先を書くのは可、どのカタログに書いたのかが読み取れないと結果を辿れないため。
環境変数の値を**入れる**側であって、コードから読む側ではないので上の規則と両立する。

`app.yaml` は挙動を変えていない。コメントが例外条項を指す文言になっていなかったので直した。

## 追加したテスト

| テスト | 何を固定するか |
|---|---|
| `test_quotas.py::test_proportion_allocation_does_not_hand_out_extras_when_proportions_exceed_one` | 比率合計が1超でも余りを配らない（L1） |
| `test_survey_loader.py::test_removed_question_scale_points_is_rejected_with_migration_hint` | 廃止キーが理由つきで止まる（L2） |
| `test_survey_loader.py::test_omitted_model_and_screening_defaults_match_the_dataclass_defaults` | 既定値がずれたら落ちる（L3） |
| `test_results_page.py::test_the_page_does_not_build_the_workbook_on_every_rerun` | ページが直接 xlsx を組み立てない（L5） |
| `test_export.py::test_every_table_row_has_one_value_per_column` | 全 `Table` で行長＝列数（L7-2） |
| `test_parsing.py`（2件） | 先頭の JSON を読む・入れ子を切らない（L7-4） |

`test_validate.py:140` の `scale_points: 5` は取り除いた。

**修正前のコードで落ちることを確認したもの**: L1・L2（`scale_points`）・L7-4 の先頭 JSON。
L3 の既定値テストは**値がずれたとき**にだけ落ちる網で、同じ値の二重定義そのものは
検出できない（そこは所見にも書いた）。

## 検証

```
$ /tmp/ci-venv/bin/python -m pytest tests/ -q -m "not spark"
597 passed, 82 deselected

$ ruff check .
All checks passed!

$ /tmp/ci-venv/bin/python -c "load_survey('examples/survey_sample.yaml') / _smoke.yaml"
cs_2026_0801_rtd_a / smoke_test_rtd     # 破壊的変更の実測: 既存の調査定義は読める
```

全角幅の詰めは `_ConsoleReporter._write()` を直接叩いて確認した。
幅27の行のあとに幅4の行を書くと空白23個で消える（`ljust` では11個しか出ず、
消し残りが出ていた）。

### CI（`b70fcef`）

| ジョブ | 結果 |
|---|---|
| `test`（非 Spark） | ✅ success |
| `Spark integration tests` | ✅ success — **82 passed, 597 deselected**（15分52秒） |

手元では Java が無く実行できなかった Spark 経路（L1 の `quotas.allocate_cell_sizes()` と
L2 の `screening`）を含め、全件通過している。

## 未対応事項

- **L9（型チェッカ導入）・L10（冪等トークン）・R1〜R3 は未着手。** `/pdca` に引き継ぐ
- **M3 の多重再試行はほどいていない**（前サイクルからの持ち越し）
- `run/prompt.py::follow_up_message` と `InferResult.codes` / `ScreeningVerdict.codes` は
  上記の理由で意図的に残した
- **Databricks 実環境での確認は未実施**（既存の history と同じ）
