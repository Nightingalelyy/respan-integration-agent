# v0 gateway readiness preflight — gap analysis and plan

- Audit date: 2026-08-22
- Integration-agent baseline: `e7b3c4342ee7066c629f2813a7f0dd4f5be85c08`
  plus the current uncommitted trusted-v0a/backend-gate worktree
- Backend snapshot reviewed: `a4ca953db45f5cda2d06049b71251bf3a4549c6b`
- Respan SDK/CLI snapshot reviewed: `6eee9fdf3c5917da4f77c83cde8425ea0627d369`
- Documentation snapshot reviewed: `f7998a5802cdefcd0a6e79620536ab9eed49aa67`
- Mode: source review and local, credential-free probes only
- Status: **plan only — no gateway-preflight runtime or backend implementation in this run**

## Decision

The next unchecked README item is not safely implementable as a few reads inside
`runner._preflight`. The repository needs two readiness decisions:

1. **Orchestration readiness on every run** — the Respan key, exact
   Anthropic-compatible operation/model, account limits, and at least one permitted
   funding/credential route must be ready before Claude spends a turn.
2. **Target readiness for `product=gateway|both`** — every exact provider/model route
   requested for the target repository must be ready using the funding mechanism the
   user selected.

The current backend has useful billing, catalog, and integration read APIs, but no one
authenticated, non-billable API answers the actual question: **can this key route this
exact operation/model now using the requested credits or BYOK path?** Composing the
existing reads would create false passes and false failures because they do not share
the gateway hot path's key, plan, budget, TEAM-plan, route-resolution, and credential
selection semantics.

Recommended next implementation: first add a sanitized, dry-run
`POST /api/gateway/readiness/` backend contract that reuses the real gateway decision
logic without contacting an upstream model. Then add a fail-closed client and immutable
route plan to this repository. Do not mark the README checkbox complete until a fresh
smoke proves that the readiness pass happens before checkout/model execution.

## Current behavior

The outer call position is useful, but the check itself is empty:

```text
run_session
  -> _preflight
       -> reject an empty RESPAN_API_KEY
       -> validate the bundled skill
       -> gateway/both: pass
  -> checkout
  -> run_agent(model="sonnet", base_url=".../api")
       -> Claude sends every product through ".../api/anthropic/"
```

Source evidence:

