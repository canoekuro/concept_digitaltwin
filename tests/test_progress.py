"""進捗表示（`persona_sim.run.progress`）。

実時間にもエンドポイントにも依存させない。`clock` を注入して間引きと残り時間を検証する。
"""

from __future__ import annotations

import io

from persona_sim.llm.client import RetryStats
from persona_sim.run.progress import ProgressUpdate, console_reporter


class _Clock:
    """呼ばれるたびに手で進める時計。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _StubClient:
    """`retry_stats()` だけを持つクライアント。"""

    def __init__(self, stats: RetryStats) -> None:
        self._stats = stats

    def retry_stats(self) -> RetryStats:
        return self._stats


def _reporter(out, clock, **kwargs):
    return console_reporter(out=out, clock=clock, **kwargs)


# --------------------------------------------------------------------------- #
# ProgressUpdate
# --------------------------------------------------------------------------- #


def test_ratio_is_bounded():
    assert ProgressUpdate(done=0, total=8).ratio == 0.0
    assert ProgressUpdate(done=2, total=8).ratio == 0.25
    # 単位の数え違いで 100% を超えて見えないようにする。
    assert ProgressUpdate(done=9, total=8).ratio == 1.0


def test_empty_run_is_finished():
    """やることが0件なら完了扱い。0除算で落ちない。"""
    update = ProgressUpdate(done=0, total=0)
    assert update.ratio == 1.0
    assert update.finished


# --------------------------------------------------------------------------- #
# 間引き
# --------------------------------------------------------------------------- #


def test_first_report_is_always_written():
    out, clock = io.StringIO(), _Clock()
    _reporter(out, clock, interval_seconds=30)(ProgressUpdate(done=1, total=100))
    assert "1/100" in out.getvalue()


def test_reports_within_the_interval_are_dropped():
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=30)

    report(ProgressUpdate(done=1, total=100))
    clock.now = 10.0
    report(ProgressUpdate(done=2, total=100))
    clock.now = 29.9
    report(ProgressUpdate(done=3, total=100))

    assert out.getvalue().count("\n") == 1


def test_reports_resume_after_the_interval():
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=30)

    report(ProgressUpdate(done=1, total=100))
    clock.now = 30.0
    report(ProgressUpdate(done=2, total=100))

    assert "2/100" in out.getvalue()


def test_completion_is_never_throttled():
    """最後の1行が出ないと、完走したのか止まったのか分からない。"""
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=30)

    report(ProgressUpdate(done=1, total=2))
    clock.now = 0.5  # 間引きの窓の中
    report(ProgressUpdate(done=2, total=2))

    assert "2/2" in out.getvalue()


def test_completion_is_written_only_once():
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=0)

    report(ProgressUpdate(done=2, total=2))
    report(ProgressUpdate(done=2, total=2))

    assert out.getvalue().count("2/2") == 1


# --------------------------------------------------------------------------- #
# 中身
# --------------------------------------------------------------------------- #


def test_elapsed_and_eta_come_from_the_measured_rate():
    """見積もりではなく実測から出す。遅くなればそのぶん残り時間も伸びる。"""
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=0)

    clock.now = 60.0  # 60秒で10件 → 残り90件は540秒（09:00）
    report(ProgressUpdate(done=10, total=100))

    line = out.getvalue()
    assert "経過 01:00" in line
    assert "残り 約09:00" in line
    assert "10.0 件/分" in line


def test_long_durations_switch_to_hours():
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=0)

    clock.now = 3725.0
    report(ProgressUpdate(done=1, total=2))

    assert "経過 1:02:05" in out.getvalue()


def test_eta_is_omitted_before_the_first_completion():
    """0件のうちは割り算できない。出せない数字を出さない。"""
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=0)

    clock.now = 10.0
    report(ProgressUpdate(done=0, total=100))

    assert "残り" not in out.getvalue()


def test_failures_are_shown():
    out, clock = io.StringIO(), _Clock()
    _reporter(out, clock, interval_seconds=0)(ProgressUpdate(done=10, total=100, failed=3))
    assert "失敗 3" in out.getvalue()


# --------------------------------------------------------------------------- #
# 再試行の表示
# --------------------------------------------------------------------------- #


def test_retries_are_shown_when_a_client_is_given():
    out, clock = io.StringIO(), _Clock()
    client = _StubClient(RetryStats(calls=40, retried_calls=9, retries=12, backoff_seconds=92.4))
    report = _reporter(out, clock, interval_seconds=0, client=client)

    clock.now = 10.0
    report(ProgressUpdate(done=10, total=100))

    assert "再試行 12回・待機 92秒" in out.getvalue()


def test_retries_are_hidden_while_the_endpoint_is_healthy():
    """平常時に毎行「再試行 0回」が並ぶと、本当に増えたときに気づけない。"""
    out, clock = io.StringIO(), _Clock()
    client = _StubClient(RetryStats(calls=40))
    report = _reporter(out, clock, interval_seconds=0, client=client)

    clock.now = 10.0
    report(ProgressUpdate(done=10, total=100))

    assert "再試行" not in out.getvalue()


def test_a_client_without_retry_stats_is_ignored():
    """`complete` しか持たないクライアントでも進捗表示は動く。"""
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=0, client=object())

    clock.now = 10.0
    report(ProgressUpdate(done=10, total=100))

    assert "10/100" in out.getvalue()


# --------------------------------------------------------------------------- #
# 出力先による切り替え
# --------------------------------------------------------------------------- #


def test_a_terminal_overwrites_one_line():
    out, clock = _Tty(), _Clock()
    report = _reporter(out, clock, interval_seconds=0)

    report(ProgressUpdate(done=1, total=100))
    clock.now = 1.0
    report(ProgressUpdate(done=2, total=100))

    written = out.getvalue()
    assert written.startswith("\r")
    assert written.count("\n") == 0  # 完了前は改行しない


def test_a_terminal_ends_the_line_when_finished():
    out, clock = _Tty(), _Clock()
    _reporter(out, clock, interval_seconds=0)(ProgressUpdate(done=2, total=2))
    assert out.getvalue().endswith("\n")


def test_a_notebook_stream_appends_lines():
    """Databricks のセル出力では `\\r` の上書きを期待できない。"""
    out, clock = io.StringIO(), _Clock()
    report = _reporter(out, clock, interval_seconds=0)

    report(ProgressUpdate(done=1, total=100))
    clock.now = 1.0
    report(ProgressUpdate(done=2, total=100))

    written = out.getvalue()
    assert "\r" not in written
    assert written.count("\n") == 2


def test_terminal_overwrite_pads_away_the_previous_line():
    """上書きで短い行に変わったとき、前の行の残骸が右側に残らないこと。

    `\\r` は桁を消さないので、短い行をそのまま書くと前の行の末尾が見えたままになる。
    """
    out, clock = _Tty(), _Clock()
    report = console_reporter(out=out, clock=clock, interval_seconds=0)

    # 1行目: 残り時間とスループットが載る長い行。
    clock.now = 60.0
    report(ProgressUpdate(done=10, total=100, failed=0))
    # 2行目: 完了行。残り時間が消えるぶん短くなる。
    clock.now = 61.0
    report(ProgressUpdate(done=100, total=100, failed=0))

    first, second = out.getvalue().split("\r")[1:3]
    assert "残り" in first
    assert "残り" not in second
    assert len(second.rstrip("\n")) >= len(first)


# --------------------------------------------------------------------------- #
# 実行系が渡す形
#
# 本調査（executor）とスクリーナー infer（`test_infer.py`）の双方が同じ形を渡すこと。
# 揃っていないと、受け手が経路を知らないと書けなくなる。
# --------------------------------------------------------------------------- #


def test_execute_reports_progress_as_updates(monkeypatch):
    from persona_sim.run import executor

    monkeypatch.setattr(executor, "run_session", lambda session, ctx: [])

    seen: list[ProgressUpdate] = []
    executor.execute(["s1", "s2", "s3"], ctx=None, concurrency=1, progress=seen.append)

    assert [(u.done, u.total, u.failed) for u in seen] == [(1, 3, 0), (2, 3, 0), (3, 3, 0)]
    assert seen[-1].finished


def test_execute_counts_failed_sessions_in_the_update(monkeypatch):
    from persona_sim.run import executor

    def boom(session, ctx):
        raise RuntimeError("エンドポイント障害")

    monkeypatch.setattr(executor, "run_session", boom)

    seen: list[ProgressUpdate] = []
    executor.execute(["s1", "s2"], ctx=None, concurrency=1, progress=seen.append)

    assert [(u.done, u.total, u.failed) for u in seen] == [(1, 2, 1), (2, 2, 2)]
