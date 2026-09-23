"""Measure `md-eval decide` against decisions with known answers (calls the TypeSafe API).

    uv run python scripts/eval_decide.py                  # all cases in evals/decide/
    uv run python scripts/eval_decide.py --json out.json  # also write every result

Each case is a decision plus `expected` ({"acceptable": [option ids], "status": [statuses]})
and optionally `key_facts`: the fact that settles the decision, which Claude is expected to
state in `context`. Every case runs as:
  - informed: key_facts appended to context (what the decide-options skill asks Claude to do)
  - uninformed: without key_facts (only for cases that have them)
  - reversed: informed, with the options in reverse order (checks order sensitivity)

A run is correct when the status is allowed and, if the case lists acceptable options, the
recommendation is one of them. A wrong run that still says ask_user (or clarify, or no viable
option) is "caught": the user gets asked. "Confidently wrong" (status proceed, wrong answer)
is the failure that matters most. "Asked unnecessarily" counts correct runs that still asked
the user on a case that doesn't need user input: safe, but friction.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from typesafe_sdk import AsyncTypeSafeClient

from md_eval.decide import Thresholds, decide, validate

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATUSES = ["proceed", "ask_user"]


def variants(case: dict) -> dict[str, dict]:
    informed = dict(case)
    if case.get("key_facts"):
        informed["context"] = f"{case.get('context', '')} {case['key_facts']}".strip()
    out = {"informed": informed}
    if case.get("key_facts"):
        out["uninformed"] = dict(case)
    out["reversed"] = {**informed, "options": list(reversed(case["options"]))}
    return out


def grade(result, expected: dict) -> str:
    statuses = expected.get("status", DEFAULT_STATUSES)
    acceptable = expected.get("acceptable", [])
    right = result.status in statuses and (not acceptable or result.recommendation in acceptable)
    if right:
        return "correct"
    return "confidently_wrong" if result.status == "proceed" else "caught"


async def run_all(cases: dict[str, dict], thresholds: Thresholds, concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    async with AsyncTypeSafeClient() as client:

        async def one(name: str, variant: str, decision: dict) -> dict:
            async with sem:
                result = await decide(validate(decision), thresholds, client)
            return {
                "case": name,
                "variant": variant,
                "status": result.status,
                "recommendation": result.recommendation,
                "reason_codes": result.reason_codes,
                "grade": grade(result, cases[name]["expected"]),
                "totals": {o.id: round(o.total, 2) for o in result.ranking},
            }

        jobs = [one(name, v, d) for name, case in cases.items() for v, d in variants(case).items()]
        return await asyncio.gather(*jobs)


def report(runs: list[dict], cases: dict[str, dict]) -> str:
    lines = [f"{'case':20} {'variant':11} {'status':17} {'recommendation':18} grade"]
    for r in sorted(runs, key=lambda r: (r["case"], r["variant"])):
        mark = {"correct": "ok", "caught": "caught (asked user)", "confidently_wrong": "WRONG"}[r["grade"]]
        lines.append(f"{r['case']:20} {r['variant']:11} {r['status']:17} {str(r['recommendation']):18} {mark}")

    lines.append("")
    for variant in ("informed", "uninformed", "reversed"):
        vs = [r for r in runs if r["variant"] == variant]
        if not vs:
            continue
        n = len(vs)
        count = lambda g: sum(r["grade"] == g for r in vs)
        # Correct answer, but the user was asked anyway although the case needs no user input.
        extra_asks = sum(
            r["grade"] == "correct" and r["status"] == "ask_user"
            and "ask_user" not in cases[r["case"]]["expected"].get("status", [])
            for r in vs
        )
        lines.append(
            f"{variant:11} {count('correct')}/{n} correct, {count('caught')} caught, "
            f"{count('confidently_wrong')} confidently wrong, {extra_asks} asked the user unnecessarily"
        )
    by_case = {}
    for r in runs:
        by_case.setdefault(r["case"], {})[r["variant"]] = r["recommendation"]
    flips = [c for c, v in by_case.items() if v.get("informed") != v.get("reversed")]
    lines.append(f"order sensitivity: {len(flips)}/{len(by_case)} cases changed recommendation when reversed"
                 + (f" ({', '.join(sorted(flips))})" if flips else ""))
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cases", type=Path, default=ROOT / "evals" / "decide")
    p.add_argument("--json", type=Path, help="Write every run as JSON")
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args()
    load_dotenv(find_dotenv(usecwd=True))

    cases = {f.stem: json.loads(f.read_text()) for f in sorted(args.cases.glob("*.json"))}
    if not cases:
        print(f"no cases in {args.cases}", file=sys.stderr)
        return 2
    runs = asyncio.run(run_all(cases, Thresholds(), args.concurrency))
    print(report(runs, cases))
    if args.json:
        args.json.write_text(json.dumps(runs, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
