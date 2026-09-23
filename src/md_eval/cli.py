"""md-eval: score technical documents / RFCs on multiple dimensions with TypeSafe."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table
from typesafe_sdk import TypeSafeError

from .dimensions import DIMENSIONS, SCORES, Dimension, Reader, dimensions_for
from .evaluate import (
    MAX_STATE_TOKENS,
    DocResult,
    build_questions,
    build_state,
    evaluate_all,
    request_tokens,
)

EXTENSIONS = {".md", ".markdown"}
DIM_IDS = {d.id for d in SCORES}
READERS: dict[str, tuple[Reader, ...]] = {
    "human": ("human",),
    "agent": ("agent",),
    "both": ("human", "agent"),
    "page": ("page",),
}

err = Console(stderr=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="md-eval",
        description="Evaluate technical documents / RFCs for human readers and/or AI "
        "coding agents, plus risk flags, using the TypeSafe API.",
        epilog="To score options for a decision instead, run 'md-eval decide --help'.",
    )
    p.add_argument("paths", nargs="+", type=Path, help="Markdown files (.md, .markdown) or directories searched recursively for them")
    p.add_argument(
        "--reader",
        choices=READERS,
        default="agent",
        help="Who the document is evaluated for: 'human' (writing + substance), "
        "'agent' (AI coding agent readiness), 'both', or 'page' (readers of a published "
        "report, write-up or explainer; not specific to technical docs) (default: agent)",
    )
    p.add_argument("--audience", help="Intended human readers, e.g. 'backend engineers on the payments team'")
    p.add_argument(
        "--weight",
        action="append",
        default=[],
        metavar="DIM=W",
        help=f"Override a score weight (repeatable). Dimensions: {', '.join(sorted(DIM_IDS))}",
    )
    p.add_argument("--flag-threshold", type=float, default=0.5, help="P(yes) at which a flag is raised (default 0.5)")
    p.add_argument("--min-confidence", type=float, default=0.5, help="Mark scores below this confidence with '?' (default 0.5)")
    p.add_argument("--details", action="store_true", help="Print a per-document breakdown of every dimension")
    p.add_argument("--json", metavar="FILE", help="Write full raw results as JSON ('-' for stdout)")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel requests (default 4)")
    p.add_argument("--model", help="TypeSafe model (default: SDK default, jev-latest)")
    p.add_argument("--dry-run", action="store_true", help="Print the request for the first document and token estimates; no API calls")
    return p.parse_args(argv)


def collect_docs(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(f for f in path.rglob("*") if f.is_file() and f.suffix.lower() in EXTENSIONS))
        elif path.is_file() and path.suffix.lower() in EXTENSIONS:
            found.append(path)
        elif path.is_file():
            err.print(f"[yellow]skipping {path}: not a Markdown file[/]")
        else:
            err.print(f"[yellow]skipping {path}: not found[/]")
    return list(dict.fromkeys(found))


def parse_weights(overrides: list[str]) -> dict[str, float]:
    weights = {d.id: d.weight for d in SCORES}
    for item in overrides:
        dim, sep, value = item.partition("=")
        if not sep or dim not in DIM_IDS:
            raise SystemExit(f"invalid --weight {item!r}; expected DIM=W with DIM in {sorted(DIM_IDS)}")
        weights[dim] = float(value)
    return weights


def fmt(score: float) -> str:
    return f"{score * 100:.0f}"


def rank_score(r: DocResult, readers: tuple[Reader, ...], weights) -> float:
    """Single reader: that reader's total. Both: the mean of the two totals."""
    return sum(r.total(reader, weights) for reader in readers) / len(readers)


def print_summary(console: Console, results: list[DocResult], args, readers, weights) -> None:
    dims = dimensions_for(readers)
    ok = sorted((r for r in results if r.ok), key=lambda r: -rank_score(r, readers, weights))
    both = len(readers) > 1
    title = "ranked by mean of Human and Agent totals" if both else "ranked by weighted total"
    table = Table(title=f"Document evaluation for {args.reader} (0-100, {title})", title_justify="left")

    # (header, value-getter) pairs; both-mode shows subtotals only, --details has the rest.
    columns: list[tuple[str, str | None, callable]] = []
    if "human" in readers:
        columns.append(("Human" if both else "Total", "bold", lambda r: fmt(r.total("human", weights))))
        columns.append(("Wrt", "cyan", lambda r: fmt(r.group_score("writing", weights))))
        if not both:
            columns += [(d.short, None, _cell(d, args)) for d in dims if d.group == "writing"]
        columns.append(("Sub", "cyan", lambda r: fmt(r.group_score("substance", weights))))
        if not both:
            columns += [(d.short, None, _cell(d, args)) for d in dims if d.group == "substance"]
    if "agent" in readers:
        columns.append(("Agent" if both else "Total", "bold", lambda r: fmt(r.total("agent", weights))))
        if not both:
            columns += [(d.short, None, _cell(d, args)) for d in dims if d.group == "agent"]
    if "page" in readers:
        columns.append(("Total", "bold", lambda r: fmt(r.total("page", weights))))
        columns += [(d.short, None, _cell(d, args)) for d in dims if d.group == "page"]

    table.add_column("#", justify="right")
    table.add_column("Document", overflow="fold", max_width=28)
    for header, style, _ in columns:
        table.add_column(header, justify="right", style=style)
    table.add_column("Flags", overflow="fold")

    for rank, r in enumerate(ok, 1):
        flags = ", ".join(f"[red]{_short(f)}[/]" for f in r.raised_flags(args.flag_threshold)) or "[green]-[/]"
        table.add_row(str(rank), Path(r.path).name, *(get(r) for _, _, get in columns), flags)

    if ok:
        console.print(table)
        shown = [d for d in dims if both is False or d.is_flag]
        legend = "  ".join(f"{d.short}={d.label}" for d in shown if d.short != d.label)
        subtotals = "Wrt/Sub = writing/substance subtotals  ·  " if "human" in readers else ""
        console.print(f"[dim]{subtotals}{legend}[/]", highlight=False)
        if both:
            console.print("[dim]use --details for every dimension[/]", highlight=False)
        console.print(
            f"[dim]? = confidence < {args.min_confidence}  ·  flags raised at P(yes) ≥ {args.flag_threshold}  ·  "
            f"weights: {', '.join(f'{d.id}={weights[d.id]:g}' for d in dims if not d.is_flag)}[/]",
            highlight=False,
        )
    for r in results:
        if not r.ok:
            console.print(f"[red]✗ {r.path}: {r.error}[/]")


