"""Shared plumbing for md-eval's Claude Code hooks (plan gate, artifact gate)."""

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .dimensions import DIMENSIONS
from .evaluate import DocResult

WEAKEST = 3
KEY_URL = "https://console.typesafe.ai"


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def session_file(hook: str, session_id: str, suffix: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "unknown"
    return Path(tempfile.gettempdir()) / f"md-eval-{hook}-{safe}.{suffix}"


def missing_key_notice(hook: str, session_id: str, what: str) -> dict | None:
    """None when a key is available. Otherwise a notice, shown once per session, so a
    missing key isn't mistaken for a passing check."""
    load_dotenv(find_dotenv(usecwd=True))
    if os.environ.get("TYPESAFE_API_KEY"):
        return None
    notice = session_file(hook, session_id, "nokey")
    if notice.exists():
        return {}
    notice.touch()
    return {"systemMessage": f"md-eval: TYPESAFE_API_KEY not set, {what} not scored (get a key at {KEY_URL})"}


def weakest_lines(result: DocResult, min_conf: float, strong: str, skip: tuple[str, ...] = ()) -> list[str]:
    """The lowest-scoring dimensions, each with its current level and the top level."""
    dims = {d.id: d for d in DIMENSIONS}
    ranked = sorted((kv for kv in result.scores.items() if kv[0] not in skip), key=lambda kv: kv[1].score)
    lines = ["Weakest dimensions:"] if ranked else []
    for dim_id, s in ranked[:WEAKEST]:
        d = dims[dim_id]
        level = round(s.level)
        line = f"- {d.label} ({s.score * 100:.0f}/100"
        line += ", low confidence)" if s.confidence < min_conf else ")"
        line += f": currently closest to \"{d.criteria[level]}\""
        if level + 1 < len(d.criteria):
            line += f"; {strong} has \"{d.criteria[-1]}\""
        lines.append(line)
    return lines


def flag_label(dim_id: str) -> str:
    return next(d.label for d in DIMENSIONS if d.id == dim_id)


def deny(summary: str, reason: str) -> dict:
    return {
        "systemMessage": summary,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def run_hook(run: Callable[[dict], dict | None], name: str) -> None:
    """Read the payload, run the hook and print its output. Fails open: always exits 0."""
    try:
        output = run(json.load(sys.stdin))
    except Exception as e:  # never block Claude on a hook failure
        print(f"md-eval {name} skipped: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
    if output:
        print(json.dumps(output))
    sys.exit(0)
