"""Per-request token usage from Claude Code transcripts: who makes the requests and how big their context is.

    python tools/usage.py ~/.claude/projects/<project slug> [days]

Sums the `usage` of every model reply in the project's transcript files (main sessions and their subagents) of
the last `days` days (default 7): requests, average context (input + cache read + cache write), cache reads and
output per day and model, then the biggest sessions by cache reads. Every turn re-reads the whole context, so
`requests x average context` is the cost driver. The transcript format is internal to Claude Code and may change
with any release: a dev tool for measuring, not part of the harness. Stdlib only.
"""
import json, sys, glob, os, collections, datetime
root = sys.argv[1]
days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
rows = []  # (ts, kind, model, ctx, out, file)
for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m = e.get("message") or {}
            u = m.get("usage") if isinstance(m, dict) else None
            if not u or e.get("timestamp", "") < since:
                continue
            ctx = u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("input_tokens", 0)
            kind = "subagent" if (e.get("isSidechain") or "subagents" in path.replace("\\", "/")) else "main"
            rows.append((e["timestamp"], kind, m.get("model", "?"), ctx, u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), os.path.basename(os.path.dirname(path)) if kind == "subagent" else os.path.basename(path)))
print(f"{len(rows)} requests in {days} days under {root}")
by = collections.defaultdict(lambda: [0, 0, 0, 0])
for ts, kind, model, ctx, out, cr, f in rows:
    b = by[(ts[:10], kind, model)]
    b[0] += 1; b[1] += ctx; b[2] += out; b[3] += cr
print(f"{'day':10} {'kind':9} {'model':28} {'reqs':>6} {'avg ctx':>9} {'cache read':>12} {'out':>8}")
for (day, kind, model), (n, ctx, out, cr) in sorted(by.items()):
    print(f"{day:10} {kind:9} {model:28} {n:6} {ctx//n:9,} {cr:12,} {out:8,}")
# Biggest sessions/agents by cache read
big = collections.Counter()
for ts, kind, model, ctx, out, cr, f in rows:
    big[(kind, f)] += cr
print("\nTop 8 by cache read:")
for (kind, f), cr in big.most_common(8):
    print(f"  {kind:9} {f[:40]:40} {cr:14,}")
