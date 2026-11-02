"""
Idle exit of a watcher-started board (`tb serve --idle-exit MIN`).

The board notes the time of every request it accepts (`Activity.touch`, in the app's before_request hook). The
watcher's look at `/api/fingerprint` every poll and an open board or task page (which polls the same endpoint)
both count, so the board stays while either is there and stops MIN minutes after the last of them is gone.
`idle_loop` checks that from a thread and stops the server under the board's write lock, so no write or update
in progress is cut off.
"""

import os
import threading
import time
from datetime import datetime

# Test-only: the idle limit in seconds instead of --idle-exit minutes (only when --idle-exit is given).
IDLE_SECONDS_ENV = "TB_IDLE_SECONDS"


class Activity:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last = clock()

    def touch(self) -> None:
        self.last = self.clock()

    def idle_seconds(self) -> float:
        return self.clock() - self.last


def idle_limit_seconds(minutes: float) -> float:
    """--idle-exit minutes as seconds; TB_IDLE_SECONDS overrides it (tests); 0 = never."""
    if not minutes:
        return 0.0
    override = os.environ.get(IDLE_SECONDS_ENV)
    if override:
        try:
            return max(0.1, float(override))
        except ValueError:
            pass
    return minutes * 60.0


def describe(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds >= 60 else f"{seconds:g} s"


def idle_loop(activity: Activity, limit: float, lock, stop, sleep=time.sleep, out=print) -> None:
    """Wait until nothing used the board for `limit` seconds, then call stop() holding `lock`."""
    step = min(10.0, max(0.05, limit / 5))
    while True:
        sleep(step)
        if activity.idle_seconds() < limit:
            continue
        with lock:  # a write, an update (or a board commit) in progress finishes first
            if activity.idle_seconds() < limit:
                continue  # a request arrived while waiting for the lock
            out(f"{datetime.now():%Y-%m-%d %H:%M:%S} idle for {describe(limit)}; the board stops", flush=True)
            stop()
            return


def start_idle_thread(activity: Activity, limit: float, lock, stop) -> threading.Thread:
    thread = threading.Thread(target=idle_loop, args=(activity, limit, lock, stop), name="idle-exit", daemon=True)
    thread.start()
    return thread
