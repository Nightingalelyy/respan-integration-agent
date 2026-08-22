# v0 backend trace gate implementation plan

- Planning date: 2026-08-22
- Checklist item: automate exact backend trace-content inspection inside the v0a smoke harness
- Status: **implemented and live-verified on 2026-08-22**
- Scope: the trusted `python-openai-auto-smoke` fixture only

The fresh credentialed smoke reached `BACKEND_VERIFIED_PASS` with exact agent trace
`c6b4cf7687f68682f24dc897c1a27ec5` and target trace
`e856a225d5b70c47274fe3b7c3f40f90`; see
[`V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md`](V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md).
The checklist item is complete for the fixed trusted profile. This work deliberately does
not include gateway readiness preflight, untrusted-repository isolation, private GitHub
credentials, dependency locking, or v0b PR delivery.

## 1. Decision summary

Use the supported Respan REST read APIs directly. Do not make hosted MCP, dashboard UI
traffic, or a broad trace search a runtime dependency.

The harness already knows the agent trace ID and creates unique agent and target run
markers. The backend gate will use both identities:

1. Find each trace through an exact `metadata__run_id` filter in the recorded smoke time
   window.
2. Require exactly one result for each marker.
3. Require the agent marker result to equal the trace ID returned by `run_agent()`.
4. Lock the target marker result to its discovered trace ID.
5. Retrieve and validate the complete trace, flat span inventory, and every full span
   record for those two exact IDs.

The exact-marker request uses explicit UTC `start_time`/`end_time`, `page=1`, a small
`page_size`, no environment filter, and this body:

```json
{
  "filters": {
    "metadata__run_id": {
      "operator": "",
      "value": ["<run_id>"]
    }
  }
}
```

The flat inventory uses the analogous exact `trace_unique_id` filter. Do not filter by
top-level environment because its current platform projection is a documented mismatch.

The public-cloud read origin will be a separate pinned constant:

```text
https://api.respan.ai
```

The relevant read operations are:

```text
POST /api/traces/list/                       exact marker lookup
GET  /api/traces/{trace_unique_id}/          aggregates plus recursive span tree
POST /api/request-logs/list/                 exact flat inventory for a trace
GET  /api/request-logs/{unique_id}/           full stored span detail
```

All requests use `Authorization: Bearer <RESPAN_API_KEY>`. The API key must never be put
in a URL, process argument, exception, fixture, snapshot, or evidence file.

### Source evidence used for this plan

- Respan OpenAPI commit `f7998a5802cd` documents exact trace retrieval, bearer API-key
  authentication, recursive `span_tree`, 404/429 responses, and full span detail in
  `../respan-docs/fern/apis/openapi/openapi.json`.
- Respan backend commit `a4ca953db45f` implements storage-enriched API-key trace detail
  and a 60-request/minute default trace-detail limit in
  `../respan-backend/clickhouse/views/traces.py`.
- Backend e2e tests poll the detail endpoint conditionally rather than sleeping once;
  local ingestion helpers report typical visibility in roughly 5-15 seconds and use a
  60-second ceiling. There is no documented production visibility SLA.
- The deployed pagination implementation treats `count` as the current page length for
  API-key callers. The gate must follow a validated `next` link instead of trusting
  `count` as the total.

Before implementation, make one read-only schema probe against the two already-approved
trace IDs and save only hand-sanitized test fixtures. No live API request was made during
this planning run.

## 2. Why all four backend views are required

The trace-detail endpoint is the authoritative tree and aggregate view, but the approved
agent trace already demonstrates projection differences:

- the trace aggregate has the correct token totals;
- the tree can show zero usage/cost on the Claude agent child;
- the full record carries the canonical `metadata.response_cost` and token fields;
- the aggregate cost is currently zero despite the SDK terminal result reporting cost.

Therefore the gate must cross-check:

| View | Purpose |
|---|---|
| Exact marker trace list | Prove unique server-side run-marker indexing and discover the target ID |
| Exact trace detail | Prove aggregates, one connected tree, and recursive hierarchy |
| Exact trace span list | Prove the complete flat inventory and detect tree/list omissions |
| Full detail for every span | Prove canonical content, tools, usage, cost metadata, errors, and redaction |

The set of full-record IDs, flat-list IDs, and flattened-tree IDs must agree exactly.

## 3. Proposed code boundaries

### `agent/src/respan_integration_agent/platform.py`

Add a read-only backend adapter with an injectable transport:

- `TraceBackend` protocol;
- `RespanPlatformClient` implementation;
- `list_traces_by_run_id(...)`;
- `retrieve_trace(...)`;
- `list_spans_by_trace_id(...)`;
- `retrieve_span(...)`.

