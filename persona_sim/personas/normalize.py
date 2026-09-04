"""`personas_base` の派生列を作るための純 Python 実装（`SPEC.md` §2.1）。

**このモジュールは pyspark に依存しない。** 分解規則そのものを Spark 抜きで
単体テストできるようにするため。Spark 実行時は `persona_sim.personas.build` が
ここで定義した語彙定数から同じ規則の Spark 式を組み立て、両者が一致することを
Spark テストで突き合わせる。

分解規則の根拠と実データでの検証結果は `docs/schema/occupation-parsing.md`。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 事業規模。`occupation` の末尾側から2番目に現れる。
SCALE_TOKENS: tuple[str, ...] = ("大手", "中堅", "中小")

#: 役職。実データに現れるのは「経営」のみ。
ROLE_TOKENS: tuple[str, ...] = ("経営",)

#: 就業状態。原文表記 → 正規化後の値。記載が無ければ「就業中」。
EMPLOYMENT_TOKENS: dict[str, str] = {
    "(現在は引退)": "引退",
    "(現在は離職)": "離職",
}

#: 就業状態の既定値。
EMPLOYMENT_DEFAULT = "就業中"

#: 100 歳以上をまとめる表記。`100代` という表記を出さないため。
AGE_BAND_CENTENARIAN = "100歳以上"


@dataclass(frozen=True)
class Occupation:
    """`occupation` を4要素に分解した結果。"""

    industry: str
    scale: str | None
    role: str | None
    employment_status: str


def parse_occupation(value: str | None) -> Occupation:
    """`occupation` を「業種 / 規模 / 役職 / 就業状態」に分解する。

    末尾から「就業状態 → 役職 → 規模」の順に既知の語を剥がし、残りを業種とする。
    実データ 125,000 行では残りが必ず 1 トークンになることを確認している。
    未知の形式でも落とさず、剥がせなかった部分をそのまま業種として返す。
    """
    if value is None:
        return Occupation(industry="", scale=None, role=None, employment_status=EMPLOYMENT_DEFAULT)

    tokens = value.split()
    employment = EMPLOYMENT_DEFAULT
    role: str | None = None
    scale: str | None = None

    if tokens and tokens[-1] in EMPLOYMENT_TOKENS:
        employment = EMPLOYMENT_TOKENS[tokens.pop()]
    if tokens and tokens[-1] in ROLE_TOKENS:
        role = tokens.pop()
    if tokens and tokens[-1] in SCALE_TOKENS:
        scale = tokens.pop()

    return Occupation(
        industry=" ".join(tokens),
        scale=scale,
        role=role,
        employment_status=employment,
    )


def age_band_5(age: int) -> str:
    """5歳刻みの年代バンド。18 → `15-19`、23 → `20-24`。"""
    if age >= 100:
        return AGE_BAND_CENTENARIAN
    lower = (age // 5) * 5
    return f"{lower}-{lower + 4}"


def age_band_10(age: int) -> str:
    """10歳刻みの年代バンド。23 → `20代`、67 → `60代`。"""
    if age >= 100:
        return AGE_BAND_CENTENARIAN
    return f"{(age // 10) * 10}代"
