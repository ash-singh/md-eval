# RFC-042: Per-tenant rate limiting for the public API

**Authors:** Platform team · **Status:** In review · **Audience:** backend engineers and SREs

## Problem

Our public API applies a single global limit of 5,000 req/s across all tenants. In the last
quarter, three incidents (INC-311, INC-327, INC-340) were caused by one tenant's batch job
saturating that budget, which returned 429s to every other tenant for 6–22 minutes. Support
logged 140 tickets from affected customers. Enterprise contracts renewing in Q3 include an
availability SLA we cannot meet while any tenant can starve the rest, so this needs to ship
before renewals begin.

## Goals and non-goals

- Goal: isolate tenants so one tenant exceeding its quota cannot degrade others.
- Goal: quotas configurable per plan and per tenant override, without a deploy.
- Non-goal: billing for overages (tracked separately in RFC-045).

## Proposal

Replace the global limiter in the API gateway with a token-bucket limiter keyed by tenant ID.

- **Storage:** bucket state in the existing Redis cluster, key `rl:{tenant_id}`, holding
  `tokens` (float) and `updated_at` (ms). A Lua script performs refill-and-take atomically.
- **Configuration:** a `rate_limits` table (`tenant_id`, `plan`, `rps`, `burst`) cached in the
  gateway for 30s. Plan defaults: Free 10 rps / burst 20, Pro 100 / 200, Enterprise 1,000 / 2,000.
- **Response:** on rejection return 429 with `Retry-After` and `X-RateLimit-Remaining` headers.
- **Redis unavailable:** fail open, with a local in-memory limiter at 50% of the tenant's quota
  so a Redis outage cannot take the API down.

The gateway's p99 overhead was measured at 0.4 ms per request in a load test of 20k req/s
against a staging Redis (p50 0.2 ms, p99 0.4 ms, p99.9 1.1 ms; full results: https://wiki.example.com/platform/rl-benchmark).

## Alternatives considered

1. **Raise the global limit.** Cheap, but only delays the problem; a single tenant can still
   consume any fixed shared budget.
2. **Sliding-window log.** More precise than token buckets but stores one entry per request;
   at our volume that is ~40x the Redis memory. Rejected.
3. **Limit at the load balancer (Envoy local rate limit).** No shared state across the 12
   gateway pods, so effective limits would vary with pod count. Rejected.

Token buckets trade some precision at bucket boundaries for O(1) memory per tenant, which we
consider acceptable.

## Risks and rollout

- **Risk:** misconfigured quotas block legitimate traffic. Mitigation: shadow mode first.
- **Risk:** Redis latency spikes. Mitigation: 5 ms timeout, then fall back to the local limiter.
- **Rollout:** (1) deploy in shadow mode logging would-be rejections for one week; (2) review
  top-affected tenants with account managers; (3) enforce for Free, then Pro, then Enterprise,
  one week apart.
- **Rollback:** a feature flag `rate_limit.enforce` reverts to the global limiter instantly.
- **Monitoring:** dashboards for rejections per tenant and limiter latency; alert if rejections
  exceed 1% of any tenant's traffic.
- **Testing:** unit tests for the Lua script, load tests in staging, and a chaos test that
  kills Redis during load.
