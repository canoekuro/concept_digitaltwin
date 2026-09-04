"""セッションの並列実行と E5 の中断（`persona_sim.run.executor`）。

Spark も実エンドポイントも使わない。`run_session` が触る `SessionContext` の代わりに、
セッションを直接引数に取る偽の実行関数を差し込んで、**中断したときに何を残すか**を見る。
"""

from __future__ import annotations

import threading

from persona_sim.run import executor as executor_module
from persona_sim.run.executor import ContinuousFailureDetector, execute
from persona_sim.run.session import ResponseRecord, Session, Unit


def _session(name: str) -> Session:
    return Session(
        persona_uuid=name,
        units=(Unit(persona_uuid=name, stimulus_id="c1", question_id="q1", sequence=1),),
    )


def _record(name: str) -> ResponseRecord:
    return ResponseRecord(
        survey_id="s1",
        persona_uuid=name,
        stimulus_id="c1",
        question_id="q1",
        sequence=1,
        answer_raw="1",
        answer_codes=[1],
        answer_text=None,
        answer_reasoning=None,
        options_order=[1],
        flags=[],
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        attempt=1,
    )


class _Runner:
    """`run_session` の差し替え。最初の N 件を失敗させ、残りは成功させる。

    成功側は「中断が決まった後もまだ走っている」状況を作るため、
    合図を受け取るまで待たせる。
    """

    def __init__(self, *, fail: set[str], release: threading.Event) -> None:
        self._fail = fail
        self._release = release
        self.started: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, session: Session, ctx) -> list[ResponseRecord]:
        with self._lock:
            self.started.append(session.persona_uuid)
        if session.persona_uuid in self._fail:
            raise RuntimeError("エンドポイント障害")
        # 中断が決まるまで走り続けているセッションを模す。
        self._release.wait(timeout=5)
        return [_record(session.persona_uuid)]


def test_records_of_sessions_still_running_at_abort_are_kept(monkeypatch):
    """中断時に走っていたセッションの記録を捨てない。

    `cancel()` はまだ始まっていない future にしか効かない。走っているぶんを回収しないと、
    `shutdown(wait=True)` の裏で走り切って**呼び出しの費用だけ払って記録が残らない**。
    再実行でもう一度同じ呼び出しをすることになる。
    """
    release = threading.Event()
    runner = _Runner(fail={"u1"}, release=release)
    monkeypatch.setattr(executor_module, "run_session", runner)

    sessions = [_session(f"u{i}") for i in range(1, 5)]
    # 1件失敗したら即中断。u1 が失敗した時点で u2〜u4 は走り出している。
    detector = ContinuousFailureDetector(consecutive_limit=1)

    # 中断判定より後に成功側を終わらせる（回収されるかを見たいので、先に終わらせない）。
    threading.Timer(0.2, release.set).start()
    result = execute(sessions, ctx=None, concurrency=4, detector=detector)

    assert result.aborted, "1件連続失敗で中断すること"
    # 走り出していたセッションの記録が結果に入っていること。
    recorded = {record.persona_uuid for record in result.records}
    started_ok = set(runner.started) - {"u1"}
    assert recorded == started_ok, "走り切ったセッションの記録を捨てないこと"
    assert result.sessions_ok == len(recorded)


def test_a_clean_run_collects_every_session(monkeypatch):
    """中断しない経路が壊れていないこと（回帰）。"""
    release = threading.Event()
    release.set()
    runner = _Runner(fail=set(), release=release)
    monkeypatch.setattr(executor_module, "run_session", runner)

    sessions = [_session(f"u{i}") for i in range(1, 4)]
    result = execute(sessions, ctx=None, concurrency=2)

    assert not result.aborted
    assert result.sessions_ok == 3
    assert {record.persona_uuid for record in result.records} == {"u1", "u2", "u3"}
