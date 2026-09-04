# 実装計画: M1 `personas_base` / M2 ジョブ定義・validate・panel

- **日時**: 2026/07/27 01:30
- **対象**: `SPEC_PHASE1.md` §12 の M1・M2
- **状態**: 承認済み（実装着手）

## 目的

リポジトリには仕様書・エージェント実行環境・CI があるだけで、実装コードが1行も無い状態だった。
「ジョブ定義 YAML を渡すと、割り付けどおりのパネルが seed 再現で出る」ところまでを通す。

```
job.yaml → persona-sim validate → persona-sim panel → panels（Delta）＋ パネル構成サマリ
```

M3（回答生成）以降は対象外。

## 決定事項

| 項目 | 決定 |
|---|---|
| スコープ | M1＋M2 のみ。LLM 呼び出しは含めない |
| 実行基盤 | Delta Lake（PySpark 4.0.1 + delta-spark 4.3.1）。ローカル parquet 実装は作らない |
| ベースデータ | HF から1シャード（125,000 行）を実際に取得して検証 |
| 提示設計 | `design.type` を**廃止**し、**3軸**に置き換える |
| 既定の提示設計 | `same` × `sequential` × `none`（反実仮想モナディック） |
| 同一コンセプトの重複回答 | 禁止（不変条件）。3軸はコンセプト**間**の関係のみを扱う |

## 設計判断

### 1. 提示設計を3軸に直交分解する（仕様改訂を伴う）

従来の `design.type`（monadic / sequential_monadic / comparative）は独立した3つの選択が
束になったもので、**LLM でしか取れない組み合わせを表現できない**。実査では「同じ人に、
前回の記憶なしで、別のコンセプトを聞く」ことが原理的に不可能なため、既存の調査語彙に
その選択肢が無いことに起因する。

| 軸 | フィールド | 値（**太字**が既定） |
|---|---|---|
| 誰が何を見るか | `sample_overlap` | `disjoint` / `allow_overlap` / **`same`** |
| どう見せるか | `presentation` | **`sequential`** / `simultaneous` |
| 覚えているか | `memory` | **`none`** / `within_stimulus` / `full_session` |

第1軸は 1ペルソナが評価するコンセプト数 m の連続体（K を総数として disjoint=1 /
allow_overlap=2〜K-1 / same=K）。

`design.type` はプリセットとしても残さない。2通りの書き方が生まれ、どちらが優先かという
無意味な規則が要るため。書かれていたら3軸での書き方を示して停止する。
`balance_within_cell` も廃止（不変条件を設定で無効化できてはいけない）。

### 2. 同一ペルソナは同一コンセプトに1回しか回答しない

パネル内で `persona_uuid` を一意にし、`assigned_stimuli` に重複を含めない。
この結果 `responses` の複合キー（§2.3）と冪等 upsert（§6.4）は仕様のまま成立するので、
テーブル定義の変更は不要。

セル条件が重なると同一 uuid が複数セルに入りうるため、記述順に処理して確定済み uuid を
anti-join で除外する処理を常時適用する（設定で無効化しない）。

### 3. 抽出は乱数ではなく決定論的ハッシュ順

`DataFrame.sample(seed=…)` はパーティション数・並列度で結果が変わり、
「seed と各バージョンが揃えば同一結果が再現できること」を満たせない。

```
rank_key = sha2(uuid | seed | cell_id, 256)  の昇順で先頭から必要数
```

### 4. 割り当ては K 通りしかないことを利用する

窓の起点は `cell_rank % K` だけで決まるため、割り当て表は K 行の純関数として作り、
Spark 側は結合するだけにする。Spark 抜きで割り当ての正しさを検証できる。

## 対象ファイル

- 仕様: `SPEC_PHASE1.md`（0.6 → 0.7）、`AGENTS.md`（不変条件）、`README.md`
- 調査結果: `docs/schema/occupation-parsing.md`（新規）
- 実装: `persona_sim/`（新規パッケージ。`cli` / `config` / `spark` / `errors` /
  `storage/{locator,delta}` / `personas/{source,normalize,build}` /
  `panel/{schema,loader,validate,quotas,sampling,assignment,build}`）
- 基盤: `pyproject.toml`、`requirements.txt`（新規）、`.github/workflows/ci.yml`
- サンプル: `examples/job_sample.yaml`（新規）
- テスト: `tests/`（Spark 不要の単体テスト＋`-m spark` の結合テスト）

## 検証方法

- 単体テスト（Spark 不要）: 3軸の整合（E6）・不変条件・割り当ての均等性と重複なし・
  整数配分・occupation 分解・年代バンド
- Spark 結合テスト: **seed 再現性**、**並列度を変えても結果が変わらないこと**、
  セル重複時も uuid が一意、`disjoint` の評価者数が均等、E1 停止、`panels` の列と冪等性
- 参照実装（純 Python）と Spark 式が一致することを突き合わせる
- 実データ1シャードで `personas build` → `validate` → `panel` を通し、2回実行して同一結果になることを確認

## 記録

本ファイルと対になる result を `docs/history/` に保存し、`CHANGELOG.md` に1行追記する。
