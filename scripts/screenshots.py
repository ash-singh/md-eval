"""Regenerate the README screenshots in docs/ from real md-eval runs (calls the API).

Usage: uv run python scripts/screenshots.py
"""

import io

import rich.console
from rich.terminal_theme import MONOKAI

import md_eval.cli as cli

SHOTS = [
    ("docs/sample-output-agent.svg", ["examples"]),
    ("docs/sample-output-both.svg", ["examples", "--reader", "both"]),
]

for path, argv in SHOTS:
    rec = rich.console.Console(record=True, width=112, file=io.StringIO(), force_terminal=True)
    cli.Console = lambda **kw: rec
    cli.err = rec
    code = cli.main(argv)
    rec.save_svg(path, title="$ md-eval " + " ".join(argv), theme=MONOKAI)
    print(f"{path} (exit {code})")