- [`runner.py:43-65`](agent/src/respan_integration_agent/runner.py#L43) checks only a
  nonempty key and the skill, and enters the gateway TODO only for `gateway|both`.
- [`agent.py:215-225`](agent/src/respan_integration_agent/agent.py#L215) supplies the
  Respan key and Anthropic gateway base to Claude for every product.
- [`agent.py:257-274`](agent/src/respan_integration_agent/agent.py#L257) hides the
  orchestration model in a `run_agent` default and validates the endpoint only after
  `_preflight` and checkout.
- [`config.py:57-71`](agent/src/respan_integration_agent/config.py#L57) represents a
  funding mode and provider names, but no exact models, gateway operation, or fallback
  routes.

A local probe with a definitely invalid key returned normally from `_preflight` for
tracing, gateway-with-credits, and gateway-with-BYOK requests. No network request was
made by that probe.

## Confirmed gaps

| ID | Priority | Gap | Consequence |
|---|---:|---|---|
| PF-01 | P0 | Gateway code in `_preflight` is a literal `pass`. | Invalid auth, no funds, or no credential route is discovered only after a model run starts. |
| PF-02 | P0 | The conditional scope is target-product scope, while the orchestration agent always uses the gateway. | A tracing-only run skips the exact gateway dependency it immediately spends. |
| PF-03 | P0 | Preflight does not receive the validated origin, protocol, model, or session dollar cap. | It cannot approve the same route that `run_agent` later executes; defaults can drift after approval. |
| PF-04 | P0 | No authoritative non-paid readiness API exists. | Credit/catalog/integration projections can disagree with the actual gateway decision. |
| PF-05 | P0 | Target gateway intent is not exact: `providers` has no models or operation, and fallbacks are a boolean. | Exact credits/BYOK/model readiness is unrepresentable and therefore unverifiable. |
| PF-06 | P1 | Funding source and route usability are conflated. | A successful request can use saved BYOK when `funding=credits`, or managed credentials when the user intended BYOK. |
| PF-07 | P1 | There is no typed client, immutable report, dependency injection boundary, or redacted error taxonomy. | Tests cannot prove call order or failure behavior; raw HTTP details could reach CLI errors. |
| PF-08 | P1 | There are no preflight runner, transport, schema, or all-product tests. | A future check can silently regress to fail-open or run after clone/model execution. |
| PF-09 | P1 | `product=both` selects the tracing reference while the bundled skill requires tracing and gateway setup to be separate passes. | Passing readiness would not make the combined onboarding path semantically correct. This adjacent gap remains separately open. |
| PF-10 | P2 | The old gap report still describes the pre-change signature and strict-config state. | Reviewers can mistake historical evidence for the current workspace behavior. |

## Why the existing backend reads are not acceptance

| Existing surface | Useful fact | Missing fact / false-decision risk |
|---|---|---|
| `GET /api/credit-transactions/summary/` | Returns the caller organization's display balance. | Display balance is not runtime `has_credits`: it has different fallback behavior, omits API-key usage/spend limits and customer budgets, and does not model TEAM-plan access or the exact route. |
| `POST /api/models/list/` with `available_to_user=true` | Narrows the catalog to credit-capable and organization-configured models. | It does not prove current funds, the exact native operation/alias, credential completeness/validity, or which credential source the gateway will select. Authentication is optional for the catalog surface. |
| `GET /llm_models/provider_integrations/` | Shows customer-visible provider integrations, activation, and model allow/exclude scopes. | It excludes managed deployments, returns broader masked integration data than preflight needs, and does not compile the endpoint-specific route or contact the upstream provider. |
| `/ready` | Reports service-level Redis/PostgreSQL readiness. | It is not caller-scoped and knows nothing about the key, organization, credits, BYOK, model, or route. |
| `POST /api/validate-api-key/` | Can make a real provider request. | It performs an LLM completion, can incur provider cost, creates logs, and still is not a generic funding-source attestation. It is not the default preflight contract. |

There is also consumer-contract drift: the sibling Respan CLI states that no balance
endpoint exists and uses a paid `gpt-4o-mini` completion as its gateway proof, while the
reviewed backend source contains a credit-summary route. That is another reason to
freeze and test one purpose-built public contract rather than infer readiness from
internal/dashboard APIs.

## Acceptance contract

### 1. Build one immutable plan

Create one `PreflightPlan` before any external I/O. It owns:

- the already allowlisted official Respan origin;
- the exact orchestration SDK model and the exact gateway model/operation it resolves
  to;
- the orchestration funding policy (`any` for the current agent unless product policy
  deliberately pins managed credits or BYOK);
- the same maximum session budget passed to Claude;
- an exact target route list for `gateway|both`, including provider, model, operation,
  and required funding source;
- explicit fallback routes rather than an unqualified boolean.

`run_agent` must consume this same plan. It must not reintroduce independent default
values after preflight approves a route.

### 2. Scope the two gates correctly

| Role | Products | Required result |
|---|---|---|
| Orchestration | tracing, gateway, both | Key/account is allowed; exact Anthropic operation/model resolves; key/plan/budget checks are ready; a permitted managed or customer credential route exists. |
| Target managed credits | gateway, both | Every exact target route resolves to a credits-capable managed route and the requested session funding threshold is ready. BYOK must not silently satisfy it. |
| Target BYOK | gateway, both | Every exact target route resolves to an active customer credential whose provider/model scopes admit the route. Managed credits must not silently satisfy it. |
| Target | tracing | No target-gateway readiness check. |

Missing, contradictory, malformed, ambiguous, unavailable, or unknown state fails
closed. Preflight has no warning-and-continue path.

### 3. Add a backend dry-run endpoint

Proposed endpoint:

```http
POST /api/gateway/readiness/
Authorization: Bearer <RESPAN_API_KEY>
Content-Type: application/json
```

Proposed request shape:

```json
{
  "schema_version": "respan.gateway-readiness/v1",
  "checks": [
    {
      "purpose": "orchestration",
      "operation": "anthropic.messages",
      "model": "<exact-gateway-model>",
      "provider": "anthropic",
      "funding": "any",
      "required_credit_usd": 1.0
    }
  ]
}
```

The backend must use the same logic as inference for:

- API-key, organization, subscription, suspension, key-usage, spending-limit, and
  customer-budget decisions;
- model alias normalization and endpoint-specific route compilation;
- credit eligibility and managed-credential availability;
- customer/managed credential bucket selection and precedence;
- active integration provider/model allow/exclude rules;
- required credential-field presence and stored endpoint SSRF validation.

Dry-run constraints:

- no upstream provider request;
- no token/provider spend;
- no request log, credit ledger entry, usage counter, budget event, or onboarding
  completion event;
- no secret decryption unless the same check cannot be made from safe metadata;
- never return raw or masked credentials, integration IDs, organization IDs, exact
  balances, authorization data, or internal exception strings;
- infrastructure uncertainty is `unknown`, not zero credits and not a pass;
- the result is point-in-time evidence, not a reservation; the real gateway call still
  rechecks all limits.

Proposed safe response shape:

```json
{
  "schema_version": "respan.gateway-readiness/v1",
  "ready": true,
  "checks": [
    {
      "purpose": "orchestration",
      "operation": "anthropic.messages",
      "requested_model": "<exact-gateway-model>",
      "resolved_model": "<resolved-model>",
      "provider": "anthropic",
      "credential_source": "managed",
      "funding_requested": "any",
      "funding_satisfied": true,
      "key_ready": true,
      "limits_ready": true,
      "route_ready": true,
      "status": "ready",
      "reason_codes": []
    }
  ]
}
```

HTTP behavior:

- `200` for a well-formed readiness decision, including deterministic
  `ready=false` results;
- `400/422` for malformed or unsupported route requirements;
- `401` for a missing, invalid, expired, or revoked Respan key;
- `403` for a blocked account or forbidden readiness scope;
- `429` for request throttling, with a bounded `Retry-After`;
- `503` when a required dependency makes the decision unknown.

Stable reason codes should distinguish at least auth, account/subscription, key usage,
key spend, customer budget, managed funding, BYOK configuration, model, provider,
operation, route ambiguity, and dependency uncertainty.

### 4. Define the honest limit of a dry run

The endpoint can prove Respan-side configuration and routing facts. It cannot prove that
an uploaded provider key has not since been revoked upstream or that the provider will
serve the model at the next instant. No universal harmless provider probe exists.

If stronger evidence is required, run one **separately labeled optional paid canary** on
the exact operation/model with one output token, no streaming, and no tools. Cache it
briefly by a non-reversible credential fingerprint plus route and invalidate it on
credential rotation. Do not run it implicitly on every session, do not call it
non-billable, and do not use a generic model canary as evidence for a different route.

The v0 checklist gate itself should use the backend dry run. The subsequent real agent
and target smoke calls remain the live provider acceptance evidence.

## Agent-side design

### Modules and data

- Add `gateway_preflight.py` with:
  - immutable `PreflightPlan`, `RouteRequirement`, `PreflightCheck`, and
    `PreflightReport` values;
  - an injectable `GatewayReadinessBackend` protocol;
  - a pinned-origin `RespanGatewayReadinessClient`;
  - a pure, strict response evaluator;
  - safe typed exceptions with stable public codes.
- Centralize orchestration protocol/model/budget constants. Resolve the current mutable
  `sonnet` alias to a deliberately pinned gateway model before freezing the plan.
- Replace `GatewayConfig.providers` with exact route objects, or add required exact
  routes and deprecate the ambiguous provider-only field. When fallbacks are enabled,
  require an ordered, nonempty fallback route list.
- Add the sanitized `PreflightReport` to `SessionResult` and smoke evidence.
- Keep the trace-only [`platform.py`](agent/src/respan_integration_agent/platform.py)
  boundary separate; share only a small hardened HTTP utility if that reduces duplicate
  origin, redirect, proxy, timeout, and response-size code.

### Required call order

```text
strict request validation
-> build immutable route plan
-> validate pinned origin, key syntax, skill, Git/Claude runtime, and temp-root writability
-> orchestration readiness (all products)
-> target-route readiness (gateway/both only)
-> return sanitized PREFLIGHT_PASS
-> checkout
-> run_agent with the exact approved plan
-> patch, target, and backend trace gates
```

Any preflight failure must leave checkout, `run_agent`, commit, push, and PR call counts
at zero.

### Transport and failure policy

- TLS only and exact official origin for v0.
- No redirects, environment proxies, cookies, ambient auth, or URL credentials.
- Authorization only on the exact allowlisted readiness path.
- Strict JSON content type/schema and a small response-size cap.
- One shared 20-second wall deadline, at most three attempts.
- Retry only timeouts/network failures, `429`, and `500/502/503/504`; honor a valid
  `Retry-After` only within the remaining deadline.
- Fail immediately on redirect, auth, permission, config, route, funding, and schema
  failures.
- Never include the API key, authorization header, raw response body, organization or
  integration identifiers, credential metadata, or chained HTTP exception in public
  errors.

Suggested stable agent error families:

- `P_CONFIG_*`
- `P_AUTH_INVALID`, `P_AUTH_FORBIDDEN`
- `P_MODEL_NOT_FOUND`, `P_ROUTE_AMBIGUOUS`, `P_OPERATION_UNSUPPORTED`
- `P_CREDITS_NOT_READY`, `P_BYOK_NOT_READY`, `P_LIMIT_NOT_READY`
- `P_RATE_LIMITED`, `P_SERVICE_UNAVAILABLE`, `P_TIMEOUT`, `P_TRANSPORT`
- `P_SCHEMA_UNSUPPORTED`, `P_REDIRECT`

## Test plan

### Backend contract tests

- key states: valid, missing, invalid, expired, revoked, blocked organization;
- managed funding: ready, zero/exhausted, missing subscription, TEAM-plan path,
  display/Redis/PG disagreement, dependency unknown;
- API-key usage/spend and customer-budget boundaries;
- exact model aliases, deprecated/missing models, provider mismatch, unsupported
  operation, duplicate/ambiguous route;
- BYOK inactive/missing/incomplete credentials, allow/exclude model scopes, wrong
  provider, environment mismatch, invalid stored endpoint;
- strict credits versus BYOK selection and `any` source reporting;
- proof that the endpoint creates no log/ledger/counter/event and makes no upstream
  request;
- response allowlist proving no secret, identifier, balance, or exception leakage.

### Agent offline tests

- request method/path/body/header and exact route encoding;
- official-origin pin, TLS, no proxy, no redirect, response cap, JSON/schema validation;
- status/error taxonomy, retry matrix, `Retry-After`, and shared deadline;
- raw, encoded, and transformed secret redaction in every exception/report path;
- orchestration exactly once for tracing/gateway/both;
- target zero times for tracing and once per exact route for gateway/both;
- strict funding-source mismatch rejection;
- identity proof that the approved model/protocol/origin/budget is what `run_agent`
  receives;
- runner spies proving `PREFLIGHT_PASS < checkout < run_agent` and zero downstream calls
  on every failure;
- CLI safe one-line failures and stable exit classes;
- smoke refusal to emit overall pass without a passing, pre-agent report.

### Protected live acceptance

1. Probe only the deployed readiness schema with the existing smoke key; sanitize and
   record paths, status classes, schema version, and booleans.
2. Run one preflight-only tracing request and one gateway/BYOK or gateway/credits fixture
   without checkout or generation.
3. Run the trusted v0a smoke once. Require the preflight timestamp to precede checkout
   and agent start, then retain all existing patch, target, and exact backend trace gates.
4. Confirm the evidence contains no key, account/provider identifiers, raw response,
   credential metadata, or exact balance.
5. Mark the README checkbox only after this fresh protected run passes.

Suggested sanitized evidence:

```json
{
  "schema_version": "respan-integration-agent-preflight/v1",
  "verdict": "PREFLIGHT_PASS",
  "paid_canary_performed": false,
  "started_at": "...",
  "finished_at": "...",
  "checks": [
    {
      "purpose": "orchestration",
      "operation": "anthropic.messages",
      "model": "<pinned-model>",
      "funding": "any",
      "status": "ready",
      "attempts": 1
    }
  ]
}
```

## Implementation sequence

1. **Backend contract** — implement and publish the dry-run readiness endpoint using
   shared gateway evaluators; add schema/security/no-side-effect tests.
2. **Exact config and plan** — introduce route objects and centralize the orchestration
   model/protocol/origin/budget; reject ambiguous gateway configs.
3. **Agent client** — add the hardened injectable client, pure evaluator, typed errors,
   bounded retries, and sanitized report.
4. **Runner integration** — execute orchestration and target checks before checkout and
   pass the same immutable plan to `run_agent`.
5. **Evidence and CLI** — add report serialization, safe user remediation, and smoke
   gating.
6. **Verification** — run the offline matrix, build/install/Ruff checks, then one fresh
   protected live smoke and exact backend trace acceptance.

## Definition of done

- Every product performs orchestration readiness before checkout/model execution.
- `gateway|both` also verifies every exact target route with the declared funding
  source; ambiguous provider-only config is rejected.
- The readiness decision uses the same Respan-side auth, limit, funding, credential,
  and route semantics as inference, while producing no provider call or usage record.
- Unknown or malformed state fails closed with a stable, redacted error.
- The plan approved by preflight is exactly the plan executed by the agent.
- Offline tests prove ordering, role/product coverage, transport safety, schema strictness,
  retries/deadlines, source selection, and secret absence.
- A fresh protected smoke records a sanitized `PREFLIGHT_PASS` before agent start and
  still passes the existing patch, target-runtime, and exact trace-content gates.
- Only then is README's gateway-preflight checkbox checked.

## Explicit non-goals for this step

- PR creation/push/idempotency (`v0b`)
- container/VM isolation and private-repository credential redesign
- proving upstream provider uptime or credential validity without a labeled paid canary
- solving the separate `product=both` one-pass skill/prompt contradiction
- replacing gateway-specific patch semantic verification