def _cell(d: Dimension, args):
    def get(r: DocResult) -> str:
        s = r.scores[d.id]
        return fmt(s.score) + ("[yellow]?[/]" if s.confidence < args.min_confidence else "")

    return get


def print_details(console: Console, r: DocResult, readers) -> None:
    table = Table(title=r.path, title_justify="left")
    table.add_column("Dimension")
    table.add_column("Score", justify="right")
    table.add_column("Conf.", justify="right")
    table.add_column("Most likely level", overflow="fold")
    group_names = {"writing": "Human · writing", "substance": "Human · substance", "agent": "AI agent", "page": "Published page", "flag": "Flags"}
    prev = None
    for d in dimensions_for(readers):
        if d.group != prev:
            table.add_row(f"[bold]{group_names[d.group]}[/]", end_section=False)
            prev = d.group
        if d.is_flag:
            table.add_row("  " + d.label, "", "", f"P(yes) = {r.flags[d.id]:.2f}")
            continue
        s = r.scores[d.id]
        top = max(s.probabilities, key=s.probabilities.get)
        table.add_row(
            "  " + d.label,
            fmt(s.score),
            f"{s.confidence:.2f}",
            f"{top}: {d.criteria[top]} ({s.probabilities[top]:.0%})",
        )
    console.print(table)


def _short(dim_id: str) -> str:
    return next(d.short for d in DIMENSIONS if d.id == dim_id)


def to_json(results: list[DocResult], readers, weights: dict[str, float], args) -> list[dict]:
    used = {d.id: weights[d.id] for d in dimensions_for(readers) if not d.is_flag}
    out = []
    for r in results:
        item = asdict(r)
        item["reader"] = args.reader
        if r.ok:
            item["totals"] = {reader: r.total(reader, weights) for reader in readers}
            if "human" in readers:
                item["writing"] = r.group_score("writing", weights)
                item["substance"] = r.group_score("substance", weights)
            item["raised_flags"] = r.raised_flags(args.flag_threshold)
            item["low_confidence"] = r.low_confidence(args.min_confidence)
        item["weights"] = used
        out.append(item)
    return out


def dry_run(console: Console, docs: list[tuple[Path, str]], audience: str | None, readers) -> None:
    dims = dimensions_for(readers)
    path, text = docs[0]
    request = {
        "state": build_state(path, text, audience),
        "questions": {k: q.model_dump(exclude_none=True) for k, q in build_questions(dims).items()},
    }
    body = json.dumps(request, indent=2)
    console.print(f"[bold]Request for {path}[/] (document text truncated for display)")
    request["state"]["document"]["text"] = text[:300] + ("…" if len(text) > 300 else "")
    console.print_json(json.dumps(request))
    console.print(f"\n[bold]{len(dims)} questions. Estimated tokens per request[/] (limit {MAX_STATE_TOKENS:,}):")
    for p, t in docs:
        tokens = request_tokens(build_state(p, t, audience), dims)
        status = "[red]too long[/]" if tokens > MAX_STATE_TOKENS else "ok"
        console.print(f"  {p}: ~{tokens:,}  {status}")
    console.print(f"[dim]full request body for first doc: {len(body):,} chars[/]")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "decide":
        from .decide import main as decide_main

        return decide_main(argv[1:])

    args = parse_args(argv)
    load_dotenv(find_dotenv(usecwd=True))
    weights = parse_weights(args.weight)
    readers = READERS[args.reader]

    paths = collect_docs(args.paths)
    if not paths:
        err.print("[red]no Markdown documents found[/]")
        return 2
    docs = [(p, p.read_text(encoding="utf-8", errors="replace")) for p in paths]

    # With --json -, stdout carries JSON only; the human report goes to stderr.
    console = err if args.json == "-" else Console()

    if args.dry_run:
        dry_run(console, docs, args.audience, readers)
        return 0

    try:
        results = asyncio.run(
            evaluate_all(docs, args.audience, dimensions_for(readers), args.concurrency, args.model)
        )
    except TypeSafeError as e:
        err.print(f"[red]{e}[/]\nSet TYPESAFE_API_KEY in your environment or in a .env file.")
        return 2

    print_summary(console, results, args, readers, weights)
    if args.details:
        for r in results:
            if r.ok:
                print_details(console, r, readers)

    if args.json:
        payload = json.dumps(to_json(results, readers, weights, args), indent=2)
        if args.json == "-":
            print(payload)
        else:
            Path(args.json).write_text(payload + "\n")
            console.print(f"[dim]wrote {args.json}[/]")

    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
