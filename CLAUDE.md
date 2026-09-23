# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`md-eval` is a Python CLI that scores Markdown docs (specs, RFCs, task docs) using the
TypeSafe System One API (`typesafe-sdk`, model `jev-latest`). It evaluates a doc for an
AI coding agent (default), for human readers, or both (`--reader agent|human|both`), or as
general writing on a published page (`--reader page`). `md-eval decide` scores options for
a decision. Two Claude Code hooks gate plans and published artifacts.

## Commands

```sh
uv sync                                        # install deps (Python 3.12, managed by uv)
uv run md-eval examples                        # evaluate a dir (recursive) or files
uv run md-eval examples --reader both --details
uv run md-eval examples --json -               # JSON on stdout, table on stderr
uv run md-eval examples --dry-run              # print request + token estimates, no API call
uv run md-eval decide examples/decisions/profile-cache.json   # score options for a decision
uv run md-eval stats                           # summarize the local decision log
uv run pytest                                  # offline tests (mock API; no key or network)
uv run python scripts/screenshots.py           # regenerate README screenshots in docs/ (calls the API)
scripts/release.sh 0.2.0                       # cut a release: versions, plugin pins, commit, tag
```

- Real runs need `TYPESAFE_API_KEY`, read from the environment or a `.env` found by searching
  upward from the current directory. `--dry-run` needs no key.
- Tests live in `tests/` (pytest, no linter). An autouse fixture in `tests/conftest.py` swaps
  every `AsyncTypeSafeClient` for one on a counting `httpx2.MockTransport`, so the SDK's real
  serialization and parsing run with no network. It also chdirs to `tmp_path` (the repo's
  `.env` must not load) and redirects the log and session files. Steer answers with
  `api.score` / `api.noul` / `api.choice`. Any new module that builds a client must be added
  to that fixture. CI (`.github/workflows/ci.yml`) runs the tests and `--dry-run`s.
- The tool is also installed globally in editable mode (`uv tool install --editable .`), so
  source edits take effect in the global `md-eval` command immediately.

## Architecture

Modules in `src/md_eval/`, driven by one data table:

- **`dimensions.py` is the single source of truth.** Every judgment is a `Dimension` record:
  id, group, instructions, criteria, weight. Groups map to readers through `READER_GROUPS`:
  `writing` + `substance` → human, `agent` → agent, `page` → page. `flag` dimensions are shared by all
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
- Subcommands (`decide`, `stats`) are dispatched in `cli.main` before argument parsing, only
  when the first argument is not an existing path.
- **`decide.py`** (`md-eval decide`)
  scores a JSON decision in one request: a Score per option × criterion, a Noul per
  option × constraint (veto), a Noul per option (vague), a Noul for "needs user input", and
  one holistic Choice with a `none_fit` label. Question templates are data at the top of
  the module. `interpret()` is pure and turns answers into a `status` plus `reasons` and a
  parallel list of `reason_codes` (logged instead of the text).
  Vetoes never feed into totals. `decide()` takes an injected client, so it can be tested
  with a `MockTransport`.
- **`log.py`** appends one JSON line per gate check or `decide` run to
  `~/.cache/md-eval/decisions.jsonl` (`MD_EVAL_LOG` overrides; `off` disables). It stores
  scores, outcomes and reason codes, and only hashes of text, session ids and paths. Writes
  never raise and never use stdout. `md-eval stats` summarizes it. `api_ms` excludes uvx
  startup.
- **`hookutil.py`** holds what the hooks share: env thresholds, per-session state files in
  the temp dir, the once-per-session missing-key notice, the weakest-dimensions feedback,
  and `run_hook`, which always exits 0.
- **`plan_hook.py`** (`md-eval-plan-hook`) is a Claude Code `PreToolUse` hook for
  `ExitPlanMode`. It scores `tool_input.plan` with the agent dimensions and denies once per
  session if the total is below `MD_EVAL_PLAN_MIN_SCORE` (default 0.6) or a flag is raised.
  It must fail open: any error allows the plan through.
- **`artifact_hook.py`** (`md-eval-artifact-hook`) is a `PreToolUse` hook for the `Artifact`
  tool. It checks only a publish of one `.html`/`.htm`/`.md` file, before any other work.
  HTML is reduced to text with headings (`#`) and list items (`-`) kept. One request asks the
  `page` dimensions, the flags and an extra `is_written_document` Noul, so apps and
  dashboards are checked for flags only. It sends a file back at most once per session; a
  changed file after that is still scored and logged, then allowed. Unchanged text is never
  scored twice (state keyed by a hash of the path). It must fail open too.

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

## Claude Code plugins

The repo root is a plugin marketplace (`.claude-plugin/marketplace.json`) with three plugins:
`plugins/md-eval` (the `eval-docs` and `decide-options` skills, which document how to run
the CLI and read its JSON output) and `plugins/md-eval-plan-gate` (a `hooks.json` wiring `md-eval-plan-hook`
to `ExitPlanMode`) and `plugins/md-eval-artifact-gate` (wiring `md-eval-artifact-hook` to
`Artifact`). Both run the CLI from GitHub via
`uvx --from git+https://github.com/ash-singh/md-eval@vX.Y.Z`, pinned to a release tag, so
pushes to `main` don't reach plugin users until the next release.

- Update the skills in `plugins/md-eval/skills/` if CLI flags or JSON fields change.
- Release with `scripts/release.sh X.Y.Z`, then `git push origin main vX.Y.Z`. It sets one
  version in `pyproject.toml` and every `plugin.json` file (after running the tests), re-pins the uvx sources, validates
  the manifests, commits and tags. Don't edit versions or pins by hand.
