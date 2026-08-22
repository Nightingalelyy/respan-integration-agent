# v0 gateway preflight implementation evidence — 2026-08-23

## Result

The integration-agent side of the gateway readiness preflight is implemented on
`v0-checklist-implementation`, but the README checkbox remains open because the
authoritative backend contract is not deployed.

A protected, non-inference request reached the official Respan API and returned:

```json
{"error":{"attempts":1,"code":"P_REQUEST_REJECTED","status_code":404},"live_preflight":"failed"}
```

The real smoke entrypoint then failed closed with exit code `4` before checkout or model
execution and emitted only this sanitized evidence:

```json
{"preflight_error":{"attempts":1,"code":"P_REQUEST_REJECTED","status_code":404},"schema_version":"respan-v0-smoke-evidence/v1","verdict":"GATEWAY_PREFLIGHT_FAILED"}
```

No response body, API key, authorization header, account data, credential metadata, or
provider inference output was printed or recorded.

## Implemented agent-side contract

- `GatewayConfig` now requires exact ordered primary and fallback routes containing a
  closed operation, provider, and model. Credits require an explicit positive bounded
  reserve; BYOK requires zero Respan credit reserve.
- The mutable `sonnet` alias was replaced by the full
  `claude-sonnet-4-20250514` orchestration model name.
- One immutable `PreflightPlan` contains the exact official execution base, pinned agent
  model, agent dollar budget, and every orchestration/target route.
- Every route has a stable check ID. The backend must echo the exact check identity and
  `required_credit_usd`; credits can pass only with a managed credential and BYOK only
  with a customer credential.
- `RespanGatewayReadinessClient` sends one bounded `POST` to the pinned official
  `/api/gateway/readiness/` path using verified direct TLS, no ambient proxy, no redirect,
  a strict JSON schema/content type, a response-size ceiling, and a shared deadline.
- Only network failures, `429`, and `500/502/503/504` retry within the deadline. Public
  errors contain stable codes and bounded numeric fields only.
- Readiness runs for every product before checkout. `gateway` and `both` additionally
  check every exact primary/fallback target route. A malformed, negative, stale, future,
  or model-substituting report cannot enter checkout.
- The same approved base, pinned model, and budget are passed to `run_agent`.
- `SessionResult`, the CLI, and the smoke evidence carry only the sanitized preflight
  report. Smoke success requires timestamp proof that preflight completed before the
  agent began.

## Credential-free validation

- Full agent test suite: **378 passed**
- Gateway preflight transport/schema suite: **132 passed**
- Ruff: **passed**
- `git diff --check`: **passed**

The tests cover exact wire identity and funding thresholds, credits/BYOK separation,
all-product routing, primary/fallback ordering, zero downstream calls on failure, model
substitution, hostile error values, redirects, content types, malformed JSON, response
limits, retry policy, late-success/late-error deadline cases, CLI redaction, and smoke
evidence ordering. CLI coverage also proves rejected credential-like config and
unexpected exception text are never reflected.

## Remaining backend blocker

The deployed backend and reviewed OpenAPI do not expose
`POST /api/gateway/readiness/`. Existing balance, model-catalog, and provider-integration
reads cannot replace it because they do not share the gateway inference path's complete
auth, subscription, budget, funding-source, credential-selection, and route-resolution
decision.

To complete the checklist item:

1. Implement and publish `respan.gateway-readiness/v1` in the backend using the shared
   inference evaluators without contacting a provider or creating usage/log/ledger side
   effects.
2. Add backend auth, schema, funding-source, route, dependency-unknown, secret-allowlist,
   and no-side-effect tests plus the public OpenAPI contract.
3. Deploy the endpoint and rerun the protected v0 smoke.
4. Require `PREFLIGHT_PASS` before the existing patch, target-runtime, and exact backend
   trace gates all pass.
5. Only then check the README item.

An optional paid one-token canary could prove upstream route usability, but it cannot
prove that the user-requested credits-versus-BYOK source funded the request. It is not a
substitute for this checklist contract.
