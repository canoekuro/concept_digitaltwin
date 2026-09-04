# Databricks Apps のソースをリポジトリ直下にしてホイールを不要にする（結果）

- **日付**: 2026/08/04 13:41
- **計画**: [20260804-134146-app-source-root-drop-wheel-plan.md](20260804-134146-app-source-root-drop-wheel-plan.md)

## 1. 何を変えたか

Databricks Apps のソースコードパスを `app/` からリポジトリ直下に移した。`persona_sim/` は
1バイトも動かしていない。

「`persona_sim` を `app/` の中に移せないか」という相談から始まったが、調べた結果、
ホイールが要る原因は配置ではなく Apps のソースコードパスの取り方だった。Apps は指定した
ソースディレクトリ配下だけを同期し、そのルートの `requirements.txt` を install する。
ソースが `app/` だと、その外にある `persona_sim/` に届かないので Volume 上のホイールで
埋める必要があった。ソースを1階層上げれば `persona_sim/` も同期対象に入り、ホイールは要らない。

`persona_sim/` を `app/` 配下へ移す案は採らなかった。`persona_sim` は CLI・ノートブック・
ジョブから使う本体で、`app/` はその上に乗る薄い UI（`app/lib/__init__.py` に「ロジックは
置かない」と明記されている）。本体を UI の下に入れると依存の向きが構造から読めなくなり、
さらにジョブ側のホイールは消えないまま `pyproject.toml` / pytest / ドキュメント全体の
書き換えだけが発生する。

あわせて `notebooks/run_survey.ipynb` にも `sys.path` ブートストラップを入れ、ジョブ側からも
ホイールを外した。これでホイールを作る作業は運用から完全に無くなった。

## 2. 変更したファイル

| ファイル | 変更内容 |
|---|---|
| `app/app.yaml` → `app.yaml` | リポジトリ直下へ移動。`command` を `['streamlit', 'run', 'app/app.py']` に。この場所である理由を冒頭コメントに明記 |
| `requirements.txt` | Apps がインストールするアプリ依存に置き換え（旧 `app/requirements.txt` からホイール行を除いたもの） |
| `requirements-pipeline.txt` | 新規。旧 `requirements.txt` の内容（pyspark / delta-spark ほか） |
| `app/requirements.txt` | 削除。ソースルートが移ったので Apps は読まない |
| `.github/workflows/ci.yml` | Spark 結合テストの依存を `requirements-pipeline.txt` へ |
| `app/lib/context.py` | `DEFAULT_UI_CONFIG_PATH` を新設し、`ui_config()` の既定を cwd 相対からモジュール相対へ |
| `app/lib/__init__.py` | docstring からホイール前提の記述を削除 |
| `notebooks/run_survey.ipynb` | 先頭に `sys.path` ブートストラップのセルを追加（`quickstart.ipynb` と同じ書き方） |
| `deploy/README.md` | §1 から artifacts Volume を削除、§2 をホイール作成から Git フォルダ配置へ、§3・§4 を更新 |
| `docs/SPEC_UI.md` §6.3 | ソースコードパスの前提と依存ファイルの分割を反映 |
| `README.md` | 開発環境の `pip install` を `requirements-pipeline.txt` に。依存3ファイルの対応表を追加 |
| `CHANGELOG.md` | 1行追記 |

`pyproject.toml` は変更していない。`python -m build --wheel` は従来どおり通り、`pip install -e .`
での開発も変わらない。ホイールを**運用で使わなくなった**だけで、作れなくしてはいない。

## 3. 検証結果

| 確認 | 結果 |
|---|---|
| `ruff check .` | All checks passed |
| `./scripts/run-tests.sh`（非 Spark） | 470 passed, 70 deselected |
| `app.yaml` の YAML 妥当性 | `command` / `env` とも期待どおり読める |
| `notebooks/run_survey.ipynb` の JSON 妥当性 | 読み込める。差分は44行の追加のみ（既存セルの整形崩れ無し） |
| cwd = リポジトリ直下での設定解決 | `DEFAULT_UI_CONFIG_PATH` が `app/config/ui_config.yaml` を指し、`ui_config()` が `UIConfig` を返す |
| 同条件での `persona_sim` の解決先 | インストール無しの環境で `/home/user/research_system/persona_sim/__init__.py`（＝リポジトリ内のソース） |
| `streamlit run app/app.py` の起動 | ポート 8899 で HTTP 200 |
| アプリ本体の実行（`AppTest`、cwd = リポジトリ直下） | 例外なし・警告なし。調査設計ページの5セクションが描画される |

`AppTest` は `streamlit run` と違って起動スクリプトのディレクトリを `sys.path` に入れないため、
検証時のみ `sys.path.insert(0, "app")` で実挙動に合わせている。

## 4. 未対応

- **実環境（Databricks Apps / ジョブ）での疎通確認は未実施。** `deploy/README.md` の手順は
  更新したが、実際にソースコードパスを付け替えたアプリの再デプロイと、Git フォルダの
  ノートブックをジョブとして走らせる確認は残っている。特に次の2点は実環境でしか確かめられない。
  - Apps がリポジトリ直下を同期したときに `persona_sim/data/*.csv` まで含まれること
  - ジョブのクラスタで、Databricks Runtime に含まれない依存（`huggingface_hub` など）を
    どこまで明示的に入れる必要があるか
- `README.md` が参照する `requirements-notebook.txt` がリポジトリに存在しない。この変更の前から
  ある不整合（履歴 #4 では作成したことになっている）で、範囲外として手を付けていない。
