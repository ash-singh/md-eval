"""md-eval decide: score a set of options against criteria and constraints with TypeSafe.

Input is a JSON decision (see validate). One request asks, in parallel:
  - a Score per option x criterion (combined into a weighted total in code)
  - a Noul per option x constraint: does the option violate it? (a veto, never averaged)
  - a Noul per option: is it too vague to judge?
  - a Noul for the decision: does it hinge on something only the user knows?
  - one holistic Choice over the options plus "none fit", as a cross-check

Code then turns the answers into a status: proceed, ask_user, clarify_options or
no_viable_option, with the reasons and every raw number, so the caller can see why.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeError

from .evaluate import MAX_STATE_TOKENS, estimate_tokens

NONE_FIT = "none_fit"

# Score levels for an option against one criterion, lowest first.
LEVELS = (
    "The option fails this criterion or works directly against it",
    "The option meets this criterion poorly: major shortfalls that need significant extra work or accepted risk",
    "The option partly meets this criterion: it helps, but with clear gaps",
    "The option meets this criterion well, with only minor gaps",
    "The option fully meets this criterion with no meaningful gap",
)

CONTEXT = (
    "The decision is described in `decision`: the problem in `decision.problem`, background in "
    "`decision.context`, hard constraints in `decision.constraints`, candidate options in "
    "`decision.options` and evaluation criteria in `decision.criteria`."
)


def score_question(o: int, c: int) -> Score:
    return Score(
        instructions=f"{CONTEXT} How well does the option at `decision.options[{o}]` meet the criterion "
        f"at `decision.criteria[{c}]`, as a solution to `decision.problem`? Judge this criterion only.",
        criteria=list(LEVELS),
    )


def veto_question(o: int, k: int) -> Noul:
    return Noul(
        instructions=f"{CONTEXT} Would adopting the option at `decision.options[{o}]` violate the hard "
        f"constraint at `decision.constraints[{k}]`?",
        criteria={
            "true": "The option breaks the constraint, or cannot be carried out without breaking it",
            "false": "The option respects the constraint, or the constraint does not apply to it",
        },
    )


def vague_question(o: int) -> Noul:
    return Noul(
        instructions=f"{CONTEXT} Is the option at `decision.options[{o}]` described too vaguely to judge "
        "how it would work?",
        criteria={
            "true": "It names a goal or category but not the approach, so its effects can't be judged",
            "false": "It states a concrete approach whose effects can be judged",
        },
    )


NEEDS_USER = Noul(
    instructions=f"{CONTEXT} Does choosing among `decision.options` depend on a preference, priority or "
    "fact that is not stated in `decision` and that only the person who owns this project could supply?",
    criteria={
        "true": "A reasonable choice needs information or a preference the decision does not state",
        "false": "The stated problem, context, constraints and criteria are enough to choose",
    },
)


def best_question(options: list[dict]) -> Choice:
    criteria: dict[str, str] = {
        opt["id"]: f"The option at `decision.options[{i}]`" for i, opt in enumerate(options)
    }
    criteria[NONE_FIT] = "None of the options adequately solves `decision.problem` within `decision.constraints`"
    return Choice(
        instructions=f"{CONTEXT} Which option is the best overall choice for `decision.problem`, weighing "
        "all of `decision.criteria` and respecting `decision.constraints`?",
        criteria=criteria,
    )


class DecisionError(ValueError):
    """The decision input is malformed."""


def validate(raw: object) -> dict:
    """Normalize a decision dict, or raise DecisionError with an actionable message."""
    if not isinstance(raw, dict):
        raise DecisionError("decision must be a JSON object")
    problem = raw.get("problem")
    if not isinstance(problem, str) or not problem.strip():
        raise DecisionError("'problem' must be a non-empty string")
    context = raw.get("context", "")
    if not isinstance(context, str):
        raise DecisionError("'context' must be a string")
    constraints = raw.get("constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(c, str) and c.strip() for c in constraints):
        raise DecisionError("'constraints' must be a list of non-empty strings")

    options = raw.get("options")
    if not isinstance(options, list) or len(options) < 2:
        raise DecisionError("'options' must list at least 2 options")
    seen: set[str] = set()
    norm_options = []
    for i, opt in enumerate(options):
        if not isinstance(opt, dict) or not isinstance(opt.get("id"), str) or not opt["id"].strip():
            raise DecisionError(f"options[{i}] needs a non-empty string 'id'")
        if not isinstance(opt.get("summary"), str) or not opt["summary"].strip():
            raise DecisionError(f"options[{i}] needs a non-empty string 'summary'")
        if opt["id"] in seen or opt["id"] == NONE_FIT:
            raise DecisionError(f"option id {opt['id']!r} is duplicated or reserved")
        seen.add(opt["id"])
        norm = {"id": opt["id"], "summary": opt["summary"]}
        if opt.get("details"):
            norm["details"] = str(opt["details"])
        norm_options.append(norm)

    criteria = raw.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise DecisionError("'criteria' must list at least 1 criterion")
    crit_ids: set[str] = set()
    norm_criteria = []
    for i, crit in enumerate(criteria):
        if not isinstance(crit, dict) or not isinstance(crit.get("id"), str) or not crit["id"].strip():
            raise DecisionError(f"criteria[{i}] needs a non-empty string 'id'")
        if not isinstance(crit.get("description"), str) or not crit["description"].strip():
            raise DecisionError(f"criteria[{i}] needs a non-empty string 'description'")
        if crit["id"] in crit_ids:
            raise DecisionError(f"criterion id {crit['id']!r} is duplicated")
        crit_ids.add(crit["id"])
        weight = crit.get("weight", 1)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            raise DecisionError(f"criteria[{i}] 'weight' must be a positive number")
        norm_criteria.append({"id": crit["id"], "description": crit["description"], "weight": float(weight)})

    return {
        "problem": problem,
        "context": context,
        "constraints": constraints,
        "options": norm_options,
        "criteria": norm_criteria,
    }


def build_state(decision: dict) -> dict:
    # Weights are policy applied in code; the model judges each criterion on its own.
    criteria = [{"id": c["id"], "description": c["description"]} for c in decision["criteria"]]
    return {"decision": {**decision, "criteria": criteria}}


def build_questions(decision: dict) -> dict[str, Score | Noul | Choice]:
    options, criteria, constraints = decision["options"], decision["criteria"], decision["constraints"]
    questions: dict[str, Score | Noul | Choice] = {}
    for o in range(len(options)):
        for c in range(len(criteria)):
            questions[f"score:{o}:{c}"] = score_question(o, c)
        for k in range(len(constraints)):
            questions[f"veto:{o}:{k}"] = veto_question(o, k)
        questions[f"vague:{o}"] = vague_question(o)
    questions["needs_user"] = NEEDS_USER
    questions["best"] = best_question(options)
    return questions


def request_tokens(state: dict, questions: dict) -> int:
    longest_q = max(len(json.dumps(q.model_dump())) for q in questions.values())
    return estimate_tokens(json.dumps(state)) + longest_q // 4


@dataclass
class Thresholds:
    veto: float = 0.5  # P(violates a constraint) at which an option is ruled out
    vague: float = 0.5  # P(too vague) at which options must be clarified
    ask: float = 0.5  # P(needs user input) at which to ask the user
    margin: float = 0.1  # min lead of the top option's total (0-1) over the runner-up
    min_confidence: float = 0.5  # score confidence below which the top two are uncertain


@dataclass
class CriterionScore:
    score: float  # normalized 0-1
    confidence: float


@dataclass
class OptionResult:
    id: str
    summary: str
    total: float
    scores: dict[str, CriterionScore]
    vetoes: dict[str, float]  # constraint text -> P(violates)
    vague: float
    viable: bool = True


@dataclass
class DecisionResult:
    status: str
    recommendation: str | None
    reasons: list[str]
    ranking: list[OptionResult]
    needs_user_input: float
    holistic: dict
    thresholds: Thresholds
    weights: dict[str, float]
    model: str | None = None
    input_tokens: int | None = None
    tokens: int = 0
    notes: list[str] = field(default_factory=list)


def bounded_total(r: "OptionResult", weights: dict[str, float], min_confidence: float, extreme: float) -> float:
    """Weighted total with every low-confidence score replaced by `extreme` (0 or 1)."""
    total_w = sum(weights.values())
    return sum(
        (extreme if s.confidence < min_confidence else s.score) * weights[cid] for cid, s in r.scores.items()
    ) / total_w


def interpret(decision: dict, answers, thresholds: Thresholds) -> DecisionResult:
    """Turn raw answers into a ranked, reasoned decision. Pure: no API calls."""
    options, criteria, constraints = decision["options"], decision["criteria"], decision["constraints"]
    weights = {c["id"]: c["weight"] for c in criteria}
    total_w = sum(weights.values())

    results: list[OptionResult] = []
    for o, opt in enumerate(options):
        scores = {}
        for c, crit in enumerate(criteria):
            a = answers[f"score:{o}:{c}"]
            scores[crit["id"]] = CriterionScore(score=a.score / (len(LEVELS) - 1), confidence=a.confidence)
        total = sum(scores[cid].score * w for cid, w in weights.items()) / total_w
        vetoes = {constraints[k]: answers[f"veto:{o}:{k}"].noul for k in range(len(constraints))}
        results.append(
            OptionResult(
                id=opt["id"],
                summary=opt["summary"],
                total=total,
                scores=scores,
                vetoes=vetoes,
                vague=answers[f"vague:{o}"].noul,
                viable=all(p < thresholds.veto for p in vetoes.values()),
            )
        )
    results.sort(key=lambda r: (not r.viable, -r.total))

    best = answers["best"]
    holistic = {"choice": best.choice, "confidence": best.confidence, "probabilities": dict(best.probabilities)}
    needs_user = answers["needs_user"].noul
    viable = [r for r in results if r.viable]
    reasons: list[str] = []

    for r in results:
        for constraint, p in r.vetoes.items():
            if p >= thresholds.veto:
                reasons.append(f"{r.id} likely violates constraint {constraint!r} (P={p:.2f})")

    vague = [r for r in results if r.vague >= thresholds.vague]
    if not viable:
        status = "no_viable_option"
        reasons.append("every option likely violates a constraint")
    elif vague:
        status = "clarify_options"
        reasons += [f"{r.id} is too vague to judge (P={r.vague:.2f})" for r in vague]
    else:
        ask: list[str] = []
        top = viable[0]
        if needs_user >= thresholds.ask:
            ask.append(f"the choice likely depends on a preference or fact only the user knows (P={needs_user:.2f})")
        if len(viable) > 1 and top.total - viable[1].total < thresholds.margin:
            ask.append(
                f"{top.id} leads {viable[1].id} by only {(top.total - viable[1].total) * 100:.0f} points "
                f"(margin {thresholds.margin * 100:.0f})"
            )
        # Uncertain scores matter only if they could flip the result: push the leader's
        # low-confidence scores to 0 and each rival's to 1, and see whether a rival catches up.
        worst_top = bounded_total(top, weights, thresholds.min_confidence, 0.0)
        for r in viable[1:]:
            best_r = bounded_total(r, weights, thresholds.min_confidence, 1.0)
            if best_r > worst_top and (worst_top < top.total or best_r > r.total):
                ask.append(
                    f"low-confidence scores could flip {top.id} vs {r.id} "
                    f"({top.id} could fall to {fmt(worst_top)}, {r.id} could reach {fmt(best_r)})"
                )
        if best.choice == NONE_FIT:
            ask.append(f"the holistic judgment is that no option fits (P={best.probabilities.get(NONE_FIT, 0):.2f})")
        elif best.choice != top.id:
            ask.append(f"the holistic pick {best.choice} disagrees with the weighted top {top.id}")
        status = "ask_user" if ask else "proceed"
        reasons += ask

    return DecisionResult(
        status=status,
        recommendation=viable[0].id if status in ("proceed", "ask_user") else None,
        reasons=reasons,
        ranking=results,
        needs_user_input=needs_user,
        holistic=holistic,
        thresholds=thresholds,
        weights=weights,
    )


async def decide(
    decision: dict, thresholds: Thresholds, client: AsyncTypeSafeClient
) -> DecisionResult:
    state = build_state(decision)
    questions = build_questions(decision)
    tokens = request_tokens(state, questions)
    if tokens > MAX_STATE_TOKENS:
        raise DecisionError(
            f"decision too long (~{tokens:,} tokens > {MAX_STATE_TOKENS:,}); shorten context, summaries or details"
        )
    response = await client.system_one(state, questions)
    result = interpret(decision, response.answers, thresholds)
    result.model = response.model
    result.input_tokens = response.usage.input_tokens
    result.tokens = tokens
    result.notes.append(f"{len(questions)} questions in one request")
    return result


def fmt(x: float) -> str:
    return f"{x * 100:.0f}"


def print_result(console: Console, decision: dict, r: DecisionResult) -> None:
    table = Table(title=f"Decision: {decision['problem']}", title_justify="left")
    table.add_column("#", justify="right")
    table.add_column("Option", overflow="fold", max_width=24)
    table.add_column("Total", justify="right", style="bold")
    for crit in decision["criteria"]:
        table.add_column(f"{crit['id']} ×{crit['weight']:g}", justify="right")
    table.add_column("Vetoed by", overflow="fold")
    for rank, o in enumerate(r.ranking, 1):
        cells = [
            fmt(s.score) + ("[yellow]?[/]" if s.confidence < r.thresholds.min_confidence else "")
            for s in (o.scores[c["id"]] for c in decision["criteria"])
        ]
        vetoed = [f"C{k + 1} ({p:.2f})" for k, p in enumerate(o.vetoes.values()) if p >= r.thresholds.veto]
        table.add_row(
            str(rank) if o.viable else "-",
            o.id if o.viable else f"[dim]{o.id}[/]",
            fmt(o.total),
            *cells,
            "[red]" + "; ".join(vetoed) + "[/]" if vetoed else "[green]-[/]",
        )
    console.print(table)
    for k, c in enumerate(decision["constraints"]):
        console.print(f"[dim]C{k + 1}: {c}[/]", highlight=False)
    style = {"proceed": "green", "ask_user": "yellow"}.get(r.status, "red")
    console.print(f"[bold {style}]{r.status}[/]" + (f"  recommendation: [bold]{r.recommendation}[/]" if r.recommendation else ""))
    for reason in r.reasons:
        console.print(f"  - {reason}", highlight=False)
    console.print(
        f"[dim]holistic pick: {r.holistic['choice']} ({r.holistic['confidence']:.2f})  ·  "
        f"needs user input: P={r.needs_user_input:.2f}  ·  ? = confidence < {r.thresholds.min_confidence}[/]",
        highlight=False,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="md-eval decide",
        description="Score options for a decision against criteria and constraints with TypeSafe. "
        "Input JSON: {problem, context?, constraints?: [str], options: [{id, summary, details?}], "
        "criteria: [{id, description, weight?}]}",
    )
    p.add_argument("file", help="Decision JSON file, or '-' for stdin")
    p.add_argument("--json", metavar="FILE", help="Write the full result as JSON ('-' for stdout)")
    d = Thresholds()
    p.add_argument("--veto-threshold", type=float, default=d.veto, help=f"P(violates a constraint) that rules an option out (default {d.veto})")
    p.add_argument("--vague-threshold", type=float, default=d.vague, help=f"P(too vague) that asks for clearer options (default {d.vague})")
    p.add_argument("--ask-threshold", type=float, default=d.ask, help=f"P(needs user input) that asks the user (default {d.ask})")
    p.add_argument("--margin", type=float, default=d.margin, help=f"Min lead of the top total over the runner-up, 0-1 (default {d.margin})")
    p.add_argument("--min-confidence", type=float, default=d.min_confidence, help=f"Score confidence below which the top two are uncertain (default {d.min_confidence})")
    p.add_argument("--model", help="TypeSafe model (default: SDK default, jev-latest)")
    p.add_argument("--dry-run", action="store_true", help="Print the request and token estimate; no API call")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    load_dotenv(find_dotenv(usecwd=True))
    err = Console(stderr=True)
    console = err if args.json == "-" else Console()

    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        decision = validate(json.loads(text))
    except (OSError, json.JSONDecodeError, DecisionError) as e:
        err.print(f"[red]invalid decision: {e}[/]")
        return 2

    if args.dry_run:
        state, questions = build_state(decision), build_questions(decision)
        request = {"state": state, "questions": {k: q.model_dump(exclude_none=True) for k, q in questions.items()}}
        console.print_json(json.dumps(request))
        console.print(f"{len(questions)} questions, ~{request_tokens(state, questions):,} tokens (limit {MAX_STATE_TOKENS:,})")
        return 0

    thresholds = Thresholds(
        veto=args.veto_threshold,
        vague=args.vague_threshold,
        ask=args.ask_threshold,
        margin=args.margin,
        min_confidence=args.min_confidence,
    )

    async def run() -> DecisionResult:
        async with AsyncTypeSafeClient(model=args.model) as client:
            return await decide(decision, thresholds, client)

    try:
        result = asyncio.run(run())
    except DecisionError as e:
        err.print(f"[red]{e}[/]")
        return 2
    except TypeSafeError as e:
        err.print(f"[red]{e}[/]\nSet TYPESAFE_API_KEY in your environment or in a .env file.")
        return 2 if "key" in str(e).lower() else 1

    print_result(console, decision, result)
    if args.json:
        payload = json.dumps(asdict(result), indent=2)
        if args.json == "-":
            print(payload)
        else:
            Path(args.json).write_text(payload + "\n")
            console.print(f"[dim]wrote {args.json}[/]")
    return 0
