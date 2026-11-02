"""
The watcher's session check: `tb watch` stops when the session that started it is gone.

At start the watcher records its parent-process chain (`ancestors`): the shells and launchers above it (bash, cmd,
powershell, python, ...) and the first process that is not one of them (the Claude Code process, or the terminal
when a person runs it), each with its creation time. On every poll `SessionGuard.gone()` checks that each of them
still runs; a pid that was reused by a new process has another creation time and counts as gone.

Windows: the Toolhelp snapshot and GetProcessTimes through ctypes. POSIX: `/proc/<pid>/stat` where it exists
(Linux), else the immediate parent only (`os.getppid()`; an orphan is re-parented, so a changed ppid means gone).
Standard library only.
"""

import os
import re
from typing import NamedTuple

# Shells and launchers between the session and the watcher: the walk continues above them.
LAUNCHER = re.compile(r"^(bash|sh|dash|zsh|cmd|powershell|pwsh|python[\d.]*w?|py|pyw|env|timeout|winpty)$")
MAX_LEVELS = 6


class Ancestor(NamedTuple):
    pid: int
    created: int | None  # creation time (Windows FILETIME, Linux start time in ticks); None = unknown
    image: str


def _image_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")).lower()
    return base[:-4] if base.endswith(".exe") else base


def is_launcher(image: str) -> bool:
    return bool(LAUNCHER.match(_image_name(image)))


def walk(start_pid: int, parent_of, created_of, image_of, max_levels: int = MAX_LEVELS) -> list[Ancestor]:
    """
    The chain from start_pid upwards: launchers, then the first process that is not one (included), at most
    max_levels entries. parent_of/created_of/image_of return None when a process can't be read; the walk stops
    there. A parent created after its child is a reused pid (the real parent ended long ago): the walk stops too.
    """
    chain: list[Ancestor] = []
    pid = start_pid
    child_created = None
    while pid and len(chain) < max_levels:
        image = image_of(pid)
        if image is None:
            break
        created = created_of(pid)
        if child_created is not None and created is not None and created > child_created:
            break
        chain.append(Ancestor(pid, created, _image_name(image)))
        if not is_launcher(image):
            break
        child_created = created
        pid = parent_of(pid)
    return chain


# ---- Windows ----

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    def _process_table() -> dict[int, tuple[int, str]]:
        """{pid: (parent pid, exe name)} of every process (one Toolhelp snapshot)."""
        snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        table: dict[int, tuple[int, str]] = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
                ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snap)
        return table

    def _open(pid: int):
        """(handle or None, access denied)."""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            return handle, False
        return None, ctypes.get_last_error() == _ERROR_ACCESS_DENIED

    def _creation_of_handle(handle) -> int | None:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def _windows_created(pid: int) -> int | None:
        handle, _ = _open(pid)
        if not handle:
            return None
        try:
            return _creation_of_handle(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_ancestors() -> list[Ancestor]:
        table = _process_table()
        return walk(os.getppid(), lambda pid: table.get(pid, (0, ""))[0], _windows_created,
                    lambda pid: table[pid][1] if pid in table else None)

    def _windows_alive(entry: Ancestor) -> bool:
        handle, denied = _open(entry.pid)
        if not handle:
            return denied  # it exists but may not be queried (another user's or an elevated process)
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return False
            return entry.created is None or _creation_of_handle(handle) == entry.created
        finally:
            _kernel32.CloseHandle(handle)


# ---- POSIX ----

def _proc_stat(pid: int) -> tuple[int, str, int] | None:
    """(parent pid, command name, start time) from /proc/<pid>/stat, or None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    name = text[text.index("(") + 1:text.rindex(")")]
    fields = text[text.rindex(")") + 2:].split()
    return int(fields[1]), name, int(fields[19])  # fields 4 and 22 of stat(5), counted after the name


def _posix_ancestors(getppid=os.getppid, proc_stat=_proc_stat) -> list[Ancestor]:
    parent = getppid()
    if proc_stat(parent) is None:  # no /proc (macOS): the immediate parent only
        return [Ancestor(parent, None, "parent")] if parent > 1 else []
    return walk(parent, lambda pid: (proc_stat(pid) or (0,))[0], lambda pid: (proc_stat(pid) or (0, "", None))[2],
                lambda pid: (proc_stat(pid) or (0, None))[1])


def _posix_alive(entry: Ancestor, getppid=os.getppid, proc_stat=_proc_stat, first_pid: int | None = None) -> bool:
    if first_pid is not None and entry.pid == first_pid and getppid() != entry.pid:
        return False  # the watcher was re-parented: its parent ended
    try:
        os.kill(entry.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if entry.created is not None:
        stat = proc_stat(entry.pid)
        return stat is not None and stat[2] == entry.created
    return True


# ---- API ----

def ancestors() -> list[Ancestor]:
    """The watcher's parent chain, or [] when it can't be determined."""
    try:
        return _windows_ancestors() if os.name == "nt" else _posix_ancestors()
    except (OSError, ValueError, IndexError, AttributeError):
        return []


def alive(entry: Ancestor, immediate_parent: bool = False) -> bool:
    """entry still runs as the same process; immediate_parent (POSIX): also still this process's parent."""
    if os.name == "nt":
        return _windows_alive(entry)
    return _posix_alive(entry, first_pid=entry.pid if immediate_parent else None)


class SessionGuard:
    """gone(): the first recorded ancestor that ended (or was replaced by a reused pid), else None."""

    def __init__(self, chain: list[Ancestor], check=None):
        self.chain = list(chain)
        self._check = check or (lambda entry, first: alive(entry, first))

    def gone(self) -> Ancestor | None:
        for i, entry in enumerate(self.chain):
            if not self._check(entry, i == 0):
                return entry
        return None


def gone_message(entry: Ancestor) -> str:
    return f"SESSION GONE: {entry.image} (pid {entry.pid}) ended; the watcher stops"
