# 設問ごとの記憶保持と、設問の提示スロットへの紐づけ（計画）

- **日付**: 2026/08/08 14:00
- **依頼**: 「調査官の記憶を、設問ごとに保持する／しないを選べるようにできないか検討してほしい」

## 目的

調査官（ペルソナ）の記憶が `design.memory` の3値（`none` / `within_stimulus` /
`full_session`）でしか指定できず、**調査全体に一括で掛かる**。そのため

> Q2 の購入意向を踏まえて Q3 の新規性を聞きたい。ただし次のコンセプト提示には
> その記憶を持ち込みたくない

という設問単位の要求が表現できない。この3区分はコンセプト調査を前提にした切り方で、
通常の調査では「この設問は引き継ぐ／この設問は引き継がない」が設問ごとに変わる。

保持の単位を**設問**まで下ろす。3値はその特殊形として全部表現でき、非連続な参照
（Q4 が Q1 と Q2 だけを覚え、Q3 を飛ばす）も書けるようになる。

## 設計中に判明したこと

参照を設問IDで書く以上、**IDは実行時にも一意でなければならない**。ところが当時の
`questions` は「全コンセプトに繰り返し適用されるテンプレート」で、1ペルソナの回答履歴に
`q_intent` がコンセプト数ぶん現れる。`remember: [q_intent]` がどれを指すのか決まらない。

さらに、IDを一意にすると**コンセプト横断で「同じ問い」を束ねる手段が消える**。
`aggregate/tables.py` のコンセプト比較表は同じ `question_id` を stimulus 横断で並べる
作りなので、ここが割れると「コンセプトAの購入意向」がAを1番目に見た人と2番目に見た人で
別の指標に分かれてしまう。

## 利用者の判断

| 論点 | 判断 |
|---|---|
| 実行時のID重複 | **設問を提示スロットに紐づけて全展開する**（テンプレート方式をやめる） |
| コンセプトまたぎの参照 | 警告を出さない（コンセプト調査以外の拡張もありうるため） |
| 既存の `design.memory` | **残す**。`remember` 未指定の設問の既定を決める略記にする |
| 推移性 | **無し（列挙どおり）**。Q4→Q3、Q3→Q1 でも Q4 は Q1 を見ない |

## 設計

### 設問の全展開（`slot`）と集計キー（`measure`）

設問は「そのペルソナが**何番目に見るコンセプト**について聞くか」を `slot`（1始まり）で持つ。
コンセプトIDではなく提示順の位置なので、`rotation` や `sample_overlap` による割り当ての
違いと直交する（実行時に `assigned_stimuli[slot - 1]` を引く）。

IDが一意になった代償として、束ねるキーを明示する `measure` を足す（省略時は `id` と同じ）。
集計はこちらで畳む。

### 記憶（`remember`）

`none` / `all` / 設問IDのリストの3形。`all` のスパンだけは `design.memory` が決める
（`within_stimulus` なら同じ slot 内、`full_session` なら全設問）。これで既存3値が完全に
再現される。再生は書いた順ではなく ask order（`slot` 昇順・記述順）。

### セッションの粒度

「設問 A が設問 B を覚えている」なら A と B は同じセッションに入る（B の回答が出てからで
ないと A を聞けない）。この連結成分が実行単位であり再開単位。3値との対応は下記で、
**分割は従来と完全に一致する**。

| `design.memory` | できるセッション |
|---|---|
| `none` | 設問ごとに1つ |
| `within_stimulus` | `slot` ごとに1つ |
| `full_session` | ペルソナごとに1つ |

### プロンプトの組み立て

`messages` を伸ばし続ける作りをやめ、**毎ターン組み直す**。伸ばし続ける形では
「直前までの全部」しか表現できない。取っておくのは回答の文面だけでよく、設問文も
選択肢の提示順も純関数から組み直せる。

守る不変条件は2つ。プロフィールは先頭の [user] に1回だけ、コンセプトはその列で初出の
ターンにだけ挟む。

### 既存定義の移行

`slot` の既定 1 で黙って読むと、m > 1 の調査で2つ目以降のコンセプトが聞かれずに消え、
**同じ調査定義で結果の意味が変わる**。`run` は `validate` を通らずに実行できる（`cli.py`）
ので、判定は**ローダー**に置いて読まずに停止させる。

## 変更対象

| ファイル | 変更 |
|---|---|
| `persona_sim/panel/schema.py` | `RememberMode` / `Remember` / `Question.{slot,measure,remember}` / 移行案内 |
| `persona_sim/panel/loader.py` | `slot` / `measure` / `remember` のパース、旧形式の停止 |
| `persona_sim/panel/validate.py` | slot・remember・measure の検証、`W_ROTATION` 条件、セッション数見積 |
| `persona_sim/run/memory.py` | **新規**。記憶の解決とセッション分割（pyspark 非依存の純関数） |
| `persona_sim/run/session.py` | `Unit.remembers`、`build_sessions` の委譲、`run_session` の再構成化 |
| `persona_sim/run/prompt.py` | `replayed_messages()` |
| `persona_sim/aggregate/{crosstab,tables,aggregate,export}.py` | 束ねキーを `measure` に |
| `persona_sim/uiconfig/build.py` | 設問を slot 展開して書き出す |
| `persona_sim/metadata.py` | `questions_memory` |
| `SPEC_PHASE1.md` / `CHANGELOG.md` / `examples/` / `notebooks/` | 仕様・例・ノートブック |

## 検証方針

- 既存3値それぞれで、セッションの分割・件数が変更前と一致すること（回帰）
- 非連続参照・非推移・slot またぎ・プロフィール1回・コンセプト初出のみ提示
- slot 範囲外／slot 抜け／未知ID／自己参照／前方参照／measure 不整合が E6 で止まること
- 旧形式が m > 1 で停止し、m == 1 では通ること
- `./scripts/run-tests.sh`（非 Spark）と Spark テスト

## やらないこと

- 設問単位の再開（再開はセッション単位のまま）
- UI からの記憶指定（`uiconfig/build.py` は反実仮想モナディック固定のまま）
- 記憶を持つ設問と持たない設問が混ざる場合の prefix cache 最適化
