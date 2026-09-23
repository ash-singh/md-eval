---
name: eval-docs
description: Evaluate Markdown specs, RFCs, task docs and design docs for AI coding agent readiness (and optionally human readability) using the md-eval CLI backed by TypeSafe. Use when asked to review, score, grade or improve a .md doc or spec, or to check whether a spec is ready for an agent to implement.
---

# Evaluate Markdown docs with md-eval

`md-eval` scores Markdown files (`.md`, `.markdown`) with TypeSafe.
Source: https://github.com/ash-singh/md-eval (dimensions are defined in `src/md_eval/dimensions.py`).

If `md-eval` is on PATH, use it. Otherwise run it through uv without installing:
`uvx --quiet --from git+https://github.com/ash-singh/md-eval@v0.4.0 md-eval ...`
(the first run takes a few seconds to build). If `uvx` is missing too, tell the user
to install uv: https://docs.astral.sh/uv/getting-started/installation/

## Run it

```sh
md-eval <files-or-dirs> --json -                 # default: AI coding agent readiness
md-eval <files-or-dirs> --reader both --json -   # also human writing/substance scores
md-eval <files-or-dirs> --reader human --json -  # human readers only
md-eval <files-or-dirs> --reader page --json -   # general writing: reports, write-ups, explainers
```

- Directories are searched recursively; only Markdown files are read.
- `--json -` prints JSON to stdout (the table goes to stderr). Parse stdout.
- Use `--reader both` when the user cares about human reviewers too, not only agents.
- Use `--reader page` for writing that isn't a technical spec, such as a report or write-up
  (for HTML, score a Markdown draft of the text).
- If it fails with a missing API key, tell the user to set `TYPESAFE_API_KEY` (e.g. in
  `~/.zshrc`; get a key at https://console.typesafe.ai). Do not look for or print the key yourself.
- Exit code 1 means some document failed or was too long (`error` field is set).

## Read the JSON

One object per document:

- `totals`: `{"agent": 0-1, "human": 0-1, "page": 0-1}` weighted totals for the chosen reader(s).
- `scores.<dimension>`: `score` (0-1), `confidence` (0-1), `probabilities` per level.
- `flags.<flag>`: P(yes) for `unfinished_content`, `unsupported_claims`, `secrets`,
  `pii`, `contradictions`. `raised_flags` lists those at or above 0.5.
- `low_confidence`: dimensions where the model was unsure. Weigh these less and
  check them by reading the doc yourself.

Agent dimensions: `agent_actionability`, `agent_explicitness`, `agent_grounding`,
`agent_interfaces`, `agent_verifiability`, `agent_boundaries`, `agent_self_contained`.
Human dimensions: `clarity`, `structure`, `concision`, `audience_fit`,
`problem_motivation`, `design_completeness`, `alternatives_tradeoffs`, `risks_rollout`.
Page dimensions: `page_point`, `page_clarity`, `page_structure`, `page_concision`.

## Report

For each document, report the total(s) as 0-100, the two or three lowest-scoring
dimensions, and any raised flags. Treat a raised `secrets` or `pii` flag as urgent.
Rough guide: 80+ ready, 60-79 usable with gaps, below 60 needs revision.
These are model judgments: say so, and don't present them as certain.

## Improve a doc (only when asked)

1. Fix the lowest-scoring dimensions first. For example: low `agent_verifiability`
   → add testable acceptance criteria and test commands. Low `agent_grounding` → name
   files, functions and commands. Low `agent_boundaries` → add scope, non-goals and
   what must not change.
2. Do not invent facts (paths, numbers, decisions) the doc author didn't give. Ask
   the user or leave a clearly marked question instead.
3. Re-run `md-eval` on the edited file and show the before/after totals.
