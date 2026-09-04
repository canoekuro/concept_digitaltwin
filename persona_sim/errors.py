"""例外定義。エラー番号は `SPEC.md` §11 に対応する。

E3・E4（パース失敗率・拒否率の超過）は「止めずに続行し、必ず表面化させる」ため
例外ではなく警告として扱う（M3 以降で実装）。
"""


class PersonaSimError(Exception):
    """本パッケージが送出する例外の基底。"""

    code: str = ""


class SurveyDefinitionError(PersonaSimError):
    """調査定義そのものが読めない・構造が不正（§3）。"""

    code = "SURVEY"


class InsufficientCandidatesError(PersonaSimError):
    """E1: セルの候補ペルソナが必要数に満たない。

    セル間の重複除外を適用した後の候補数で判定する（§4.1）。
    """

    code = "E1"


class ScreenerShortfallError(PersonaSimError):
    """E2: スクリーニング後、通過者が必要数に満たない（M4 で使用）。"""

    code = "E2"


class EndpointFailureError(PersonaSimError):
    """E5: エンドポイントの継続的な失敗（M3 以降で使用）。"""

    code = "E5"

