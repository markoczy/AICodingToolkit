"""
The periodic update of the checkout: the schedule, the reporting and the board header's state, shared by both
version control backends; the update itself is the backend's (taskboard/vcs: `git.py` fetches and fast-forwards
the main branch, `svn.py` runs `svn update` of the task folders; each module's docstring holds its rules).

Run by the board runners: `tb serve` in a background thread and `tb watch` in its poll loop, every
`watch.update_seconds` (default 60; 0 disables). Common to both backends:

- Local task edits pause the update unless `watch.update_when_dirty` is true; a conflict stops it until the
  user resolves it (the tools never resolve anything).
- Nothing can prompt (the backend's environment, no stdin, a timeout); credentials are never read or stored.
- The update runs under the cross-process write lock (store.write_lock), so no task-file write interleaves.
- Failures (offline, authentication, timeout, a refused fast-forward) are reported once per message, identical
  repeats are silent, recovery is reported; after 3 failures in a row the interval backs off to 5 minutes.
"""

import time

from .vcs import UpdateResult, backend

__all__ = ["UpdateResult", "Updater", "banner", "serve_updates", "run_update", "update_paths", "conflicted",
           "BACKOFF_AFTER", "BACKOFF_SECONDS", "FAILURES_TO_WAKE"]

BACKOFF_AFTER = 3
BACKOFF_SECONDS = 300
FAILURES_TO_WAKE = 3


def update_paths(config: dict) -> list[str]:
    return list(config["watch"].get("update_paths") or ["AI"])


def run_update(config: dict, lock_wait: float = 5.0) -> UpdateResult:
    """One update attempt by the config's backend. Never raises for version control trouble."""
    return backend(config).update(config, lock_wait)


def conflicted(config: dict) -> list[str]:
    """Conflicted paths in the update paths (the backend's, local only)."""
    return backend(config).conflicted(config)


class Updater:
    """
    The update schedule and its reporting, shared by `tb serve` (thread) and `tb watch` (poll loop).

    `due()` says whether an attempt is due; `run()` attempts one and returns the result. `notices` collects
    the lines worth reporting (first failure, a changed message, recovery, conflict, pause), each once.
    `update` replaces the backend's update (tests).
    """

    def __init__(self, config: dict, interval: float, clock=time.monotonic, wall=time.time,
                 update=None):
        self.config = config
        self.backend = backend(config)
        self.interval = interval
        self.clock = clock
        self.wall = wall
        self.update = update
        self.next_at = 0.0
        self.failures = 0
        self.enabled = interval > 0
        self.reported = ""
        self.state = {"status": "off" if not self.enabled else "pending", "message": "", "revision": None,
                      "last_attempt": None, "last_ok": None, "failures": 0, "conflicts": [],
                      "interval": interval, "vcs": self.backend.name}

    def due(self) -> bool:
        return self.enabled and self.clock() >= self.next_at

    def run(self) -> tuple[UpdateResult, list[str]]:
        """One attempt; (result, notice lines to print/log, normally none)."""
        result = self.update(self.config) if self.update else run_update(self.config)
        notices: list[str] = []
        now = self.clock()
        if result.status == "no-vcs":
            self.enabled = False
        if result.status == "error":
            self.failures += 1
        elif result.status != "busy":
            self.failures = 0
        wait = BACKOFF_SECONDS if self.failures >= BACKOFF_AFTER else self.interval
        self.next_at = now + max(wait, self.interval)
        key = result.status if result.status in ("ok", "changed") else f"{result.status}: {result.message}"
        if result.status in ("ok", "changed"):
            if self.reported:
                notices.append(f"update ok again ({self.backend.format_revision(result.revision)})")
            self.reported = ""
        elif result.status != "busy" and key != self.reported:
            self.reported = key
            notices.append(self.describe(result))
        stamp = self.wall()
        self.state.update({"status": result.status, "message": result.message,
                           "failures": self.failures, "conflicts": list(result.conflicts), "last_attempt": stamp,
                           "revision": result.revision if result.revision is not None else self.state["revision"]})
        if result.status in ("ok", "changed"):
            self.state["last_ok"] = stamp
        return result, notices

    @staticmethod
    def describe(result: UpdateResult) -> str:
        if result.status in ("conflict", "diverged"):
            return f"update stopped: {result.message}"
        if result.status == "dirty-skip":
            return f"update paused: {result.message}"
        if result.status in ("no-vcs", "off-branch"):
            return f"update {'disabled' if result.status == 'no-vcs' else 'off'}: {result.message}"
        return f"update failed: {result.message}; retrying"


def banner(state: dict) -> tuple[str, str]:
    """(text, level) of the board header's update line for an Updater.state; ("", "") when updating is off."""
    status = state.get("status")
    if status in (None, "off"):
        return "", ""
    vcs = backend(state.get("vcs") or "git")
    if status == "pending":
        return "Update pending", "info"
    if status in ("ok", "changed", "busy") and state.get("last_ok"):
        when = time.strftime("%H:%M", time.localtime(state["last_ok"]))
        rev = f"{vcs.format_revision(state['revision'])}, " if state.get("revision") is not None else ""
        return f"Updated {rev}{when}", "info"
    if status == "busy":
        return "Update busy, retrying", "info"
    if status == "conflict":
        return vcs.conflict_banner(state.get("conflicts") or []), "error"
    if status == "diverged":
        return f"Update stopped: {state.get('message')}", "error"
    if status == "dirty-skip":
        return f"Update paused: {state.get('message')}", "warn"
    if status == "off-branch":
        return f"Update off: {state.get('message')}", "warn"
    if status == "no-vcs":
        return f"Update off: {state.get('message') or f'{vcs.name} is not on PATH'}", "warn"
    return f"Update failed: {state.get('message')}, retrying", "error"


def serve_updates(updater: Updater, lock, stop=None, tick: float = 1.0, out=print) -> None:
    """The `tb serve` updater thread: every due update runs under the server's write lock; logs state changes."""
    while updater.enabled and not (stop is not None and stop.is_set()):
        if updater.due():
            with lock:
                result, notices = updater.run()
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if result.status == "changed":
                fmt = updater.backend.format_revision
                notices = [f"updated {fmt(result.revision_before)} -> {fmt(result.revision)} "
                           f"({len(result.changed)} file(s))"] + notices
            for line in notices:
                out(f"{stamp} {line}", flush=True)
        if stop is not None:
            stop.wait(tick)
        else:
            time.sleep(tick)
