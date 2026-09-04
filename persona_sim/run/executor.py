"""セッションの並列実行と、エンドポイント継続失敗の検知（`SPEC_PHASE1.md` §6.5, §11 E5）。

セッション内は逐次、セッション間は並列。`model.concurrency` でワーカ数を決める。
"""

from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from persona_sim.errors import EndpointFailureError
from persona_sim.run.progress import ProgressUpdate
from persona_sim.run.session import ResponseRecord, Session, SessionContext, run_session

#: E5: この回数だけ連続で失敗したら中断する。
CONSECUTIVE_FAILURE_LIMIT = 20

#: E5: 直近この件数のうち失敗率が閾値を超えたら中断する。
FAILURE_WINDOW = 100
FAILURE_RATE_LIMIT = 0.5


class ContinuousFailureDetector:
    """E5 の判定。スレッドから同時に呼ばれる。"""

    def __init__(
        self,
        *,
        consecutive_limit: int = CONSECUTIVE_FAILURE_LIMIT,
        window: int = FAILURE_WINDOW,
        rate_limit: float = FAILURE_RATE_LIMIT,
    ) -> None:
        self._consecutive_limit = consecutive_limit
        self._window: deque[bool] = deque(maxlen=window)
        self._window_size = window
        self._rate_limit = rate_limit
        self._consecutive = 0
        self._lock = threading.Lock()

    def record(self, *, failed: bool) -> str | None:
        """1セッション分の結果を記録し、中断すべきなら理由を返す。"""
        with self._lock:
            self._consecutive = self._consecutive + 1 if failed else 0
            self._window.append(failed)

            if self._consecutive >= self._consecutive_limit:
                return f"{self._consecutive} セッション連続で失敗した"

            if len(self._window) >= self._window_size:
                rate = sum(self._window) / len(self._window)
                if rate > self._rate_limit:
                    return f"直近 {len(self._window)} セッションの失敗率が {rate:.0%}"
        return None


@dataclass
class ExecutionResult:
    records: list[ResponseRecord] = field(default_factory=list)
    sessions_total: int = 0
    sessions_ok: int = 0
    sessions_failed: int = 0
    #: 中断した場合の理由（E5）。完走したら None。
    aborted_reason: str | None = None
    #: セッションごとの最初の例外（先頭数件だけ残す。ログを溢れさせないため）。
    errors: list[str] = field(default_factory=list)

    @property
    def aborted(self) -> bool:
        return self.aborted_reason is not None

    @property
    def input_tokens(self) -> int:
        """この実行で使った入力トークン。1レコード＝1呼び出しなので合算でよい。"""
        return sum(record.input_tokens or 0 for record in self.records)

    @property
    def output_tokens(self) -> int:
        return sum(record.output_tokens or 0 for record in self.records)


def execute(
    sessions: list[Session],
    ctx: SessionContext,
    *,
    concurrency: int,
    detector: ContinuousFailureDetector | None = None,
    progress=None,
    runner=None,
) -> ExecutionResult:
    """セッションを並列に実行する。

    E5 を検知したら未着手のセッションを取り消し、**完了分を保存できる形で返す**。
    例外にせず結果に載せるのは、途中まで書き出してから再開できるようにするため。

    `runner` は「1単位を実行してレコードの並びを返す」呼び出し可能なもの。既定は
    コンセプト調査のセッション実行。単位が違う実行（スクリーニングの一括判定など）も
    **並列度の決め方・完了分の回収・E5 の判定は同じであるべき**なので、`runner` を
    差し替えて同じ経路を通す。単位ごとに別々に実装すると、片方だけ「中断時に走っている
    呼び出しを捨てる」実装になり、費用を払って記録が残らない。

    **既定値を `run_session` にせず、ここで解決している。** 既定引数はモジュールの
    読み込み時に束縛されるので、`monkeypatch.setattr(executor, "run_session", …)` で
    差し替えたテストが効かなくなる。
    """
    result = ExecutionResult(sessions_total=len(sessions))
    if not sessions:
        return result

    run = runner or run_session
    detector = detector or ContinuousFailureDetector()
    workers = max(1, min(concurrency, len(sessions)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, session, ctx): session for session in sessions}
        pending = set(futures)

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            # 完了済みのぶんは中断が決まっていても取りこぼさない。
            # ここで捨てると、書き出せたはずのセッションを再実行する羽目になる。
            for future in done:
                _collect(future, result, detector)
                if progress is not None:
                    progress(
                        ProgressUpdate(
                            done=result.sessions_ok + result.sessions_failed,
                            total=result.sessions_total,
                            failed=result.sessions_failed,
                        )
                    )

            if result.aborted:
                # `cancel()` はまだ始まっていない future にしか効かない。**走っているぶんは
                # 待って回収する。** 捨てると `shutdown(wait=True)` の裏で走り切り、
                # 呼び出しの費用だけ払って記録が残らない（再実行でもう一度呼ぶことになる）。
                # 上の「完了済みのぶんは取りこぼさない」と同じ理由で、待ち時間より費用を採る。
                still_running = [future for future in pending if not future.cancel()]
                for future in wait(still_running).done:
                    _collect(future, result, detector)
                pending = set()

    return result


def _collect(future, result: ExecutionResult, detector: ContinuousFailureDetector) -> None:
    try:
        result.records.extend(future.result())
        result.sessions_ok += 1
        failed = False
    except Exception as exc:  # セッション単位で失敗を吸収し、調査全体は止めない
        result.sessions_failed += 1
        if len(result.errors) < 10:
            result.errors.append(f"{type(exc).__name__}: {exc}")
        failed = True

    reason = detector.record(failed=failed)
    if reason and not result.aborted:
        result.aborted_reason = reason


def raise_if_unrecoverable(result: ExecutionResult) -> None:
    """E5 を例外にする。完了分の保存が済んだ後に呼ぶこと。"""
    if result.aborted:
        raise EndpointFailureError(
            f"エンドポイントの継続的な失敗により中断した（{result.aborted_reason}）。"
            f"完了した {result.sessions_ok} セッション分は保存済み。"
            "同じコマンドで再実行すれば続きから再開する"
        )
