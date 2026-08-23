# Respan Agent

A first-party onboarding agent that integrates **Respan** into a repo. The current
v0a path returns a validated patch and dogfoods Respan's gateway and tracing. The
protected v0b path can deliver that exact accepted patch to an explicitly allowlisted
disposable public fixture; live acceptance remains outstanding.

Form factor: a GitHub App (proactive, PR-producing — like Snyk/Dependabot, not CodeRabbit).
The full design is in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

```
Install GitHub App → setup.respan.ai → pick repo → questionnaire → Submit
   → sandbox: clone → agent runs the /respan skill (Sonnet via gateway, traced)
   → commit branch → open PR → "your first trace →"
```

## Layout

| Path | What | Status |
|------|------|--------|
| `agent/` | Session runner + isolated Claude Agent SDK loop + validator + protected delivery | **v0a live-smoked; v0b offline-verified** |
| `web/` | `setup.respan.ai` — auth, credits/BYOK, questionnaire, live progress | v1 |
| `github-app/` | App manifest + webhook handler | v1 |
| `evals/` | Sample-repo dataset + scorers (Respan experiments) | v2 |

## v0 — prove the loop

No GitHub App yet. v0a accepts a trusted public/local repo and returns a validated
patch plus an exact trace link. v0b adds authenticated PR delivery only after the
same patch passes the fresh target and exact backend trace-content gates.

For trusted public/local v0a, the **only secret needed is `RESPAN_API_KEY`** — the
gateway routes the model (no Anthropic key), and the same key sends the dogfood trace.
Private repositories are not part of the accepted path yet. The first v0b boundary is
restricted to one operator-allowlisted, disposable, public GitHub.com fixture.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ./agent
export RESPAN_API_KEY=...

# v0a — integrate + show the diff + emit a trace (no GitHub needed):
respan-integration-agent run --repo https://github.com/acme/app --config config.json

# Do not pass a GitHub token to this CLI. Protected v0b delivery is a separate
# post-acceptance runner and the normal CLI always remains mutation-free.
```

The protected entrypoint is `scripts/run_v0b_smoke.py`. It requires an exact
disposable fixture URL/slug, base ref, an existing absolute mode-`0700` journal
directory, and `RESPAN_GITHUB_TOKEN`. It hard-denies this repository, strips the
credential before every agent/install/target/telemetry subprocess, and exposes it
only to the bounded push and GitHub REST client after acceptance. Do not target an
upstream or production repository. See the
[implementation evidence](V0_PR_DELIVERY_IMPLEMENTATION_2026-08-23.md) and
[v0b plan](V0_PR_DELIVERY_GAP_AND_PLAN.md).

The repeatable credentialed fixture is:

```bash
.venv/bin/python scripts/run_v0_smoke.py
```

It creates a fresh trusted repo, runs the onboarding agent, replays and validates the
complete patch, installs the modified target in a fresh environment, runs one real
gateway-routed target call, then polls the exact agent/target markers and validates every
backend trace/span record before it can emit `BACKEND_VERIFIED_PASS`.

On `v0-checklist-implementation`, that paid path is now preceded by a fail-closed gateway
readiness check. The agent-side contract is implemented, but the deployed readiness
endpoint returned `404` in the recorded live check; therefore that run stopped before
checkout/model execution and the checklist remains open pending a fresh pass. See the
[implementation evidence](V0_GATEWAY_PREFLIGHT_IMPLEMENTATION_2026-08-23.md).

`config.json` is an `OnboardingRequest` ([config.py](agent/src/respan_integration_agent/config.py)):

```json
{ "repo_url": "https://github.com/acme/app", "product": "tracing", "tracing": { "mode": "auto" } }
```

### v0 checklist

- [x] Strict config contract for the deterministic v0a profile
- [x] Session path: preflight → clone → bounded agent → complete patch → semantic gate
- [x] Bundle and hash-pin the `/respan` skill; isolate it in a temporary Claude config
- [x] Require a successful Claude SDK terminal result and active instrumentation
- [x] Route the model through the gateway with time, turn, and dollar caps
- [x] **v0a smoke run** — validated patch + fresh target run + exact agent/target traces ([evidence](V0_SMOKE_RUN_2026-08-21_CORRECTED.md))
- [x] Automate exact backend trace-content inspection inside the smoke harness ([evidence](V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md), [plan](V0_BACKEND_TRACE_GATE_PLAN.md))
- [ ] Gateway preflight: agent-side route/funding gate implemented; backend endpoint deployment and live acceptance remain ([evidence](V0_GATEWAY_PREFLIGHT_IMPLEMENTATION_2026-08-23.md), [plan](V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md))
- [ ] `open_pr`: offline implementation complete; protected live fixture creation + replay acceptance remain — v0b ([evidence](V0_PR_DELIVERY_IMPLEMENTATION_2026-08-23.md), [plan](V0_PR_DELIVERY_GAP_AND_PLAN.md))
- [ ] Container/VM isolation and safe private-repository credentials before untrusted repos

**v0a success = the agent integrates Respan and both agent/target traces pass the exact
backend content gate. v0b adds the PR.**

The live v0a uses `respan-ai==4.1.0` plus
`opentelemetry-instrumentation-openai==0.62.3`. Published `respan-ai` 4.2.x wheels
currently reference instrumentation versions that are not available on PyPI; the
workaround and remaining platform-field gaps are recorded in the smoke evidence.

## Dogfood hooks

- **Tracing:** the agent loop is instrumented (`respan-instrumentation-claude-agent-sdk`).
- **Gateway:** the agent's LLM calls route through the gateway with a per-user budget.
- **Evals:** `evals/` scores onboarding outcomes over a dataset of sample repos.
