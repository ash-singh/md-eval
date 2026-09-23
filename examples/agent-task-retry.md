# Task: add retry with exponential backoff to `PaymentsClient.charge`

## Scope

- Change only `src/payments/client.py` and add tests in `tests/payments/test_client_retry.py`.
- Do NOT change the public signature of `PaymentsClient.charge` or any other method.
- Do NOT add new dependencies; use the standard library (`time`, `random`).
- Follow the existing logging pattern: `logger = logging.getLogger(__name__)`.

## Required behavior

`PaymentsClient.charge(amount_cents: int, currency: str, idempotency_key: str) -> ChargeResult`

1. On `PaymentsTimeoutError` or an HTTP 502/503/504 response, retry the call.
2. Retry at most 3 times (4 attempts total).
3. Before retry `n` (1-based), sleep `min(0.2 * 2 ** (n - 1), 2.0)` seconds plus uniform jitter in `[0, 0.1)`.
4. Send the same `idempotency_key` on every attempt.
5. Do not retry on HTTP 4xx or `PaymentsDeclinedError`; raise immediately.
6. After the final failed attempt, raise the last exception unchanged.
7. Log each retry at WARNING: `"charge retry %d/3 after %s"` with the attempt number and exception class name.

## Acceptance criteria

- `uv run pytest tests/payments -q` passes.
- New tests (use `monkeypatch` to replace `time.sleep` and `random.uniform`):
  - succeeds on the 3rd attempt after two 503s; `sleep` called with 0.2 and 0.4 (+ jitter 0).
  - raises `PaymentsTimeoutError` after 4 attempts; `sleep` called 3 times.
  - HTTP 400 raises immediately with no `sleep` calls.
  - `PaymentsDeclinedError` raises immediately with no `sleep` calls.
  - all attempts send an identical `idempotency_key`.
- `uv run ruff check src/payments` reports no new errors.
