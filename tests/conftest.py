"""テスト共通の調査定義ひな形。

各テストは `base_survey_dict()` を書き換えて「1箇所だけ壊れた定義」を作る。
ひな形自体は検証を通過する状態に保つこと。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

_BASE: dict[str, Any] = {
    "survey": {"id": "survey_test", "name": "テスト調査"},
    "panel": {
        "size": 4,
        "seed": 42,
        "quotas": {
            "mode": "count",
            "cells": [
                {"cell_id": "M_20s", "sex": "男", "age_min": 20, "age_max": 29, "n": 2},
                {"cell_id": "F_20s", "sex": "女", "age_min": 20, "age_max": 29, "n": 2},
            ],
        },
    },
    "stimuli": [
        {"id": "c1", "name": "コンセプトA", "text": "内容A"},
        {"id": "c2", "name": "コンセプトB", "text": "内容B"},
        {"id": "c3", "name": "コンセプトC", "text": "内容C"},
    ],
    "main_survey": {"model": {"endpoint": "fake", "deployment": "test-deployment"}},
    "output": {"segments": ["total"], "formats": ["csv"]},
}


def questions_for(slots: int) -> list[dict[str, Any]]:
    """設問を `slot`（何番目に提示するコンセプトについて聞くか）ごとに展開する。

    設問はコンセプトに繰り返し適用されるテンプレートではないので、1ペルソナが
    評価するコンセプトの数だけ設問が要る。同じ問いをコンセプト横断で比べるための
    キーが `measure`。
    """
    return [
        question
        for slot in range(1, slots + 1)
        for question in (
            {
                "id": f"q_intent_{slot}",
                "slot": slot,
                "measure": "q_intent",
                "text": "購入したいと思いますか。",
                "type": "single",
                "options": ["ぜひ", "やや", "どちらとも", "あまり", "まったく"],
                "top_box": [1, 2],
            },
            {
                "id": f"q_reason_{slot}",
                "slot": slot,
                "measure": "q_reason",
                "text": "理由は。",
                "type": "open",
                "max_length": 100,
            },
        )
    ]


def with_remember(data: dict[str, Any], shape: str) -> dict[str, Any]:
    """かつて `design.memory` の3値で表していた記憶の持ち方を、設問ごとに書く。

    設定そのものは廃止したが、**セッションの粒度がこの3つの形で変わらないこと**は
    引き続き固定したいので、テストからはこの関数を通して同じ形を作る。

    - `none`: 何も書かない（既定）
    - `within_stimulus`: 同じ slot の先行設問のIDを並べる
    - `full_session`: 全設問に `remember: all`
    """
    questions = data["questions"]
    if shape == "none":
        return data
    if shape == "full_session":
        for question in questions:
            question["remember"] = "all"
        return data
    if shape == "within_stimulus":
        for index, question in enumerate(questions):
            preceding = [q["id"] for q in questions[:index] if q["slot"] == question["slot"]]
            question["remember"] = preceding or "none"
        return data
    raise ValueError(f"未知の形: {shape}")


def base_survey_dict(slots: int = 3) -> dict[str, Any]:
    """検証を通過する最小の調査定義（毎回新しいコピーを返す）。

    `slots` はコンセプト数。提示設計は反実仮想モナディック固定で、全ペルソナが
    全コンセプトを評価するため、**コンセプト数と slot の数は必ず一致する**
    （`slot` の抜けと範囲外は読み込みの時点で止まる）。だから両方をここで揃える。
    """
    data = copy.deepcopy(_BASE)
    data["stimuli"] = copy.deepcopy(_BASE["stimuli"][:slots])
    data["questions"] = questions_for(slots)
    return data


@pytest.fixture
def survey_dict() -> dict[str, Any]:
    return base_survey_dict()


@pytest.fixture(scope="session")
def spark():
    """`-m spark` のテストが共有する SparkSession。

    pyspark の import は fixture の中で行う。Spark 抜きの CI 調査では
    この fixture が呼ばれないため、pyspark が無くてもテスト収集が壊れない。
    """
    from persona_sim.spark import get_spark

    session = get_spark("persona-sim-tests")
    session.sparkContext.setLogLevel("ERROR")
    return session