Use a narrow standard-library HTTPS transport to avoid an undeclared client dependency.
Build it with verified TLS, no ambient proxy configuration, no cross-host redirects,
explicit connect/read timeouts, a response-size ceiling, and a pinned official origin.
Validate and percent-encode all path identifiers. Follow pagination only after validating
that the next URL retains the official scheme, host, and expected endpoint path.

The adapter should accept additive response fields while strictly validating the types
and presence of the core fields used by the gate.

### `agent/src/respan_integration_agent/trace_gate.py`

Keep pure normalization and policy separate from HTTP and from patch validation:

- `AgentTraceExpectation`;
- `TargetTraceExpectation`;
- `PollingPolicy`;
- `TraceObservation` and `SpanObservation`;
- `TraceCheck` with `pass`, `warn`, or `fail` severity;
- `TraceGateReport` containing only sanitized summaries;
- `poll_and_verify_smoke_traces(...)`.

Inject the monotonic clock, sleeper, and transport so every retry/deadline path is tested
without real sleeps or network calls.

### `scripts/run_v0_smoke.py`

Wire the gate only into the deterministic smoke harness:

1. retain all current local patch, replay, install, and target-runtime gates;
2. after target success, poll both backend traces under one deadline;
3. emit exit zero only after both backend contracts pass;
4. replace `LOCAL_AND_GATEWAY_PASS_BACKEND_INSPECTION_PENDING` with a versioned
   `BACKEND_VERIFIED_PASS` evidence record;
5. on failure, emit trace IDs/links and safe check codes, never raw trace bodies.

Do not add this polling to general `run_session()` yet. A general onboarding session does
not execute a deterministic target and cannot construct the second trace contract.

## 4. Polling and convergence policy

Use one 240-second wall-clock deadline shared by the agent and target traces. The initial
120-second proposal was tested against the two approved, already-indexed traces and was
too short: the complete two-observation plus 12-detail read converged in 164.88 seconds.
The 240-second fixed harness ceiling leaves bounded ingestion headroom without making this
an onboarding questionnaire field. Connect/read calls are separately capped at 10/30
seconds, and the gate checks the shared deadline before and after each backend operation;
it never accepts after the deadline, although an in-flight synchronous call can delay the
failure report by its socket timeout.

- Poll both traces in each cycle so one does not consume a separate full deadline.
- Backoff schedule: 1, 2, 4, then at most 5 seconds between cycles.
- Never sleep past the remaining deadline.
- Respect a bounded `Retry-After` on 429 responses.
- The capped cadence stays below the backend's current 60-request/minute trace-detail
  limit even while checking both traces.
- Retry 404, empty results, missing end time, incomplete pagination, count disagreement,
  missing full records, and a still-changing span inventory.
- Retry transient network failures and 5xx responses within the shared deadline.
- Fail immediately on 400, 401, 403, an untrusted redirect/origin, malformed core schema,
  marker ambiguity, a wrong fixed trace ID, or detected secret exposure.

Do not accept the first nonempty tree. Require two consecutive complete observations with
the same normalized completeness fingerprint. The fingerprint contains sorted span IDs,
parent IDs, types, status/closure fields, aggregate counts, and required-content presence;
it must not contain raw prompts, outputs, tool payloads, or credentials.

After the inventory is stable, retrieve every full record and run the semantic contract.
If a record is missing or its content is not storage-enriched yet, resume polling until
the deadline.

## 5. Failure taxonomy

Use stable, redacted error classes/codes:

| Error | Retry behavior |
|---|---|
| `BackendAuthenticationError` | 401/403; fail immediately |
| `BackendRateLimitError` | Retry within deadline using capped `Retry-After` |
| `BackendTransportError` | Retry DNS/TLS/timeout/5xx; fail at deadline |
| `BackendSchemaError` | Fail malformed JSON or missing/mistyped core fields |
| `TraceNotReady` | Internal retry state for absent or incomplete ingestion |
| `TraceAmbiguityError` | Fail multiple exact-marker traces or marker/ID disagreement |
| `TraceDeadlineExceeded` | Fail with the last safe unmet-check codes |
| `TraceContractError` | Fail a stable semantic contract violation |
| `TraceSecretExposureError` | Fail immediately without echoing the matched value |

Exceptions and JSON evidence may expose only operation name, trace role, status class,
attempt count, elapsed time, check codes, trace ID, and trace URL. They must omit request
headers, the API key, raw response bodies, prompts, outputs, organization data, and storage
object keys.

## 6. Shared hard acceptance contract

Both traces must satisfy all of the following:

