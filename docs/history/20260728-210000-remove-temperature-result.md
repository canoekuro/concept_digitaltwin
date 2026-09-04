# `temperature` パラメータ廃止 結果

- **日付**: 2026/07/28 21:20
- **テーマ**: `remove-temperature`
- **計画**: [`docs/history/20260728-210000-remove-temperature-plan.md`](20260728-210000-remove-temperature-plan.md)

## 概要

`temperature` の指定をコード・調査定義・実行メタデータ・仕様書から全面的に削除した。
サンプリングの設定はエンドポイント側の既定に委ねる。

**調査定義の破壊的変更を含む。** `model.temperature` / `infer.model.temperature` が
書かれた既存の調査定義は、廃止理由を添えたエラーで停止する。既存 Delta テーブルの
作り直しは不要（`runs` テーブルは実行メタデータ全体を JSON 文字列1列で持つため、
スキーマは不変）。

## 実施内容

1. **推論クライアント層**
   - `persona_sim/llm/client.py`: `LLMClient.complete()` の契約から `temperature` 引数を削除。
     docstring に「サンプリングパラメータは渡さない」方針を明記。
   - `persona_sim/llm/databricks.py`: シグネチャと、サービングエンドポイントへ送る `payload` から削除。
     送信するのは `messages` / `max_tokens`（+ 任意で `response_format`）のみになった。
   - `persona_sim/llm/fake.py`: シグネチャから削除（値は元々未使用）。
2. **調査定義スキーマ**
   - `persona_sim/panel/schema.py`: `ModelConfig.temperature`（既定 0.0）と
     `InferModelOverrides.temperature` を削除。`InferModelOverrides.apply()` は `vars(self)` を
     回す実装なので無改修。廃止理由を持つ `REMOVED_MODEL_FIELDS` を新設した。
3. **YAML ローダー**
   - `persona_sim/panel/loader.py`: `_model()` / `_infer_model()` の許可キーから外し、
     `_reject_removed_model_fields()` を `_reject_unknown` の**手前**で呼ぶ。
     手前に置かないと「未知のキー temperature」という無内容なメッセージが先に出て、
     打ち間違えたのか廃止されたのかが読み取れない。
4. **実行エンジン**
   - `persona_sim/run/session.py`: `RETRY_TEMPERATURE = 1.0` 定数を削除。`_ask()` のリトライから
     昇温を外し、同じ入力の再送に変更した。3回リトライ自体は維持（`responses.attempt` の
     記録・E3/E4 の閾値運用がこの回数に依存しているため）。
   - `persona_sim/panel/infer.py`: `infer` スクリーニングの判定呼び出しから削除。
5. **実行メタデータ**
   - `persona_sim/metadata.py`: `model` ブロック（§9）と `screener_infer.model` ブロックの
     2箇所から、送っていないパラメータとしてキーごと削除。
6. **調査定義サンプル・ノートブック**
   - `examples/survey_sample.yaml`、`notebooks/quickstart.ipynb`（実行される定義 dict、
     Databricks 接続例の markdown、コメントアウトされた参考 dict の計3箇所）。
7. **仕様書**
   - `SPEC_PHASE1.md`: バージョン 1.0 → 1.1。§3 の YAML 例、§6.3（リトライ項目 + 方針の段落を追加）、
     §9 の `run_metadata.json` 例、§9.1 再現性の範囲、§13 実装上の注意。
   - `SPEC.md`: §8.2 実行フローの「open設問: サンプリング生成（temperature 0.8）」、
     §17 リスク R4 の対応方針。
   - `AGENTS.md`: 「実装上の不変条件」節（`SPEC_PHASE1.md` §13 の写し）に同じ1行を追加し同期。
8. **テスト**
   - `tests/test_infer.py`: 「未記入キーは本体から引き継ぐ」の検証役を `temperature` から
     `concurrency` へ差し替え。`_model()` ヘルパが既定で `concurrency=1` を渡しており、
     `ModelConfig` の素の既定（32）と違う値なので継承を明確に示せる。
   - `tests/test_databricks_client.py`: 呼び出しから削除し、`test_multimodal_user_message_payload` に
     `assert "temperature" not in sent_body` を追加。payload に混ぜ戻さないことの回帰固定。
   - `tests/test_survey_loader.py`: `model.temperature` と `infer.model.temperature` の両方が
     廃止理由つきで停止することを2件追加。

## 検証結果

1. **非Spark**: `./scripts/run-tests.sh` → **307 passed, 63 deselected**。
2. **Spark**: `pytest -m spark` → **63 passed, 307 deselected**（12分57秒）。
   `test_run_spark.py::test_run_metadata_is_written` を含み、`run_metadata.json` の
   生成経路を端から端まで通っている。
3. `examples/survey_sample.yaml` がパースを通ることを確認。
   `ModelConfig` のフィールドは `endpoint` / `deployment` / `thinking` / `max_tokens` /
   `max_tokens_open` / `concurrency` / `structured_output` の7つになった。
4. `temperature: 0.0` を書き戻した定義が、次のメッセージで停止することを確認:
   > `model.temperature: temperature は廃止した。最新モデルではサンプリングパラメータの指定が非推奨・無効化される傾向にあるため、リクエストに載せずエンドポイント既定に任せる（§6.3）。この行を削除すること。`
5. `grep -rni temperature` で残るのは、過去記録（下記）・廃止ヒント文言・仕様の方針記述・
   テストのみであることを確認。

## 副次的に解消した乖離

`SPEC_PHASE1.md` §6.3 は「temperature を 0.0 → 0.3 に上げる」と書いていたが、実装
（`session.py` の `RETRY_TEMPERATURE`）は **1.0** だった。仕様は段階的な昇温を記述し、
実装は 2回目以降を固定値にしていた点も食い違っていた。今回の削除でこの乖離ごと消えた。

## 未対応事項

- **リトライは同じ入力の再送になった。** エンドポイントが既定で決定論的に応答する場合、
  2回目・3回目は1回目と同じ結果になり、費用だけがかかる。入力側を変える案
  （読み取れなかった回答を履歴に置いて形式の再指示を1ターン足す）を検討したが、
  プロンプト仕様・`prompt.rules` の拡張・`memory: full_session` の履歴汚染回避を伴い、
  今回の「temperature をやめる」という趣旨を超えるため見送った。別件として扱う。
- `docs/issues/202607281100.md` と `docs/history/20260727-023000-m3-m6-run-engine-plan.md` は
  **書き換えていない**。当時の観測・計画の記録であり、書き換えると履歴が信用できなくなるため。
  前者が指摘する「`thinking` がエンドポイントに届いていない」問題自体は未解決のまま残る
  （payload の記述のうち `temperature` の部分だけが本改修で古くなった）。
- `SPEC.md` §8.2 の logprobs 取得はフェーズ1では未実装のままで、今回も着手していない。
