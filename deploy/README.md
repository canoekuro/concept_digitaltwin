# デプロイ手順（Web UI）

コンセプト調査 Web UI（`app/`）を Databricks Apps に載せるまでの手順。
仕様は `docs/SPEC_UI.md`。

## 1. 用意するもの

| 資源 | 用途 |
|---|---|
| Unity Catalog のカタログ・スキーマ | `personas_base` / `panels` / `responses` / `screener_responses` / `runs` |
| Volume（例 `surveys`） | 調査定義 YAML の置き場所 |
| SQL Warehouse | 結果閲覧ページの読み取り |
| ジョブ | 調査の実行 |

## 2. リポジトリを Git フォルダに置く

**ホイールは作りません。** アプリもジョブもリポジトリ内の `persona_sim` を直接読みます。

ワークスペースに Databricks Git フォルダとしてこのリポジトリを追加し、デプロイしたい
ブランチに合わせてください。以降の手順はこの Git フォルダを指します。

更新するときは Git フォルダを pull し、アプリを再デプロイするだけです。バージョンを
上げてビルドし直す作業はありません。

## 3. ジョブを作る

調査定義の実行は `notebooks/run_survey.ipynb` に1本化してあります。
アプリからは `run_now` で起動するだけなので、クラスタ構成・再実行・権限は
このジョブ定義側で管理できます。

- ジョブとパイプライン→ジョブ→noteboookをジョブとして登録。（`notebook_path: notebooks/run_survey.ipynb`）
  **Git フォルダ内のノートブックを指すこと。** リポジトリ直下が `sys.path` に入り、
  `persona_sim` がライブラリのインストール無しで import できます
- **クラスタのライブラリ**: `requirements-pipeline.txt`（`pyspark` / `delta-spark` ほか）。
  Databricks Runtime に含まれないもの（`huggingface_hub` など）だけ入れれば足ります
- **ジョブパラメータ**: `survey`（アプリが調査定義のパスを入れる。）`PERSONA_SIM_CATALOG` / `PERSONA_SIM_SCHEMA`（テーブルのあるカタログとスキーマを指定）

## 4. アプリを作る
**リポジトリ直下**（`app.yaml` と `requirements.txt` のある場所）をソースコードパスにして
アプリを作成し、以下のリソースを3つ紐づけます。`app/` ではありません — `app/` を指すと
その外にある `persona_sim` が同期されず、ホイールが必要になります。

| リソースキー | 種別 | 権限 | 注入される値 |
|---|---|---|---|
| `sql-warehouse` | SQL Warehouse | `CAN USE` | ウェアハウス **ID** |
| `persona-sim-job` | ジョブ | `CAN MANAGE RUN` | ジョブ ID |
| `survey-volume` | Volume | 読み書き | `/Volumes/<catalog>/<schema>/surveys` |

### `app.yaml` のカタログ・スキーマ

`PERSONA_SIM_CATALOG` / `PERSONA_SIM_SCHEMA` は `app.yaml` に直接書く値で、テーブルを
どこに置いたかの記録を兼ねます（`AGENTS.md` の例外条項）。`research_system` から
このリポジトリへ切り出した時点の値は移行前のカタログのままなので、**デプロイ先の
カタログ・スキーマに合わせて書き換えてください**。同じ値を §3 のジョブパラメータにも
入れます（アプリとジョブが別のカタログを見ると、投入した調査の結果が結果閲覧に出ません）。

### アプリのサービスプリンシパルに要る権限
アプリのサービスプリンシパルに、対象テーブルのあるスキーマへの
`USE SCHEMA`/`SELECT`（結果閲覧は読み取りのみ）/`READ VOLUME` / `WRITE VOLUME`（調査定義を置くため）
を設定

## 5. 疎通確認（初回は1つずつ）

1. アプリを開き、**調査設計ページ**が表示されること
   （設定が足りなければ、何が足りないか画面に出ます）
2. 調査名とコンセプトを入れ、**見積もりが表示される**こと
   — ここまでは Databricks に接続しません
3. 「調査を開始する」を押し、Volumes に YAML ができ、ジョブの実行が始まること
4. ジョブが完走し、`runs` に1行入ること
5. **結果閲覧ページ**に調査が出て、「データを取得」で集計が表示されること

3 で失敗する場合は Volume の書き込み権限とジョブの `CAN MANAGE RUN` を、
5 で失敗する場合は SQL Warehouse の `CAN USE` とテーブルの `SELECT` を確認してください。

## 6. ノートブック単体の動作確認

アプリを操作せずに `notebooks/run_survey.ipynb` / ジョブ単体だけが最後まで通るかを
確かめたいときの手順。UI 側の投入経路（`persona_sim/uiconfig/jobs.py`、`tests/test_jobs.py`
で fake クライアントによりテスト済み）とは独立に確認できる。

1. `examples/survey_sample_smoke.yaml`（`model.endpoint: fake` の小規模サンプル）を
   Volume（`survey-volume` で指定した場所、例 `/Volumes/<catalog>/<schema>/surveys/`）に置く。
   Catalog Explorer からのアップロードや `databricks fs cp` を使う
2. 次のどちらかで起動する（どちらもアプリの「調査を開始する」ボタンは使わない）
   - クラスタにアタッチして `run_survey.ipynb` を開き、`survey` ウィジェットに置いた
     パスを入力して全セル実行する。クラスタに `PERSONA_SIM_CATALOG` / `PERSONA_SIM_SCHEMA`
     （または `PERSONA_SIM_WAREHOUSE`）が未設定なら、`catalog` / `schema` / `warehouse`
     ウィジェットにも値を入れる
   - §3 の事前定義ジョブに対して Databricks Jobs UI の「今すぐ実行」（または
     `databricks jobs run-now --job-id <ID> --job-parameters survey=/Volumes/.../survey_sample_smoke.yaml`）
     で `job_parameters` を直接入力して起動する
3. ジョブが完走し、`runs` に1行入ることを確認する

ここが失敗する場合、原因は §5 の3〜4と同じ（Volume の書き込み・読み取り権限、クラスタの
テーブル配置設定）だが、アプリを経由していないぶん切り分けがしやすい。

## 7. 未対応

- **国勢調査による割り付け**は、参照テーブル（`persona_sim/data/census_population_by_sex_age5.csv`）が
  未投入のため選べません。投入手順は `persona_sim/data/README.md`
- **見積もりの実績値**（`config/ui_config.yaml` の `ui.estimation_benchmarks`）は仮の値です。
  小規模な実走で実測値に置き換えてください。特に `screener_session` は根拠がありません
- **実行中の進捗表示**はありません。完走した調査が結果閲覧ページの一覧に出てきます
