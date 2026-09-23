---
name: decide-options
description: Get an independent, structured second opinion on a choice between 2 or more concrete options (designs, libraries, fixes, migration strategies) using `md-eval decide`, backed by TypeSafe Jev. It scores each option per criterion, checks hard constraints, and says whether to proceed or ask the user. Use at your judgment when a choice is consequential, hard to reverse, or you notice you are anchored on your first idea.
---

# Decide between options with md-eval decide

`md-eval decide` sends a decision to TypeSafe's Jev model and returns scores, vetoes and a
status. Jev sees only the JSON you give it, not this conversation, so it is not anchored on
whichever option you proposed first. Code, not the model, combines the answers.

When to use it is your call. It adds a few seconds and one API request. It is most useful
when options are genuinely close, when a wrong choice is costly, or when you want to check
whether the choice hinges on something only the user knows. It is not worth it for choices
that are trivial, easily reversed, or dictated by the codebase.

## What Jev can and cannot do

Jev weighs the facts you state. It does not reliably know specialist facts. In a test, it
vetoed the correct Postgres migration (a plain `ADD COLUMN ... DEFAULT`, safe since
PostgreSQL 11) until the context stated that fact, and then it chose it with 0.97
confidence. So:

- **Put the facts that settle the decision in `context`**: versions, sizes, traffic, what
  already exists, and behavior you know that the options depend on. Include only facts you
  know or verified in the codebase. Never invent them.
- Treat its output as a check on your reasoning, not as a source of truth. If it
  contradicts something you know, say so and explain, rather than deferring to it.

## Run it

If `md-eval` is on PATH, use it. Otherwise run it through uv without installing:
`uvx --quiet --from git+https://github.com/ash-singh/md-eval@v0.4.0 md-eval decide ...`

Write the decision to a temporary JSON file (or pipe it on stdin with `-`), then:

```sh
md-eval decide decision.json --json -     # JSON on stdout, table on stderr
md-eval decide decision.json --dry-run    # show the request and token estimate, no API call
```

If it fails with a missing API key, tell the user to set `TYPESAFE_API_KEY` (get a key at
https://console.typesafe.ai). Do not look for or print the key yourself. Exit code 2
means invalid input (the message says what to fix) or a missing key.

## Input

```json
{
  "problem": "What must be solved, with the measurable goal if there is one",
  "context": "Facts that bear on the choice: versions, scale, existing systems, known behavior",
  "constraints": ["Hard requirements an option must not break, phrased as must / must not"],
  "options": [
    {"id": "short_id", "summary": "The concrete approach in one or two sentences", "details": "optional"}
  ],
  "criteria": [
    {"id": "short_id", "description": "What good looks like on this dimension", "weight": 2}
  ]
}
```

- **Options**: 2 or more, each a concrete approach (mechanism, not just a goal). Include the
  realistic alternatives, including ones you are not inclined toward and doing nothing if
  that is viable. Do not invent options the situation doesn't support.
- **Constraints** are vetoes: an option judged to break one is ruled out regardless of its
  score. Only list true requirements. Phrase them as restrictions ("must not add new
  infrastructure"). Allowances such as "data may be 60 s stale" are better stated in
  `context`, since a constraint phrased as a permission can be misread as a requirement.
- **Criteria** are what you trade off, each judged separately. Weights (default 1) are
  applied in code. Weight what the user said matters most.
- The only hard limit is size: about 28k tokens of state. Keep summaries short.

## Read the result

`status` tells you what to do, and `reasons` explains it:

| status | Meaning | What to do |
| --- | --- | --- |
| `proceed` | A clear, viable winner, and the holistic pick agrees | Go with `recommendation`. Mention the key scores briefly when you explain the choice |
| `ask_user` | A viable leader, but the result is fragile or needs the user's input | Ask the user, presenting `recommendation`, the runner-up and the `reasons` |
| `clarify_options` | Some option is too vague to judge | Make the options concrete (or drop one) and run again |
| `no_viable_option` | Every option likely breaks a constraint | Tell the user. Look for another option or check whether a constraint is really hard |

Other fields: `ranking` (each option's `total` 0-1, per-criterion `score` and `confidence`,
`vetoes` as P(violates) per constraint, `vague`, `viable`), `holistic` (Jev's single overall
pick with probabilities, including `none_fit`), `needs_user_input` (P), and the
`thresholds` and `weights` used.

A single veto near the threshold (around 0.5-0.6) is weak evidence. Check it against what
you know before ruling an option out. These are model judgments: don't present them as
certain, and don't run the same decision repeatedly hoping for a different answer.
