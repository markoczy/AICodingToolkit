"""Windows subprocess flags: keep console children of the (console-less) board server from flashing a window."""

import os
import subprocess


def no_window_flags() -> int:
    """`creationflags` for console children: CREATE_NO_WINDOW on Windows, 0 elsewhere."""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
