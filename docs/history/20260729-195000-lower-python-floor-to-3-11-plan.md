# wheel の Python 要件を 3.11 に緩和 計画

- **日付**: 2026/07/29 19:50
- **テーマ**: `lower-python-floor-to-3-11`

## 経緯

PR #10 マージ後、Databricks Apps へのデプロイでエラーが発生した。

```
ERROR: Package 'persona-sim' requires a different Python: 3.11.15 not in '>=3.12'
```

`pyproject.toml` の `requires-python = ">=3.12"` は、リポジトリ立ち上げ時に `SPEC.md` §13
の技術スタック表（将来の API 実装向けの記述）に合わせて一律で設定したもの。一方
Databricks Apps のコンテナは Python 3.11.15 で動いており、`persona-sim` は純粋な
Python wheel（`py3-none-any`）で 3.12 専用の構文も使っていない
（`enum.StrEnum` は 3.11 から利用可能。3.11 の AST パーサで全ソースが構文エラー無く読めることを確認済み）。
つまり pip が拒否していたのはメタデータ上の宣言だけが原因で、実態に合っていなかった。

## 方針

`requires-python` を実行環境の実態（Databricks Apps / ジョブが動く Python 3.11）に
合わせて `>=3.11` に緩める。あわせて、宣言と実挙動の食い違いを再発させないよう
CI のテスト対象 Python も 3.11 に揃え、`SPEC.md` §13 の記述も実態に合わせて修正する
（ユーザー承認済み）。

## 対象ファイル

- `pyproject.toml`: `requires-python = ">=3.11"`、ruff `target-version = "py311"`
  （`UP` ルールが 3.12 専用構文への書き換えを提案しないように揃える）
- `.github/workflows/ci.yml`: `test` / `spark-test` 両ジョブの `python-version` を `3.11` に
- `SPEC.md` §13: 技術スタック表の「Python 3.12」を「Python 3.11」に修正し、
  Databricks 実行環境に合わせている旨の備考を追加
- `README.md` / `notebooks/quickstart.ipynb`: ローカル開発の前提記述を `Python 3.11 以上` に統一
- `uv.lock`: `requires-python` 変更に伴う再解決（cp311 wheel の追加）

## 検証方法

1. `python -m build --wheel` でホイールを作り、`dist-info/METADATA` の
   `Requires-Python` が `>=3.11` になっていることを確認
2. `./scripts/run-tests.sh`（非Spark）が全件パスすること
3. `-m spark` のテストが全件パスすること
4. `ruff check .` が通ること
5. `grep -rn "3\.12"` で、残るのが `.venv` 配下（ローカル開発用の仮想環境。実行環境の
   Python バージョンとは無関係）と過去の `docs/history/` 記録だけであること
