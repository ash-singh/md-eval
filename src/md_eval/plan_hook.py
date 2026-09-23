"""Claude Code PreToolUse hook: score a plan with md-eval before ExitPlanMode.

Reads the hook payload on stdin, evaluates `tool_input.plan` for an AI coding
agent reader, and denies ExitPlanMode with concrete gaps when the plan scores
below the threshold or raises a flag. Claude sees the reason and revises.

Fails open: any error (no API key, network, oversized plan) lets the plan through.
Each session is sent back at most MD_EVAL_PLAN_MAX_BLOCKS times (default 1), so a
plan that can't improve without user input never loops.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .dimensions import DIMENSIONS, dimensions_for
from .evaluate import evaluate_all

READERS = ("agent",)
WEAKEST = 3


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _counter(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "unknown"
    return Path(tempfile.gettempdir()) / f"md-eval-plan-hook-{safe}.count"


def _feedback(result, total: float, min_score: float, flag_threshold: float, min_conf: float) -> str:
    dims = {d.id: d for d in DIMENSIONS}
    lines = [
        f"md-eval (TypeSafe Jev) scored this plan {total * 100:.0f}/100 for an AI coding agent "
        f"implementing it (threshold {min_score * 100:.0f}).",
    ]
    weakest = sorted(result.scores.items(), key=lambda kv: kv[1].score)[:WEAKEST]
    if weakest:
        lines.append("Weakest dimensions:")
    for dim_id, s in weakest:
        d = dims[dim_id]
        level = round(s.level)
        line = f"- {d.label} ({s.score * 100:.0f}/100"
        line += ", low confidence)" if s.confidence < min_conf else ")"
        line += f": currently closest to \"{d.criteria[level]}\""
        if level + 1 < len(d.criteria):
            line += f"; a strong plan has \"{d.criteria[-1]}\""
        lines.append(line)
    raised = result.raised_flags(flag_threshold)
    if raised:
        lines.append("Raised flags: " + ", ".join(f"{dims[f].label} (P={result.flags[f]:.2f})" for f in raised))
    lines.append(
        "Revise the plan to close these gaps where you can from facts you already know or can "
        "look up in the codebase (files, functions, commands, acceptance checks). Do not invent "
        "details. If a gap needs the user's input, ask them instead. These are model judgments, "
        "not certainties; the plan will not be sent back again this session."
    )
    return "\n".join(lines)


def run(payload: dict) -> dict | None:
    """Return hook JSON output, or None to allow silently."""
    plan = (payload.get("tool_input") or {}).get("plan") or ""
    if not plan.strip():
        return None

    max_blocks = int(_env_float("MD_EVAL_PLAN_MAX_BLOCKS", 1))
    counter = _counter(str(payload.get("session_id", "")))
    blocks = int(counter.read_text()) if counter.exists() else 0

    min_score = _env_float("MD_EVAL_PLAN_MIN_SCORE", 0.6)
    flag_threshold = _env_float("MD_EVAL_PLAN_FLAG_THRESHOLD", 0.5)
    min_conf = _env_float("MD_EVAL_PLAN_MIN_CONFIDENCE", 0.5)

    load_dotenv(find_dotenv(usecwd=True))
    [result] = asyncio.run(
        evaluate_all([(Path("plan.md"), plan)], None, dimensions_for(READERS), 1, None)
    )
    if not result.ok:
        return None

    weights = {d.id: d.weight for d in DIMENSIONS if not d.is_flag}
    total = result.total("agent", weights)
    failing = total < min_score or result.raised_flags(flag_threshold)
    summary = f"md-eval: plan scored {total * 100:.0f}/100 for agent readiness"

    if not failing or blocks >= max_blocks:
        return {"systemMessage": summary}

    counter.write_text(str(blocks + 1))
    return {
        "systemMessage": summary + " — sent back for revision",
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _feedback(result, total, min_score, flag_threshold, min_conf),
        },
    }


def main() -> None:
    try:
        output = run(json.load(sys.stdin))
    except Exception as e:  # fail open: never block planning on a hook failure
        print(f"md-eval plan hook skipped: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
    if output:
        print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
