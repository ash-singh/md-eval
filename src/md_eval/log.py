"""Local decision log: one JSON line per gate check or `decide` run.

Records scores, outcomes, reason codes and timings, never the checked text: text,
session ids and file paths are stored only as short hashes. The log lives at
$MD_EVAL_LOG, or ~/.cache/md-eval/decisions.jsonl ($XDG_CACHE_HOME respected).
MD_EVAL_LOG=off disables it. Writing never raises and never touches stdout, since
hook output and `--json -` use stdout.
"""

import hashlib
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

SCHEMA = 1


def log_path() -> Path | None:
    configured = os.environ.get("MD_EVAL_LOG", "")
    if configured.lower() in {"off", "0", "false", "none"}:
        return None
    if configured:
        return Path(configured).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "md-eval" / "decisions.jsonl"


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _version() -> str:
    try:
        return version("md-eval")
    except PackageNotFoundError:
        return "unknown"


def log_event(tool: str, **fields) -> None:
    """Append one event. Silently does nothing if logging is off or fails."""
    try:
        path = log_path()
        if path is None:
            return
        event = {
            "schema": SCHEMA,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "version": _version(),
            **fields,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        pass


class Timer:
    """Milliseconds spent inside the block (API time; excludes uvx and process startup)."""

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc) -> None:
        self.ms = round((time.perf_counter() - self.start) * 1000)


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summarize(events: list[dict]) -> str:
    """Plain-text summary: counts, outcomes, timings, and before/after scores for revisions."""
    if not events:
        return "No events logged yet."
    lines = [f"{len(events)} events, {events[0]['ts']} to {events[-1]['ts']}"]
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_tool[e.get("tool", "?")].append(e)

    for tool, evs in sorted(by_tool.items()):
        lines.append(f"\n{tool}: {len(evs)} runs")
        outcomes = Counter(e.get("outcome") or e.get("status") for e in evs)
        lines.append("  outcomes: " + ", ".join(f"{k} {v}" for k, v in outcomes.most_common()))
        ms = sorted(e["api_ms"] for e in evs if isinstance(e.get("api_ms"), int))
        if ms:
            p90 = ms[min(len(ms) - 1, int(len(ms) * 0.9))]
            lines.append(f"  API time: median {statistics.median(ms):.0f} ms, p90 {p90} ms")
        scores = [e["total"] for e in evs if isinstance(e.get("total"), (int, float))]
        if scores:
            lines.append(f"  mean total: {statistics.mean(scores) * 100:.0f}/100")
        codes = Counter(c for e in evs for c in e.get("reason_codes", []) + e.get("flags", []))
        if codes:
            lines.append("  reasons/flags: " + ", ".join(f"{k} {v}" for k, v in codes.most_common()))

        # Revisions: a sent-back plan or page followed by a new score for the same subject.
        pairs = []
        last_blocked: dict[tuple, float] = {}
        for e in evs:
            key = (e.get("session"), e.get("subject"))
            if key in last_blocked and isinstance(e.get("total"), (int, float)):
                pairs.append((last_blocked.pop(key), e["total"]))
            if e.get("outcome") == "sent_back" and isinstance(e.get("total"), (int, float)):
                last_blocked[key] = e["total"]
        if pairs:
            better = sum(after > before for before, after in pairs)
            gain = statistics.mean(after - before for before, after in pairs) * 100
            lines.append(f"  revisions: {len(pairs)} scored again, {better} improved, mean change {gain:+.0f} points")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="md-eval stats", description="Summarize the local md-eval decision log.")
    p.add_argument("--path", type=Path, help="Log file (default: $MD_EVAL_LOG or ~/.cache/md-eval/decisions.jsonl)")
    args = p.parse_args(argv)
    path = args.path or log_path()
    if path is None:
        print("Logging is off (MD_EVAL_LOG=off).")
        return 0
    if not path.exists():
        print(f"No log at {path} yet.")
        return 0
    print(f"Log: {path}")
    print(summarize(read_events(path)))
    return 0
