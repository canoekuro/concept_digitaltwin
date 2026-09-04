"""実行中の進捗表示（`SPEC_PHASE1.md` §6.5）。

**進捗の1報の形をここで1つに決める。** 実行系は3つある（本調査、スクリーナーの `ask`、
スクリーナーの `infer`）が、呼び出し側から見ると「いくつ中いくつ終わったか」しか要らない。
形が経路ごとに違うと、受け手が経路を知らないと書けなくなる（実際、`infer` だけ整数を
渡していたため CLI の進捗表示が `infer` モードで落ちていた）。

表示側を `console_reporter` に集約するのは、**間引きと残り時間の計算を1箇所に置く**ため。
1件あたり数十秒かかる状態では、件数で間引くと更新が数分に1回になり、生存確認の役に立たない。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from persona_sim.llm.client import SupportsRetryStats
from persona_sim.textwidth import display_width

#: 既定の表示間隔（秒）。エンドポイントが詰まっていても「動いている」ことが分かる程度。
DEFAULT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class ProgressUpdate:
    """進捗の1報。本調査・スクリーナーのどちらからも同じ形で飛ぶ。

    単位は経路によって違う（本調査はセッション、`infer` はバッチ）。数える対象が
    違うだけで、受け手のやることは変わらないので型は分けない。
    """

    done: int
    total: int
    failed: int = 0

    @property
    def ratio(self) -> float:
        """0.0〜1.0。`total` が 0 なら 1.0（やることが無い＝完了）。"""
        if self.total <= 0:
            return 1.0
        return min(1.0, self.done / self.total)

    @property
    def finished(self) -> bool:
        return self.done >= self.total


def console_reporter(
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    client: object | None = None,
    out=None,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[ProgressUpdate], None]:
    """コンソール／ノートブックに進捗を出すコールバックを作る。

    `client` を渡すと、再試行の状況を行末に足す（`SupportsRetryStats` を満たす場合のみ）。
    エンドポイントが詰まっているのか単に量が多いのかは、**実行中に**分からないと
    打つ手が決められないため。

    `clock` を差し替えられるのはテストのため。実時間に依存すると間引きの検証ができない。
    """
    return _ConsoleReporter(
        interval_seconds=interval_seconds,
        client=client,
        out=out,
        clock=clock,
    )


class _ConsoleReporter:
    """時間で間引いて1行出す。

    `execute()` の待ち合わせループ（単一スレッド）から呼ばれる前提なので、
    状態にロックは掛けない。
    """

    def __init__(self, *, interval_seconds: float, client, out, clock) -> None:
        self._interval = max(0.0, interval_seconds)
        self._client = client if isinstance(client, SupportsRetryStats) else None
        self._out = out
        self._clock = clock
        self._started = clock()
        self._last_emit: float | None = None
        self._last_width = 0
        self._done = False

    def __call__(self, update: ProgressUpdate) -> None:
        now = self._clock()
        # 完了は間引かない。最後の1行が出ないと「途中で止まった」ようにしか見えない。
        if not update.finished:
            if self._last_emit is not None and now - self._last_emit < self._interval:
                return
        elif self._done:
            return  # 完了行は1回だけ

        self._last_emit = now
        self._done = update.finished
        self._write(self._line(update, elapsed=now - self._started))

    # ----------------------------------------------------------------- #

    def _line(self, update: ProgressUpdate, *, elapsed: float) -> str:
        parts = [
            f"  {update.done:,}/{update.total:,}（{update.ratio:.0%}）",
            f"経過 {_duration(elapsed)}",
        ]

        # 実測スループットから見た残り。見積もり値ではなく実測から出すので、
        # エンドポイントが遅くなっていればそのぶん残り時間も伸びる。
        rate = update.done / elapsed if elapsed > 0 and update.done else 0.0
        if rate > 0 and not update.finished:
            parts.append(f"残り 約{_duration((update.total - update.done) / rate)}")
        if rate > 0:
            parts.append(f"{rate * 60:,.1f} 件/分")

        parts.append(f"失敗 {update.failed:,}")

        retries = self._retry_part()
        if retries:
            parts.append(retries)

        return " · ".join(parts)

    def _retry_part(self) -> str | None:
        """再試行の状況。詰まっていなければ何も出さない（平常時の行を短く保つ）。"""
        if self._client is None:
            return None
        stats = self._client.retry_stats()
        if not stats.retries:
            return None
        return f"再試行 {stats.retries:,}回・待機 {stats.backoff_seconds:,.0f}秒"

    def _write(self, line: str) -> None:
        stream = self._out if self._out is not None else sys.stdout

        # 端末なら上書きして1行に収める。ノートブックやジョブログでは `\r` の上書きを
        # 期待できないので、素直に積む。
        if _is_tty(stream):
            # `ljust` は文字数で詰めるので、日本語を含む行では前の行の消し残りが出る。
            # 幅で数える（`textwidth.display_width`）。
            width = display_width(line)
            padded = line + " " * max(0, self._last_width - width)
            self._last_width = width
            end = "\n" if self._done else ""
            print(f"\r{padded}", end=end, file=stream, flush=True)
        else:
            print(line, file=stream, flush=True)


def _is_tty(stream) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:  # 閉じた・差し替えたストリームでも表示だけは続ける
        return False


def _duration(seconds: float) -> str:
    """`mm:ss`。1時間を超えたら `h:mm:ss`。"""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
