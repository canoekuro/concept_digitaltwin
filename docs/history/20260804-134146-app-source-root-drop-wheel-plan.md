# Databricks Apps のソースをリポジトリ直下にしてホイールを不要にする（計画）

- **日付**: 2026/08/04 13:41
- **対象**: `app.yaml` / `requirements.txt` 群 / `app/lib` / `notebooks/run_survey.ipynb` / デプロイ手順

## 1. 目的

`persona_sim` を更新するたびに「版を上げる → `python -m build --wheel` → Volume にアップロード →
`app/requirements.txt` を書き換える」の4手が必要になっている。これを無くす。

原因は配置ではなく **Databricks Apps のソースコードパスの取り方**にある。Apps は指定した
ソースディレクトリ配下だけを同期し、そのルートの `requirements.txt` を pip install する。
現在のソースパスは `app/` なので、その外にある `persona_sim/` には届かない。だから Volume に
置いたホイール（`app/requirements.txt` の最終行）で埋めていた。

ソースパスをリポジトリ直下にすれば `persona_sim/` も同期対象に入り、ホイールは要らなくなる。

## 2. 方針

**`persona_sim/` は動かさない。** `persona_sim` は CLI・ノートブック・ジョブから使う本体で、
`app/` はその上に乗る薄い UI（`app/lib/__init__.py` に「ロジックは置かない」と明記）。
本体を UI の下に移すと依存の向きが構造から読めなくなるうえ、ジョブ側のホイールも消えない。

代わりに Apps のソースパスを1階層上げる。import は `app/lib/__init__.py` が既に持っている
「`persona_sim` が見つからなければリポジトリ直下を `sys.path` に入れる」処理でそのまま通る。
リポジトリ全体は 2.1MB / 212 ファイルなので、まるごと同期しても問題ない。

あわせて `notebooks/run_survey.ipynb` にも `quickstart.ipynb` と同じ `sys.path` ブートストラップを
入れ、ジョブ側からもホイールを外す。これでホイール作業が完全に無くなる。

## 3. 変更対象

| ファイル | 変更内容 |
|---|---|
| `app/app.yaml` → `app.yaml` | リポジトリ直下へ移動。`command` を `['streamlit', 'run', 'app/app.py']` に |
| `requirements.txt` | Apps がインストールする**アプリ依存**にする（旧 `app/requirements.txt` からホイール行を除いたもの） |
| `requirements-pipeline.txt` | 新規。旧 `requirements.txt` の内容（pyspark / delta-spark ほか実行基盤の依存） |
| `app/requirements.txt` | 削除（ソースルートが移るので Apps は読まない） |
| `.github/workflows/ci.yml` | Spark 結合テストの依存を `requirements-pipeline.txt` に切り替え |
| `app/lib/context.py` | `ui_config` の既定パスを cwd 相対からモジュール相対へ（cwd がリポジトリ直下に変わるため） |
| `app/lib/__init__.py` | docstring からホイール前提の記述を外す |
| `notebooks/run_survey.ipynb` | 冒頭に `sys.path` ブートストラップのセルを追加 |
| `deploy/README.md` | §1・§2・§4 を書き換え（ホイールと artifacts Volume の手順を削除） |
| `docs/SPEC_UI.md` §6.3 | デプロイ前提の記述を更新 |
| `README.md` | 開発環境の `pip install` 行を `requirements-pipeline.txt` に |
| `CHANGELOG.md` | 1行追記 |

## 4. 検証方法

- `ruff check .`
- `python -m pytest tests/ -m "not spark"`
- `python -c "import ast, pathlib; ..."` 相当で `app.yaml` の YAML 妥当性を確認
- アプリのエントリを cwd = リポジトリ直下で起動し、`config/ui_config.yaml` が解決できることを
  `lib.context.ui_config()` の単体呼び出しで確認する
- ノートブックの JSON 妥当性を確認する

## 5. 未対応（この変更に含めないこと）

- 実環境（Databricks Apps / ジョブ）での疎通確認。手順は `deploy/README.md` に反映するが、
  実行は未実施のまま
- `README.md` が参照する `requirements-notebook.txt` がリポジトリに存在しない件。
  この変更の前から不整合で、範囲外
