# `temperature` パラメータ廃止 計画

- **日付**: 2026/07/28 21:00
- **テーマ**: `remove-temperature`

## 目的

最新の推論モデルでは `temperature` の指定が非推奨・無効化される傾向にある。指定しても
実際には効かないまま「設定したつもり」の値だけが調査定義と `run_metadata.json` に残り、
実挙動と記録が食い違う。これは `AGENTS.md` の不変条件「黙って劣化した経路に落ちない」に
反する状態である。

そこで `temperature` の指定を全面的にやめ、サンプリングはエンドポイント側の既定に委ねる。
コード・調査定義・実行メタデータ・仕様書まで一貫させる。

## 方針

- 既存の調査定義 YAML に `temperature:` があれば**理由つきのエラーで停止**する。
  黙って無視すると「指定したつもり」がそのまま `survey_definition` に記録されるため。
  既存の `REMOVED_DESIGN_FIELDS`（`design.type` の廃止）と同じ機構を再利用する。
- 仕様書は `SPEC_PHASE1.md`（実装の直接の根拠）と `SPEC.md`(親仕様) の**両方**を改訂する。
- `run_metadata.json` の `model` ブロックからは**キーごと削除**する（`null` で残さない）。
  送っていないパラメータを記録しない。

## 対象ファイル と 変更内容

1. **推論クライアント層** — `complete()` の契約から外す
   - `persona_sim/llm/client.py`: `LLMClient.complete()` の `temperature` 引数を削除。docstring に方針を明記。
   - `persona_sim/llm/databricks.py`: シグネチャと、サービングエンドポイントへ送る `payload` から削除。
   - `persona_sim/llm/fake.py`: シグネチャから削除（値は元々未使用）。
2. **調査定義スキーマ**
   - `persona_sim/panel/schema.py`: `ModelConfig.temperature` と `InferModelOverrides.temperature` を削除。
     廃止理由を持つ `REMOVED_MODEL_FIELDS` を新設。
3. **YAML ローダー**
   - `persona_sim/panel/loader.py`: `_model()` / `_infer_model()` の許可キーから外し、
     `_reject_removed_model_fields()` で廃止理由つきに停止させる。
4. **実行エンジン**
   - `persona_sim/run/session.py`: `RETRY_TEMPERATURE` 定数を削除。`_ask()` のリトライから昇温を外す。
     3回リトライ自体は残す（エンドポイント既定のサンプリングは非決定的なため）。
   - `persona_sim/panel/infer.py`: 判定呼び出しから削除。
5. **実行メタデータ**
   - `persona_sim/metadata.py`: `model` ブロックと判定モデルブロックの2箇所から削除。
6. **調査定義サンプル・ノートブック**
   - `examples/survey_sample.yaml`、`notebooks/quickstart.ipynb`（3箇所）。
7. **仕様書**
   - `SPEC_PHASE1.md`: バージョン 1.0 → 1.1。§3 の YAML 例、§6.3 リトライ、§9 の
     `run_metadata.json` 例、§9.1 再現性、§13 実装上の注意。
   - `SPEC.md`: §8.2 実行フロー、§17 リスク R4。
   - `AGENTS.md`: 「実装上の不変条件」節（`SPEC_PHASE1.md` §13 の写し）に同じ1行を追加して同期を保つ。
   - `docs/issues/` と `docs/history/` の過去記録は**書き換えない**（当時の観測の保存であるため）。
8. **テスト**
   - `tests/test_infer.py`: 「未記入キーは本体から引き継ぐ」の検証役を `temperature` から `concurrency` へ差し替え。
   - `tests/test_databricks_client.py`: 呼び出しから削除し、送信ペイロードに `temperature` が
     載らないことを回帰として固定する。
   - `tests/test_survey_loader.py`: `model.temperature` / `infer.model.temperature` の
     両方が廃止理由つきで停止することを追加。

## 検証方法

1. `./scripts/run-tests.sh`（非Spark）が全件パスすること。
2. `-m spark` のテストが全件パスすること（`run_metadata.json` 生成経路を通る）。
3. `examples/survey_sample.yaml` がパースを通ること。
4. `temperature: 0.0` を書き戻した定義が、廃止理由を含むエラーで停止すること。
5. `grep -rni temperature` で、残るのが過去記録・廃止ヒント文言・仕様の方針記述だけであること。
