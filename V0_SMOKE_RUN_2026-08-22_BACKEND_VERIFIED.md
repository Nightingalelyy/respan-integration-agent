# Backend-verified v0a smoke run — 2026-08-22

## Verdict

**BACKEND_VERIFIED_PASS** for the trusted deterministic v0a profile.

This is the first run whose exit code was gated automatically on the complete Respan
backend contents. It passed the existing patch/replay/install/target checks, found both
traces through exact run markers, required two stable inventories, retrieved every full
span record, and passed the shared, agent, and target semantic contracts. No manual UI or
MCP inspection was needed to justify exit zero.

The implementation remains an uncommitted working tree on the original audited base; no
branch was pushed and no PR was created.

## Fresh run evidence

- Started: `2026-08-22T07:39:48.487699Z`
- Target capture finished: `2026-08-22T07:41:29.668372Z`
- Backend verified: `2026-08-22T07:44:19.943995Z`
- Evidence schema: `respan-v0-smoke-evidence/v1`
- Gate schema: `respan-v0-backend-trace-gate/v1`
- Trusted fixture base commit: `adde717463ffbbf61ff9d6ac71f27a795e74c670`
- Accepted patch SHA-256: `7f8daecc8e50239bdc3c62df323f77e20392d22d1e6c938a118747a8ad3c3f68`
- Changed paths: exactly `app.py` and `requirements.txt`
- Target stdout: exactly `SMOKE_OK`
- Gate convergence: two observations in `170.264695` seconds under one 240-second
  deadline, with 10-second connect and 30-second read caps

### Agent trace

- Run marker: `respan-v0a-agent-13d00913457a`
- SDK session: `c18c80a0-69c4-4bd0-8df3-f47cebfb6f08`
- Turns: 11
- SDK cost: USD `0.1148847`
- Trace: [`c6b4cf7687f68682f24dc897c1a27ec5`](https://platform.respan.ai/platform/traces?trace_unique_id=c6b4cf7687f68682f24dc897c1a27ec5)

The gate proved the fixed `workflow` root and exact workflow/group marker identity, one
direct `agent.respan-integration-agent` Claude chat, allowed tool-only descendants,
`Skill(respan)` first, the pinned tracing reference read before edits, and the exact
`app.py`/`requirements.txt` Edit union inside the retained checkout root. It reconciled
canonical full-detail tokens and string-valued `metadata.response_cost` with the SDK
cost and rejected errors, partial closure, blurred records, forbidden tools, wrong
paths, malformed content, and credential exposure.

### Target trace

- Run marker: `respan-v0a-target-13d00913457a`
- Trace: [`e856a225d5b70c47274fe3b7c3f40f90`](https://platform.respan.ai/platform/traces?trace_unique_id=e856a225d5b70c47274fe3b7c3f40f90)

The gate proved one root `llm.gpt-4o-mini` OpenAI chat and no children; exact marker
request/response semantics; successful closure; positive, internally consistent tokens
and cost in every view; service `respan-v0a-python-smoke`; environment metadata `smoke`;
scope `opentelemetry.instrumentation.openai.v1==0.62.3`; and the pinned
`https://api.respan.ai/api/` target endpoint.

### One-time independent REST cross-check

After the automated pass, a separate exact-marker/detail/list/full-record read confirmed:

- agent marker results: 1 and matching the recorded ID; trace/tree/list/detail count:
  11; types: one workflow, one chat, and nine tools;
- target marker results: 1 and matching the recorded ID; trace/tree/list/detail count:
  1; type: one chat;
- both traces: `error_count=0`; every detail successful with status code 200, explicitly
  unblurred, and carrying the exact expected run marker.

Only these sanitized counts and booleans were printed; no raw prompts, outputs, tool
payloads, organization fields, storage fields, or credentials were retained.

## Automated checks and registered warnings

Passing checks:

- `P_SHARED_TRACE_CONTRACT` for agent and target
- `P_AGENT_TRACE_CONTRACT`
- `P_TARGET_TRACE_CONTRACT`

Known warnings, and no others:

- `W_ENVIRONMENT_PROJECTION`: top-level environment remains `prod`, while canonical
  metadata correctly says `onboarding` or `smoke`.
- `W_AGENT_AGGREGATE_COST`: agent aggregate cost projects zero; canonical full-detail
  `metadata.response_cost` equals the SDK terminal cost.
- `W_AGENT_TREE_USAGE`: agent tree usage projects zero; full detail and trace aggregate
  token totals agree.
- `W_TARGET_SPAN_CONTRACT`: the pinned compatibility instrumentation uses
  `llm.gpt-4o-mini`; re-freeze this contract after the current SDK packaging path is
  repaired.

## Reproducibility and package verification

- Python `3.12.13` (project range: 3.11–3.13)
- `respan-ai==4.1.0`
- `opentelemetry-instrumentation-openai==0.62.3`
- `claude-agent-sdk==0.2.143`
- `respan-instrumentation-claude-agent-sdk==0.2.0`
- `opentelemetry-claude-agent-sdk==0.1.4`
- `pydantic==2.13.4`
- Final wheel SHA-256:
  `ee47560ebc1cd15fa6cc1623b85bbfac63e710609b476fc0cc459e630ae31856`
- Fresh isolated wheel install: `pip check` passed; CLI, platform/gate modules, and
  bundled skill resources loaded successfully
- Offline repository verification: 203 tests passed, Ruff passed, and
  `git diff --check` passed

The read adapter uses direct verified TLS to the pinned `https://api.respan.ai` origin,
does not honor ambient proxies or redirects, bounds response size/time, and emits typed
payload-free errors. Normal tests remain credential-free and network-free.

## Remaining v0 work

This completes the README item for automated backend trace-content inspection. It does
not complete v0b or authorize untrusted repositories. The next checklist item is the
orchestration gateway credits/BYOK readiness preflight. PR delivery, disposable
container/VM isolation, safe private-repository credentials, dependency locking/hashes,
and the four named platform projection issues remain separate work.
