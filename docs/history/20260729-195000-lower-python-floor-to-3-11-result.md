# wheel の Python 要件を 3.11 に緩和 結果

- **日付**: 2026/07/29 19:50
- **テーマ**: `lower-python-floor-to-3-11`

## やったこと

PR #10 マージ後、Databricks Apps へのデプロイで
`ERROR: Package 'persona-sim' requires a different Python: 3.11.15 not in '>=3.12'`
が発生した。`pyproject.toml` の `requires-python` が実行環境の実態（Databricks Apps /
ジョブが動く Python 3.11）と食い違っていたことが原因だったため、これを是正した。

1. `pyproject.toml`: `requires-python` を `>=3.12` → `>=3.11` に、ruff の
   `target-version` を `py311` に変更（`UP` ルールが 3.12 専用構文への書き換えを
   提案しないようにするため）。
2. `.github/workflows/ci.yml`: `test` / `spark-test` 両ジョブの `python-version` を
   `3.11` に変更。実行環境と乖離した Python でテストしても、その乖離自体には
   気づけないため。
3. `SPEC.md` §13: 技術スタック表の「Python 3.12」を「Python 3.11」に修正し、
   Databricks 実行環境に合わせている旨の備考を追加。
4. `README.md` / `notebooks/quickstart.ipynb`: ローカル開発の前提記述を
   `Python 3.11 以上` に統一。
5. `uv.lock`: `requires-python` の変更に伴い再解決（cp311 wheel の追加）。

## 検証結果

| 項目 | 結果 |
|---|---|
| `python -m build --wheel` → `dist-info/METADATA` の `Requires-Python` | `>=3.11` になっていることを確認 |
| `./scripts/run-tests.sh`（非Spark） | 391 passed |
| `-m spark` | 63 passed（762秒） |
| `ruff check .` | All checks passed |

`grep -rn "3\.12"` で残るのは `.venv`（ローカル開発用の仮想環境で、実行環境の Python
バージョンとは無関係）と過去の `docs/history/` 記録のみであることを確認した。

## 補足

コード自体に 3.12 専用構文（PEP 695 の `type` 文・ジェネリクス構文など）は使っておらず、
`enum.StrEnum` も 3.11 から利用可能。3.11 の AST パーサで全ソースが構文エラー無く読める
ことも確認済みで、実質的な非互換は無かった。
