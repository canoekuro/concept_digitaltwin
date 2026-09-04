# 仕様書とコードの整合化および `image_mode: native` 実装計画

- **日付**: 2026/07/28 20:28
- **テーマ**: `spec-code-alignment`

## 目的

`SPEC.md`（親仕様）、`SPEC_PHASE1.md`（フェーズ1仕様）、および `persona_sim` コードベースの乖離を解消し、方針決定に従って仕様書とコードを補正・実装する。

## 変更内容

1. **`SPEC_PHASE1.md` の更新**:
   - `azure_ai_foundry`: フェーズ1対象外（Databricks / Fake のみ）であることを明記。
   - `estimated_cost`: 環境依存のため `null` 固定（トークン数 `tokens` を記録）と明記。
   - コード側での仕様拡張（`screener.infer` 設定パラメータ、`answer_codes` 一本化、`run_metadata.json` 拡張項目）を仕様書に反映。
2. **`SPEC.md` の更新**:
   - `SPEC_PHASE1.md` が優先である旨の明確化。
   - `job_id` / `job:` から `survey_id` / `survey:` への用語統合および旧設計記述への注記追記。
3. **`persona_sim` コード実装の追加**:
   - `image_mode: native`: マルチモーダル提示（画像を直接モデルに渡す）をコード側で対応（`ChatMessage` およびプロンプト組み立て、`DatabricksServingClient` / `FakeLLMClient` のマルチモーダルペイロード展開、`validate.py` での未実装エラー解除）。
4. **テスト・検証**:
   - `pytest` による全テストの実行。
   - `image_mode: native` をテストする unit test の追加と完了確認。
