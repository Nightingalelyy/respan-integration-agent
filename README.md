# Respan Agent

A first-party onboarding agent that integrates **Respan** into a repo. The current
v0a path returns a validated patch and dogfoods Respan's gateway and tracing. PR
delivery remains the separate v0b milestone.

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
| `agent/` | Session runner + isolated Claude Agent SDK loop + validator + CLI | **v0a live-smoked** |
| `web/` | `setup.respan.ai` — auth, credits/BYOK, questionnaire, live progress | v1 |
| `github-app/` | App manifest + webhook handler | v1 |
| `evals/` | Sample-repo dataset + scorers (Respan experiments) | v2 |

## v0 — prove the loop

No GitHub App yet. v0a accepts a trusted public/local repo and returns a validated
patch plus an exact trace link. v0b will add authenticated PR delivery.

For trusted public/local v0a, the **only secret needed is `RESPAN_API_KEY`** — the
gateway routes the model (no Anthropic key), and the same key sends the dogfood trace.
Private repositories and v0b additionally require redesigned GitHub authorization and
are not part of the accepted path yet.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ./agent
export RESPAN_API_KEY=...

# v0a — integrate + show the diff + emit a trace (no GitHub needed):
respan-integration-agent run --repo https://github.com/acme/app --config config.json

# v0b — also open a PR:
respan-integration-agent run --repo ... --config config.json --token "$GH_TOKEN"
```

The repeatable credentialed fixture is:

```bash
.venv/bin/python scripts/run_v0_smoke.py
```

It creates a fresh trusted repo, runs the onboarding agent, replays and validates the
complete patch, installs the modified target in a fresh environment, runs one real
gateway-routed target call, then polls the exact agent/target markers and validates every
backend trace/span record before it can emit `BACKEND_VERIFIED_PASS`.

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
- [ ] Gateway preflight: verify credits/BYOK before spending a turn (`runner._preflight`) ([gap analysis and plan](V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md))
- [ ] `open_pr`: push branch + create PR via REST (`github.py`) — v0b
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
