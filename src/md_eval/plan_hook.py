"""Claude Code PreToolUse hook: score a plan with md-eval before ExitPlanMode.

Reads the hook payload on stdin, evaluates `tool_input.plan` for an AI coding
agent reader, and denies ExitPlanMode with concrete gaps when the plan scores
below the threshold or raises a flag. Claude sees the reason and revises.

Fails open: any error (network, oversized plan) lets the plan through. A missing
API key also lets it through, with a notice to the user once per session.
Each session is sent back at most MD_EVAL_PLAN_MAX_BLOCKS times (default 1), so a
plan that can't improve without user input never loops.
"""

import asyncio
from pathlib import Path

from .dimensions import DIMENSIONS, dimensions_for
from .evaluate import evaluate_all
from .hookutil import deny, env_float, flag_label, log_check, missing_key_notice, run_hook, session_file, weakest_lines
from .log import Timer

HOOK = "plan-hook"
READERS = ("agent",)


def _feedback(result, total: float, min_score: float, flag_threshold: float, min_conf: float) -> str:
    lines = [
        f"md-eval (TypeSafe Jev) scored this plan {total * 100:.0f}/100 for an AI coding agent "
        f"implementing it (threshold {min_score * 100:.0f}).",
        *weakest_lines(result, min_conf, "a strong plan"),
    ]
    raised = result.raised_flags(flag_threshold)
    if raised:
        lines.append("Raised flags: " + ", ".join(f"{flag_label(f)} (P={result.flags[f]:.2f})" for f in raised))
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

    session_id = str(payload.get("session_id", ""))
    notice = missing_key_notice(HOOK, session_id, "plan")
    if notice is not None:
        return notice or None

    max_blocks = int(env_float("MD_EVAL_PLAN_MAX_BLOCKS", 1))
    counter = session_file(HOOK, session_id, "count")
    blocks = int(counter.read_text()) if counter.exists() else 0

    min_score = env_float("MD_EVAL_PLAN_MIN_SCORE", 0.6)
    flag_threshold = env_float("MD_EVAL_PLAN_FLAG_THRESHOLD", 0.5)
    min_conf = env_float("MD_EVAL_PLAN_MIN_CONFIDENCE", 0.5)

    with Timer() as timer:
        [result] = asyncio.run(
            evaluate_all([(Path("plan.md"), plan)], None, dimensions_for(READERS), 1, None)
        )
    if not result.ok:
        return None

    weights = {d.id: d.weight for d in DIMENSIONS if not d.is_flag}
    total = result.total("agent", weights)
    flags = result.raised_flags(flag_threshold)
    failing = total < min_score or flags
    summary = f"md-eval: plan scored {total * 100:.0f}/100 for agent readiness"

    outcome = "passed" if not failing else "allowed_after_limit" if blocks >= max_blocks else "sent_back"
    log_check(HOOK, session_id, "plan", plan, result, total, flags, outcome, blocks, timer.ms)
    if outcome != "sent_back":
        return {"systemMessage": summary}

    counter.write_text(str(blocks + 1))
    return deny(summary + " — sent back for revision", _feedback(result, total, min_score, flag_threshold, min_conf))


def main() -> None:
    run_hook(run, "plan hook")


if __name__ == "__main__":
    main()
