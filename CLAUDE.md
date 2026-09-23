# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`md-eval` is a Python CLI that scores Markdown docs (specs, RFCs, task docs) using the
TypeSafe System One API (`typesafe-sdk`, model `jev-latest`). It evaluates a doc for an
AI coding agent (default), for human readers, or both (`--reader agent|human|both`).

## Commands

```sh
uv sync                                        # install deps (Python 3.12, managed by uv)
uv run md-eval examples                        # evaluate a dir (recursive) or files
uv run md-eval examples --reader both --details
uv run md-eval examples --json -               # JSON on stdout, table on stderr
uv run md-eval examples --dry-run              # print request + token estimates, no API call
uv run python scripts/screenshots.py           # regenerate README screenshots in docs/ (calls the API)
```

- Real runs need `TYPESAFE_API_KEY`, read from the environment or a `.env` found by searching
  upward from the current directory. `--dry-run` needs no key.
- There is no test suite or linter configured. To check code paths offline, pass an
  `httpx2.MockTransport` as `transport=` to `AsyncTypeSafeClient`. That exercises the SDK's
  real request serialization and response parsing, which monkeypatching `system_one` skips.
- The tool is also installed globally in editable mode (`uv tool install --editable .`), so
  source edits take effect in the global `md-eval` command immediately.

## Architecture

Modules in `src/md_eval/`, driven by one data table:

- **`dimensions.py` is the single source of truth.** Every judgment is a `Dimension` record:
  id, group, instructions, criteria, weight. Groups map to readers through `READER_GROUPS`:
  `writing` + `substance` → human, `agent` → agent. `flag` dimensions are shared by all
  readers. `dimensions_for(readers)` picks the questions to send. Adding, removing or
  rewording a dimension should need no change outside this file.
- **`evaluate.py`** builds one TypeSafe request per document: state is
  `{"document": {"filename", "text"}, "intended_audience"?}`, and all selected questions go in
  that one request, answered in parallel.
  - Scores become `Score` questions (5 levels), normalized as `score / (len(criteria) - 1)`.
  - Flags become `Noul` questions (P(yes)).
  - Answers come from `response.answers[id]`.
  - Requests run concurrently under a semaphore. A `TypeSafeError` is recorded on that
    document's `DocResult.error` instead of aborting the run.
- **`cli.py`** handles argument parsing and Markdown-only file collection (`.md`, `.markdown`),
  plus rendering with `rich`.
  - Weighted totals and rankings are computed here, from raw scores and weights, with no
    re-inference.
  - With `both`, documents rank by the mean of the human and agent totals.
  - Exit code: 0 = ok, 1 = some document errored or was skipped, 2 = no documents found or
    missing API key.
- **`plan_hook.py`** (`md-eval-plan-hook`) is a Claude Code `PreToolUse` hook for
  `ExitPlanMode`. It scores `tool_input.plan` with the agent dimensions and denies once per
  session if the total is below `MD_EVAL_PLAN_MIN_SCORE` (default 0.6) or a flag is raised.
  It must fail open: any error allows the plan through.

## Design rules to preserve

- **Flags never feed into totals.** They are reported separately and raised at
  `--flag-threshold`. A serious risk should not average away.
- **Oversized docs are skipped, never truncated.** Jev allows roughly 32k tokens for state
  plus the longest question. Docs estimated above `MAX_STATE_TOKENS` (28k, estimated as
  chars/4) are skipped with an error, because dimensions like rollout or verifiability need
  the whole document.
- **Question wording is read literally by Jev.** Each Score level must describe a concrete,
  standalone situation. Question ids are not sent to the model, so the instructions must
  carry the full meaning. Refer to state fields by backticked path (`` `document.text` ``).
  Live docs: https://docs.typesafe.ai/llms.txt.
- **Check low confidence before trusting a score.** Scores below `--min-confidence` are
  marked `?`, and Noul answers carry no confidence.

## Examples

`examples/` holds fixtures with known expected outcomes (see the README table).
`risky-migration.md` intentionally contains fabricated credentials and PII so the Secrets
and PII flags have something to detect. Don't add "this is fake" notes inside the fixture
files, because the model would read them and it could bias the flags. The disclaimer
belongs in the README, and `.github/secret_scanning.yml` excludes `examples/` from GitHub
secret scanning.

A companion Claude Code skill at `~/.claude/skills/eval-docs/SKILL.md` documents how to run
the CLI and read its JSON output. Update it if CLI flags or JSON fields change.
