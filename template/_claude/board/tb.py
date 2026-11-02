"""
Task board entry point: `python .claude/board/tb.py <command>` from anywhere in the repo.

Commands: board, check, show, next-id, next-plan, new, move, log, ask, answer, decide, set, section, me,
serve, watch. `--help` on any command explains it. AI/Tasks/README.md defines the files; AI/Methodology.md the flow.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from taskboard.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
