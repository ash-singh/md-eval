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
from .log import log_event, short_hash

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


def log_check(hook: str, session_id: str, subject: str, text: str, result: DocResult, total: float,
              flags: list[str], outcome: str, blocks: int, api_ms: int, **extra) -> None:
    """Log one scored gate check. Text, session and subject are stored as hashes only."""
    log_event(
        hook,
        session=short_hash(session_id),
        subject=short_hash(subject),
        text=short_hash(text),
        total=round(total, 3),
        scores={k: round(v.score, 3) for k, v in result.scores.items()},
        flags=flags,
        outcome=outcome,
        blocks=blocks,
        api_ms=api_ms,
        **extra,
    )


ERROR_HINTS = {
    "too_long": "too long to score",
    "firewall_blocked": "the TypeSafe API's firewall rejected the request; text that looks like "
    "shell commands or file paths such as /etc/passwd can trigger it",
}


def error_code(error: str) -> str:
    """A short label for a failed check, logged instead of the error message."""
    if error.startswith("too long"):
        return "too_long"
    if "403" in error and "cloudflare" in error.lower():
        return "firewall_blocked"
    return error.split(":", 1)[0].strip() or "error"


def check_failed(hook: str, session_id: str, subject: str, text: str, error: str, api_ms: int, what: str) -> dict | None:
    """Log a check that could not score, and tell the user once per session. The gate
    still fails open; this keeps a skipped check from passing for a clean one."""
    code = error_code(error)
    log_event(
        hook,
        session=short_hash(session_id),
        subject=short_hash(subject),
        text=short_hash(text),
        outcome="error",
        reason_codes=[code],
        api_ms=api_ms,
    )
    marker = session_file(hook, session_id, "error")
    if marker.exists():
        return None
    marker.touch()
    return {"systemMessage": f"md-eval: {what} not scored ({ERROR_HINTS.get(code, code)}); allowed without a check"}


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
