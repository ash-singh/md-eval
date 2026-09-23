# md-eval

A CLI that scores Markdown technical documents and RFCs using [TypeSafe](https://docs.typesafe.ai)
(Jev System One model). It can evaluate a document for **human readers**, for
**AI coding agents** that will implement from it, or both (`--reader agent|human|both`, default `agent`).
Each document goes out as one request, and the model answers every question in parallel:

| Reader | Group | Dimensions | Primitive |
| --- | --- | --- | --- |
| human | Writing quality | Clarity, Structure, Concision, Audience fit | Score (5 levels) |
| human | Content substance | Problem & motivation, Design completeness, Alternatives & trade-offs, Risks & rollout | Score (5 levels) |
| agent | AI agent readiness | Actionability, Explicitness, Codebase grounding, Interface precision, Verifiability, Scope boundaries, Self-contained | Score (5 levels) |
| all | Risk flags | Unfinished (TBD/TODO), Unsupported claims, Secrets, PII, Contradictions | Noul (P(yes)) |

Scores are normalized to 0–100 and combined **in code** into a weighted total per
reader (plus writing/substance subtotals for humans). Flags are reported separately
and never averaged into a total. Scores with low model confidence are marked `?`.
With `--reader both`, documents are ranked by the mean of the Human and Agent totals.

![md-eval output for the example docs, default agent reader](docs/sample-output-agent.svg)

With `--reader both`, the same docs ranked for human reviewers and AI agents side by side:

![md-eval output for the example docs with --reader both](docs/sample-output-both.svg)

## Setup

```sh
uv sync
cp .env.example .env   # then add your key from https://console.typesafe.ai
```

## Usage

```sh
uv run md-eval examples/                        # agent-readiness of every .md/.markdown file (recursive)
uv run md-eval rfc-*.md --details               # per-dimension breakdown with the chosen level
uv run md-eval specs/ --reader human             # evaluate for human readers instead
uv run md-eval specs/ --reader both --details    # human and agent scores side by side
uv run md-eval docs/ --audience "SREs new to the payments stack"
uv run md-eval docs/ --weight design_completeness=3 --weight agent_verifiability=2
uv run md-eval docs/ --json results.json        # raw scores, confidences, level probabilities
uv run md-eval docs/ --json - | jq '.[].total'  # JSON on stdout, table on stderr
uv run md-eval docs/ --dry-run                  # show the request + token estimates, no API call
```

The screenshots are real runs, regenerated with `uv run python scripts/screenshots.py`
(this calls the API).

Other options: `--flag-threshold` (default 0.5), `--min-confidence` (default 0.5),
`--concurrency` (default 4), `--model` (default `jev-latest`). The exit code is 1 if
any document failed or was skipped.

## Use with Claude Code

This repo is also a Claude Code plugin marketplace with two plugins:

- **`md-eval`**: the `eval-docs` skill. Ask Claude to "review this spec" or "is this
  doc ready for an agent?" and it runs md-eval and reports the scores.
- **`md-eval-plan-gate`** (optional): a hook that scores every plan before Claude leaves
  plan mode. A plan scoring below 60/100 for agent readiness, or raising a flag, is sent
  back once with its weakest dimensions so Claude can fill the gaps. For example, a
  one-line "add caching" plan scored 23 and was sent back, and a plan with files, steps
  and test commands scored 89 and went straight through.

Setup needs [uv](https://docs.astral.sh/uv/getting-started/installation/) and a TypeSafe
key. Neither plugin needs a separate install; they run md-eval from GitHub through `uvx`.

```sh
claude plugin marketplace add ash-singh/md-eval
claude plugin install md-eval@md-eval
claude plugin install md-eval-plan-gate@md-eval          # optional
echo 'export TYPESAFE_API_KEY=...' >> ~/.zshrc           # from https://console.typesafe.ai
```

Restart Claude Code afterwards so it picks up the key. The plan gate lets plans through
if anything goes wrong (network, oversized plan). Without a key it says so once per
session. Tune it with environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MD_EVAL_PLAN_MIN_SCORE` | `0.6` | Send back plans whose agent total is below this (0-1) |
| `MD_EVAL_PLAN_FLAG_THRESHOLD` | `0.5` | Send back plans with a flag at or above this P(yes) |
| `MD_EVAL_PLAN_MAX_BLOCKS` | `1` | Times a plan can be sent back per session |
| `MD_EVAL_PLAN_MIN_CONFIDENCE` | `0.5` | Mark scores below this confidence as low confidence |

To turn the gate off, run `claude plugin disable md-eval-plan-gate@md-eval`.

## Customizing

All dimensions are defined as data in `src/md_eval/dimensions.py`. Edit
instructions, level descriptions or default weights there. Changing weights or
thresholds doesn't change what the model judged. The `--json` output keeps the raw
per-dimension scores, so you can re-weight them without calling the API again.

## Limits

Jev accepts ~32k tokens of state per request. Documents estimated above ~28k tokens
are skipped with a message rather than truncated, because dimensions like
"Risks & rollout" need the whole document.

## Examples

`examples/` holds sample docs for trying the tool:

| File | Written for | Expected result |
| --- | --- | --- |
| `strong-rate-limiter.md` | Human reviewers | Highest human score. Unsupported claims is borderline (P(yes) about 0.5) because the "~40x the Redis memory" estimate has no calculation |
| `agent-task-retry.md` | An AI coding agent | Highest agent score, low human substance score |
| `thin-caching.md` | Nobody in particular | Low scores on both, Unsupported claims flag |
| `risky-migration.md` | Flag testing | Lowest agent score, Unfinished, Unsupported claims, Secrets and PII flags |

> **All credentials, hostnames, names and contact details in `examples/` are fabricated.**
> They exist only so the Secrets and PII flags have something to detect. None of them
> are real or were ever valid. The notice lives here rather than inside the sample docs
> so it doesn't influence the evaluation. `.github/secret_scanning.yml` excludes
> `examples/` from GitHub secret scanning.