- distinct, nonzero, lowercase 32-hex trace IDs;
- exact marker lookup returns one trace and no unrelated trace;
- every tree/list/detail record carries the locked trace ID;
- top-level `span_count` equals the recursive-tree count and flat-list count;
- exactly one real root, with no synthetic root;
- unique span and record IDs;
- every non-root parent exists and contains the child it claims;
- connected, acyclic hierarchy with no orphan records;
- completed timestamps and nonnegative durations within the recorded smoke window plus
  a small documented clock-skew allowance;
- successful terminal status, 2xx status code, empty error fields, and trace
  `error_count=0`;
- no `blurred=true` or other usage-cap response that withholds full record contents;
- no `Span not properly closed` value anywhere;
- semantically nonempty required input/output after parsing embedded JSON;
- the exact marker on the trace/root metadata; child-marker propagation will become
  strict only if the pre-implementation schema probe confirms it for every full record;
- no exact runtime API-key value, encoded variant, bearer header, credentialed URL, or
  high-confidence Respan/OpenAI, GitHub, AWS, private-key, or JWT credential shape in any
  raw or decoded content.

Environment-variable names such as `RESPAN_API_KEY` are allowed. Generic high-entropy
values are warning-only because legitimate span IDs, signatures, and organization IDs
are high entropy.

## 7. Agent trace contract

The semantic shape is strict; model-dependent tool counts are not:

```text
workflow [workflow root; workflow/group identity = <agent_run_id>]
└── agent.respan-integration-agent [chat]
    └── tool.<allowed tool> [tool] × N
```

Required checks:

- the deployed root `span_name` is exactly `workflow`, while both
  `span_workflow_name` and trace `trace_group_identifier` equal the agent marker;
- exactly one direct `agent.respan-integration-agent` chat child;
- agent input/output is captured and nonempty;
- input proves the exact marker, Auto mode, service/environment, pinned versions,
  tracing-reference suffix, no-web/no-install constraint, and edit-only boundary;
- the first tool milestone invokes `Skill` for exactly `respan`;
- a successful `Read` of `references/tracing.md` occurs before the first edit;
- allowed tools are only `Skill`, `Read`, `Glob`, `Grep`, and `Edit`;
- reject Bash, WebFetch, WebSearch, Agent/sub-agent, Write, or an unknown tool;
- every tool has parseable, nonempty input and output;
- every Edit resolves inside the checkout and the union of Edit targets equals exactly
  `app.py` and `requirements.txt`;
- one LLM call; canonical prompt/completion counts are positive and sum to total tokens;
- trace aggregate token totals agree with the canonical full chat record;
- canonical `metadata.response_cost` is finite, positive, and agrees with the local SDK
  terminal cost within a small relative/absolute tolerance;
- `respan.environment=onboarding`, service identity, and the pinned Claude SDK
  instrumentation scope are present in the canonical record shape frozen in phase 1.

Do not hardcode the observed total of 11 spans or the exact counts of Skill, Read, Glob,
Grep, and Edit calls.

## 8. Target trace contract

The current `respan-ai==4.1.0` plus OpenAI OTel `0.62.3` compatibility profile requires:

- exactly one trace and one root chat span, with no children;
- current span name `llm.gpt-4o-mini` and model `gpt-4o-mini`;
- OpenAI provider and chat operation;
- input semantically equals one user request to return the exact target marker;
- output assistant content equals the target marker exactly;
- successful finish with no error;
- positive prompt and completion tokens, with total equal to their sum;
- finite positive cost, consistent across aggregate/list/detail within documented decimal
  tolerance;
- exact run marker, service `respan-v0a-python-smoke`, `respan.environment=smoke`,
  official Respan base, and OpenAI instrumentation scope `0.62.3` in the normalized
  canonical fields frozen in phase 1.

Do not hardcode the observed `28/13/41` tokens or USD `0.000012`. The target name is strict
only for this versioned compatibility profile; a future 4.2/native profile must update the
contract explicitly rather than accepting multiple names silently.

## 9. Named warnings for known platform gaps

Known mismatches must be emitted as explicit warning codes, not ignored and not allowed to
weaken other checks:

- `W_ENVIRONMENT_PROJECTION`: top-level environment is `prod` while canonical metadata
  says `onboarding` or `smoke`;
- `W_AGENT_AGGREGATE_COST`: agent aggregate cost is zero while the canonical full record
  and SDK terminal result agree on a positive cost;
- `W_AGENT_TREE_USAGE`: agent child usage/cost is zero in the tree while the trace
  aggregate and full record contain the canonical values;
- `W_TARGET_SPAN_CONTRACT`: current compatibility span naming differs from the desired
  future native Respan contract.

Unknown warnings or new cross-view contradictions are failures until reviewed and added to
the versioned contract.

