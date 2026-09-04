# `occupation` の分解規則

`SPEC_PHASE1.md` §2.1 の `occupation_industry` / `occupation_scale` / `occupation_role` /
`employment_status` を、Nemotron-Personas-Japan の `occupation` から導出するための規則。

**推測ではなく実データから決めた。** 検証対象は
`nvidia/Nemotron-Personas-Japan` revision `f1f37019d8497143c507b3deb547e65646de2ab7` の
`data/train-00000-of-00008.parquet`（125,000 行）。

## 構造

```
occupation = "{業種} [ {規模}] [ 経営] [ (現在は引退)|(現在は離職)]"
```

末尾から「就業状態 → 役職 → 規模」の順に既知の語を剥がすと、**残りは必ず1トークン**になり、
それが業種にあたる。125,000 行すべてでこの性質が成り立つことを確認済み
（残余トークン数の分布は `1` が 125,000 件、それ以外なし）。

## 語彙（実データの全量）

| 要素 | 値 | 出現数 |
|---|---|---|
| 規模 `occupation_scale` | `中堅` / `中小` / `大手` | 47,040 / 36,144 / 33,250 |
| （規模なし） | null | 8,566 |
| 役職 `occupation_role` | `経営` | 4,684 |
| （役職なし） | null | 120,316 |
| 就業状態 `employment_status` | `(現在は引退)` → `引退` | 31,211 |
| | `(現在は離職)` → `離職` | 1,541 |
| | 記載なし → `就業中` | 92,248 |

業種 `occupation_industry` は 103 種類。上位は `介護福祉業` `小売業` `卸売業` `建設業` など。

規模を持たない業種は `地方公務員` `農業` `学生` `国家公務員` `漁業` `林業` の6種類のみ。
`学生` は業種として扱う（`employment_status` は `就業中` になる）。実データ上、
就学状態を表す独立した語は存在しない。

## 実装

- 参照実装（純 Python）: `persona_sim/personas/normalize.py` の `parse_occupation()`
- Spark 実行時の式: `persona_sim/personas/build.py`

同じ語彙定数（`normalize.py`）を両方が参照する。両実装が一致することは
Spark テスト（`-m spark`）で突き合わせて検証する。乖離をテストで検出できるようにするため、
語彙をどちらか一方に書き足さないこと。

## 年代バンド

| カラム | 規則 | 例 |
|---|---|---|
| `age_band_5` | `floor(age/5)*5` を下限とする5歳刻み | 18 → `15-19`、23 → `20-24` |
| `age_band_10` | `floor(age/10)*10` の年代表記 | 23 → `20代`、67 → `60代` |

実データの `age` は 18〜100。100 以上は両バンドとも `100歳以上` にまとめる
（`100代` という表記を出さないため）。
