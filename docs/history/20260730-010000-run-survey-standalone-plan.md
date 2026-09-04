# `run_survey.ipynb` のアプリ非依存化と単体テスト対応（計画）

## 背景・目的

`notebooks/run_survey.ipynb` は本番ジョブ用の notebook で、これまで Streamlit アプリ
（`app/`）の「調査を開始する」ボタン →
`persona_sim/uiconfig/jobs.py:submit_survey()` → `client.jobs.run_now(job_parameters=...)`
という経路でのみ起動されてきた。この notebook を Databricks 上でアプリを経由せず単体で
起動・検証できるようにし、UI 側（`tests/test_jobs.py` で確認済み）と notebook 側を
別々にテストできるようにしたい。あわせて、Volumes に置いて試すための調査定義サンプルを
用意する。

調査の結果、`run_survey.ipynb` は `persona_sim.*` のみを import しており `app/` への
直接依存は無い。実質的な結合点は次の2点だった。

1. cell-1 のエラー文言「アプリ側が渡すはずのパスが来ていない」が、あたかもアプリ経由
   でしか起動できないかのような誤解を招く。実際には Databricks Jobs の「今すぐ実行」で
   `job_parameters` を手入力しても同じ仕組みで渡る。
2. `storage_config()`（`persona_sim/config.py`）が `PERSONA_SIM_CATALOG` /
   `PERSONA_SIM_SCHEMA` / `PERSONA_SIM_WAREHOUSE` を環境変数からのみ解決するため、
   ジョブクラスタ側に事前設定が無いと単体実行時に例外で落ちる
   （`deploy/README.md` §3 の手動設定に依存しており、実環境での動作確認は未実施と
   `docs/history/20260729-140000-...-result.md` に記載がある）。

## 変更内容

既存のアプリ→ジョブ投入経路（`job_parameters={"survey": ...}`）は変えず、後方互換を保つ。

1. **`notebooks/run_survey.ipynb`**
   - cell-0（markdown）に「アプリを経由しない単体実行」の手順を追記
     （クラスタアタッチしてウィジェットに直接値を入れる方法、
     Databricks Jobs UI の「今すぐ実行」で `job_parameters` を直接入力する方法）
   - cell-1 に `catalog` / `schema` / `warehouse` の任意ウィジェットを追加し、
     値があればその場で対応する環境変数を上書きしてから `storage_config()` を呼べるようにする
     （ジョブクラスタに事前設定が無くても単体実行できるようにするため）
   - `survey` が空のときのエラー文言をアプリ限定を前提としない表現に修正
2. **`examples/survey_sample_smoke.yaml`（新規）**
   - Volumes に置いて `run_survey.ipynb` を単体で素早く・無料で通すための最小サンプル
     （`model.endpoint: fake`、`panel.size: 10`、`screener.mode: assume`）
3. **`deploy/README.md`**
   - 既存の §5（疎通確認、アプリ経由）と §7（未対応）の間に §6「ノートブック単体の動作確認」
     を新設し、サンプルの配置とアプリを経由しない起動手順を記載
4. **テスト**
   - `tests/test_run_survey_notebook.py`（新規）: `tests/test_notebook.py` と同じ構成で
     notebook の構文健全性・コミット禁止項目（outputs/execution_count）・エラー文言の
     アプリ限定表現が復活していないことを確認
   - `tests/test_survey_loader.py`: `examples/survey_sample_smoke.yaml` が読め
     `validate_static()` を通ることを確認するテストを追加
5. **記録**
   - 本 plan/result ペアを `docs/history/` に保存
   - `CHANGELOG.md` に履歴一覧の新規エントリを追記

## 検証方法

- `python -m json.tool notebooks/run_survey.ipynb` で notebook JSON の健全性を確認
- `python -c "import yaml; yaml.safe_load(open('examples/survey_sample_smoke.yaml'))"` で
  YAML のパース確認
- `./scripts/run-tests.sh tests/test_run_survey_notebook.py tests/test_survey_loader.py -v`
  で新規・既存テストがグリーンであることを確認
- Databricks 上での実機確認（クラスタアタッチ、または事前定義ジョブの「今すぐ実行」）は
  実行環境が無いため本セッションでは実施できない。既存の `deploy/README.md` にも
  同様の「実環境未検証」の注記が既にある