## 10. Test plan

Add hand-sanitized response fixtures under `agent/tests/fixtures/backend/`. Preserve schema,
hierarchy, and canonical content while removing organization identifiers, storage keys,
absolute temporary paths, and any credential-bearing data.

### `agent/tests/test_platform.py`

- exact URL and POST-body construction;
- official-host/TLS pinning, no ambient proxy, and no redirects;
- trace/span ID encoding and validation;
- bearer authentication without key leakage in request representations or errors;
- pagination with the deployed `count` discrepancy;
- connect/read timeout and response-size ceiling;
- 400/401/403, 404, 429 plus `Retry-After`, and 5xx handling;
- malformed JSON, missing core fields, wrong types, and additive fields.

### `agent/tests/test_trace_gate.py`

- golden agent and target observations;
- not found -> partial -> complete -> stable pass;
- shared deadline, capped backoff, and no oversleep using a fake clock;
- zero/one/multiple exact-marker results;
- agent marker/known-ID disagreement;
- partial pagination and tree/list/detail ID disagreement;
- duplicate ID, orphan, cycle, multiple root, synthetic root, and count mismatch;
- missing closure, error status, improperly closed span, or missing full record;
- blurred/usage-capped full detail that prevents content inspection;
- missing/empty/invalid input or output;
- forbidden tool, wrong tool order, or Edit outside the two-file allowlist;
- wrong target marker, output, model, provider, service, environment, or scope;
- zero/inconsistent token or canonical cost fields;
- exact, URL-encoded, JSON-escaped, bearer, private-key, and credential-shaped leaks;
- every named projection warning and rejection of an unregistered warning.

### `agent/tests/test_smoke_script.py`

- backend verification runs only after local and target gates pass;
- pending/failed backend state cannot emit a pass verdict;
- successful report contains both IDs/links, attempts, timestamps, check codes, warnings,
  patch hash, and dependency versions;
- report contains neither raw backend payloads nor the sentinel API key;
- stable exit classes for local/config, backend availability/deadline, and contract failure.

Normal CI remains credential-free and network-free. Add a separate protected manual or
scheduled live job only after the local implementation passes. The live job must be
disabled on forks, use a hard timeout/concurrency limit, invoke the paid smoke once, and
archive sanitized evidence only.

## 11. Implementation sequence

1. **Freeze the read contract**
   - Probe the four documented read operations against the two approved trace IDs.
   - Confirm exact field names, metadata propagation, pagination, and blurred-record
     behavior.
   - Create hand-sanitized golden fixtures and initially failing contract tests.
2. **Add the read-only platform client**
   - Implement the pinned, no-proxy/no-redirect transport and typed redacted errors.
   - Complete request, pagination, schema, timeout, and secret-safety tests.
3. **Implement pure trace validation**
   - Normalize all four views, cross-check inventories, and implement shared, agent, target,
     and warning registries.
   - Complete all mutation/false-pass tests before wiring the smoke.
4. **Implement bounded convergence polling**
   - Add the shared deadline, backoff, stable fingerprint, and safe diagnostics.
   - Prove every retry and terminal failure with fake time and transport.
5. **Gate the smoke**
   - Call the gate after target execution and replace the pending verdict.
   - Add versioned sanitized JSON evidence and nonzero failure exit classes.
6. **Run one fresh live acceptance**
   - Use new exact agent/target markers.
   - Cross-check the first automated result manually once against MCP/platform records.
   - Update the corrected evidence report and check the README item only after agreement.

Suggested reviewable commit sequence:

1. `test: freeze v0 backend trace contracts`
2. `feat: add read-only Respan platform client`
3. `feat: validate exact agent and target trace contents`
4. `feat: gate v0 smoke on backend verification`
5. `test: prove automated backend acceptance live`

## 12. Definition of done

The checklist item is complete only when one fresh credentialed run:

- passes the existing patch, replay, dependency, and real target gates;
- finds exactly one agent trace and one target trace by exact marker;
- proves the known agent ID matches the marker and discovers one target ID;
- validates the two complete trees and every full span record;
- rejects duplicates, orphans, cycles, partial data, errors, forbidden tools, wrong edits,
  wrong model/content, and secret exposure;
- converges within the bounded shared deadline;
- emits `BACKEND_VERIFIED_PASS` with both deep links and sanitized evidence;
- exits nonzero for every backend-pending or backend-failed state;
- passes all credential-free fixtures/mocks and one protected live run;
- requires no manual backend inspection to justify exit zero.

Only after those conditions pass should the README checkbox be marked complete. The next
checklist item after this one remains gateway credits/BYOK readiness preflight.
