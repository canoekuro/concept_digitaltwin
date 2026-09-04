# 仕様書とコードの整合化および `image_mode: native` 実装結果

- **日付**: 2026/07/28 20:34
- **テーマ**: `spec-code-alignment`
- **計画**: [`docs/history/20260728-202800-spec-code-alignment-plan.md`](docs/history/20260728-202800-spec-code-alignment-plan.md)

## 概要

`SPEC.md`（親仕様）、`SPEC_PHASE1.md`（フェーズ1仕様）、および `persona_sim` コードベースの乖離を解消し、方針決定に従って仕様書の改訂および `image_mode: native`（マルチモーダル提示）の追加実装を完了しました。

## 実施内容

1. **仕様書 (`SPEC_PHASE1.md`) の更新**:
   - `azure_ai_foundry`: フェーズ1対象外（Databricks / Fake のみ）であることを明記。
   - `estimated_cost`: 環境依存のため `null` 固定（トークン数 `tokens` を記録）と明記。
   - コード側での仕様拡張（`screener.infer` 設定パラメータ、`responses.answer_codes` への一本化、`run_metadata.json` 拡張項目）を仕様書に反映。
2. **仕様書 (`SPEC.md`) の更新**:
   - `SPEC_PHASE1.md` が優先である旨の明確化。
   - `job_id` / `job:` から `survey_id` / `survey:` への用語統合および旧設計記述への注記追記。
3. **`persona_sim` コード実装の追加 (`image_mode: native`)**:
   - `persona_sim/llm/client.py`: `ChatMessage` の `content` フィールドの型を `str | list[dict[str, Any]]` に拡張。
   - `persona_sim/run/prompt.py` & `session.py` & `metadata.py`: `build_user_message` で `stimulus.image_mode is ImageMode.NATIVE` かつ `stimulus.image_uri` がある場合、`content` を `list[dict[str, Any]]`（テキストパート＋ `{"type": "image_url", "image_url": {"url": stimulus.image_uri}}` パート）として構築するように改修。
   - `persona_sim/llm/databricks.py`: `DatabricksServingClient.complete` で `m.content` が `str` または `list` のいずれでも OpenAI 互換 JSON ペイロードに乗るよう保護。
   - `persona_sim/llm/fake.py`: `FakeLLMClient` で `m.content` が `list`（マルチモーダルパーツ）の場合でもテキスト抽出を行ってダミー回答を返せるよう修正。
   - `persona_sim/panel/validate.py`: `_check_unimplemented` から `image_mode: native` の未実装エラーを削除し、`image_uri` 未指定時エラーチェックを強化。
4. **テスト**:
   - `tests/test_prompt.py`, `tests/test_validate.py`, `tests/test_session.py`, `tests/test_databricks_client.py` にテストケースを追加。
   - 全 305 件の単体テスト（非Spark）がすべてパスすることを確認済み。

## 検証結果

`uv run --with pytest ... pytest -m "not spark"` にて 305 passed, 63 deselected で全テストが合格。
