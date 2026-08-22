# v0 gap analysis and implementation plan

- Audit date: 2026-08-21
- Repository: `nightingalelyy/respan-integration-agent`
- Branch: `main`
- Commit: `e7b3c4342ee7066c629f2813a7f0dd4f5be85c08`
- Audit mode: source review plus credential-free runtime probes, followed by one failed credentialed v0a smoke; no product implementation or GitHub write

## Status update — backend-verified trusted v0a smoke

The original audit below remains the baseline at commit `e7b3c43`. A subsequent local
implementation and live run corrected the trusted v0a path; see
[`V0_SMOKE_RUN_2026-08-21_CORRECTED.md`](V0_SMOKE_RUN_2026-08-21_CORRECTED.md). The
subsequent automated backend gate and fresh passing run are recorded in
[`V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md`](V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md).

Now demonstrated end to end:

- exact bundled-skill provisioning and discovery before the first model turn;
- bounded/restricted Claude execution with strict terminal-result acceptance;
- a complete replayable patch and deterministic target semantic gates;
- fresh dependency installation, `pip check`, and a real successful target call;
- exact-marker agent and target trace discovery plus automated tree/list/full-detail
  content acceptance before exit zero;
- 203 passing tests plus Ruff and a fresh clean wheel-install check.

This resolves the v0a portions of G-02 through G-05 and G-12, and materially advances
G-03, G-04, G-13, G-14, and G-15. Automated backend acceptance for the fixed trusted
profile is now complete. It does **not** complete full v0: current-SDK packaging, trace
environment/cost projection, untrusted-repo isolation, safe GitHub credentials, gateway
preflight, idempotency, and v0b/PR delivery remain open.

The backend-gate implementation follows
[`V0_BACKEND_TRACE_GATE_PLAN.md`](V0_BACKEND_TRACE_GATE_PLAN.md), with its deadline
updated from the proposed 120 seconds to the measured 240-second ceiling. The next
checklist item is orchestration gateway credits/BYOK readiness preflight; its current
source-backed gap analysis and implementation sequence are in
[`V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md`](V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md).

## Status update — agent-side gateway readiness

On 2026-08-23, the integration-agent side of that next item was implemented on
`v0-checklist-implementation`: strict exact target routes, a pinned orchestration model,
an immutable readiness plan, hardened fail-closed client, pre-checkout runner ordering,
safe CLI/smoke evidence, and 378 passing offline tests. A protected request to the
official origin returned `404` because the reviewed/deployed backend does not yet expose
`POST /api/gateway/readiness/`; the actual smoke therefore exited `4` before checkout or
model execution. The item remains correctly open. See the
[`implementation evidence`](V0_GATEWAY_PREFLIGHT_IMPLEMENTATION_2026-08-23.md).

## Executive result

The `563edbd` baseline has a **workable backend-verified trusted v0a demo**, but the
current branch intentionally cannot start that paid path until the missing backend
readiness endpoint is deployed. Neither state is full v0/v0b release acceptance.

- The package builds, installs, imports, and exposes its CLI.
- The fixed v0a profile cannot report success until the complete patch, fresh target run,
  and both exact backend trace-content contracts pass; the wheel includes and verifies
  the pinned `/respan` skill.
- The v0b path cannot work: `open_pr()` always raises `NotImplementedError`.
- The current token and execution boundaries are unsafe for an agent operating on an untrusted repository.
- Gateway readiness preflight and the remaining packaging/platform/reproducibility gaps
  are still open.

The detailed checklist reassessment and confirmed gaps below preserve the original
`e7b3c43` audit evidence; use this status update and the linked 2026-08-22 report for the
current trusted-v0a state.

Recommended milestone naming:

- **v0a acceptance:** a safe, reproducible local/isolated run produces a correct integration patch and a backend-verified trace.
- **v0 completion (v0b):** the same quality gate also produces one correct, idempotent pull request.

This reconciles the README's v0a/v0b split with the architecture document's stricter definition of v0 success as a real PR plus a real trace.

## Original checklist reassessment at `e7b3c43`

| README checklist item | Recorded state | Evidence and remaining gap |
|---|---|---|
| Config contract | **Partial** | Typed Pydantic models exist, but URLs/branches are unvalidated, extra fields and typos are silently ignored, inconsistent cross-fields are accepted, gateway models are absent, and `enterprise` has no endpoint URL. See [`config.py`](agent/src/respan_integration_agent/config.py#L38-L88). |
| Session skeleton: preflight -> clone -> agent -> diff/PR | **Partial** | The call order exists, but preflight is a no-op, patch capture is incomplete, success gates are weak, and the PR path is a stub. See [`runner.py`](agent/src/respan_integration_agent/runner.py#L26-L59). |
| Claude Agent SDK + `/respan` + instrumentor | **Partial** | `query()` and the instrumentor are wired, but the skill is neither packaged nor verified; terminal SDK errors are ignored; trace capture/flush is nondeterministic. See [`agent.py`](agent/src/respan_integration_agent/agent.py#L73-L143). |
| Gateway routing + cost cap | **Partial** | The Anthropic-compatible base URL and Respan key are passed and `max_turns=40` is set. There is no readiness check, timeout, dollar/token budget, pinned model, or usage gate. See [`agent.py`](agent/src/respan_integration_agent/agent.py#L73-L112). |
| v0a smoke run | **Attempted; failed** | A real gateway-routed run exited 0 but generated a broken dependency/initializer, omitted flush and target verification, returned an all-zero trace ID, and stored an internally inconsistent trace. See [`V0_SMOKE_RUN_2026-08-21.md`](V0_SMOKE_RUN_2026-08-21.md). |
| Gateway preflight | **Missing** | Current `_preflight()` rejects an empty API key and verifies the bundled skill, but its gateway branch is a literal `pass`; it receives neither the base URL nor model and skips orchestration readiness for tracing-only runs. See [`runner.py`](agent/src/respan_integration_agent/runner.py#L43-L65) and the [current plan](V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md). |
| `open_pr` | **Missing** | The function always raises `NotImplementedError`. See [`github.py`](agent/src/respan_integration_agent/github.py#L40-L50). |
| Provision `/respan` skill in sandbox image | **Deferred in README, but a v0 prerequisite is missing** | Hosted image provisioning can remain v1, but v0a still needs a pinned local skill source plus a preflight check. The wheel does not contain the skill. See [`agent.py`](agent/src/respan_integration_agent/agent.py#L141-L143). |

## Acceptance ambiguities to settle first

1. **What completes v0?** [`README.md`](README.md#L36-L59) calls patch + trace v0a and PR v0b; [`ARCHITECTURE.md`](ARCHITECTURE.md#L76-L84) says v0 success is PR + trace. Use v0a as a gate and v0b as v0 completion.
2. **Which secrets are required?** “Only `RESPAN_API_KEY`” is true only for the model/telemetry path on a public v0a repo. A private clone and every v0b run also need GitHub authorization. See [`README.md`](README.md#L27-L40).
3. **Which gateway is preflighted?** Every orchestration run uses the Respan gateway, including tracing-only onboarding. Gateway/both target integrations have a second, distinct readiness check for the target repo's selected funding/providers/models.
4. **Where does `/respan` come from?** v0 must not depend silently on mutable operator state under `~/.claude/skills`. Define a pinned asset/version and verify it before the first turn.
5. **What proves a trace?** Initialization is not acceptance. Define a run marker, trace/root shape, required Claude/tool spans, inputs/outputs, token/cost fields, terminal status, flush behavior, and backend lookup.
6. **What proves an integration?** Define expected dependency/config/code changes by product/mode, target-repo build/test checks, prohibited files, secret scan, and rerun/idempotency behavior.
7. **What is the v0 sandbox?** A temporary directory is cleanup, not containment. Decide whether v0a is restricted to trusted throwaway repos or must already use a disposable container/VM.

## Confirmed gaps

### P0 - demo/release blockers

#### G-01: v0b cannot create a PR

`run_session()` commits locally and calls `open_pr()`, which unconditionally raises. The temporary checkout is then deleted, so the local recovery branch disappears too.

Evidence:

- [`runner.py:50-52`](agent/src/respan_integration_agent/runner.py#L50-L52)
- [`github.py:40-50`](agent/src/respan_integration_agent/github.py#L40-L50)
- Runtime probe: `v0b_open_pr_unimplemented = true`

Required outcome: a no-force push and GitHub REST PR creation against the configured base branch, returning a stable PR URL/number and handling retries without duplicates.

#### G-02: the documented clean v0a install does not supply or verify `/respan`

The prompt requires `/respan`, while the source comment assumes it already exists in the operator's home directory. The built wheel contains only the seven Python modules and metadata; it contains no skill. README setup documents only `pip install -e .` and `RESPAN_API_KEY`.

Evidence:

- [`README.md:29-37`](README.md#L29-L37)
- [`agent.py:40-43`](agent/src/respan_integration_agent/agent.py#L40-L43)
- [`agent.py:141-143`](agent/src/respan_integration_agent/agent.py#L141-L143)
- [`pyproject.toml:23-24`](agent/pyproject.toml#L23-L24)

The current [Claude Agent SDK skills guide](https://code.claude.com/docs/en/agent-sdk/skills) also provides an init-message `skills` list specifically for checking discovery. This runner never checks it.

Required outcome: bundle or install a pinned skill, load only the intended source, and fail preflight if the exact name/version/hash is absent.

#### G-03: failed or incomplete agent runs can be delivered as success

The loop captures `ResultMessage.result` but ignores `is_error`, subtype, terminal reason, permission denials, turns, usage, and cost. It substitutes “Applied Respan integration” when no usable result text exists. `run_session()` only requires any changed path, so partial edits from an API error, denied tool, timeout, or max-turn stop may be committed.

Evidence:

- [`agent.py:114-130`](agent/src/respan_integration_agent/agent.py#L114-L130)
- [`runner.py:45-52`](agent/src/respan_integration_agent/runner.py#L45-L52)
- Runtime probe: an SDK result with `is_error=true`, an error string, and a partial edit returned a normal `AgentResult`; `sdk_error_result_treated_as_summary = true`.

The current [Claude Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python) defines explicit result error states and fields; these must be evaluated, not treated as summary text.

Required outcome: require one explicit successful terminal result, classify failures, enforce timeout/cancellation/budget, and never commit partial changes from an unsuccessful run.

#### G-04: v0a can report an empty patch despite real changes

`git diff` shows only unstaged tracked changes. It omits new untracked files and staged edits. The agent status helper can see those paths, and v0b later stages everything with `git add -A`, so files hidden from v0a review can still be committed.

Evidence:

- [`agent.py:134-138`](agent/src/respan_integration_agent/agent.py#L134-L138)
- [`runner.py:62-67`](agent/src/respan_integration_agent/runner.py#L62-L67)
- [`github.py:32-37`](agent/src/respan_integration_agent/github.py#L32-L37)
- Runtime probe: Git status saw one staged and one untracked file while `_git_diff()` returned an empty string; `v0a_diff_is_empty_despite_two_changes = true`.
- Runtime probe: `run_session()` returned normally with both an empty diff and `trace_id=None`.

Required outcome: generate one canonical, reviewable patch covering staged, unstaged, untracked, renamed, deleted, and binary changes; check Git return codes; apply path/size/binary/secret policies before success or commit.

#### G-05: “real trace” is not a deterministic success condition

The Respan handle is discarded. Trace ID lookup occurs only after the instrumented query has ended, when there may be no active span, and all lookup errors are swallowed. There is no explicit flush or backend verification, and clone/preflight/diff/commit/PR are outside a session root span. The runner accepts `trace_id=None`.

Evidence:

- [`agent.py:82-92`](agent/src/respan_integration_agent/agent.py#L82-L92)
- [`agent.py:119-130`](agent/src/respan_integration_agent/agent.py#L119-L130)
- [`runner.py:53-58`](agent/src/respan_integration_agent/runner.py#L53-L58)
- Runtime probe with a fake telemetry handle: neither `flush()` nor `shutdown()` was called.
- Runtime probe: a session with `trace_id=None` returned success.

The current [Respan Claude Agent SDK integration guide](https://www.respan.ai/docs/integrations/claude-agents-sdk) retains the `Respan` object and explicitly calls `respan.flush()` after the query. The live smoke returned an all-zero ID even though a time-window backend search later found the actual trace; 13 of 21 stored records said `Span not properly closed` while all 21 were marked successful. See [`V0_SMOKE_RUN_2026-08-21.md`](V0_SMOKE_RUN_2026-08-21.md).

Required outcome: create an explicit session/root trace, capture its ID while active, attach an exact run marker and phase/result metadata, flush in `finally` with a timeout, and verify the exact backend record before v0a acceptance.

#### G-06: GitHub credentials can leak and can be sent to an arbitrary host

Any supplied token is embedded in any `https://` URL, not just an approved GitHub host. It appears in the `git clone` process arguments and clone exception command. A successful clone normally retains its credential-bearing origin URL in `.git/config`, which is readable by the agent.

Evidence:

- [`sandbox.py:16-31`](agent/src/respan_integration_agent/sandbox.py#L16-L31)
- Runtime probe with a fake token and `evil.example`: `token_sent_to_arbitrary_https_host = true`.
- Runtime probe against a closed local port: `token_present_in_clone_exception_command = true`.
- [`cli.py:31-33`](agent/src/respan_integration_agent/cli.py#L31-L33) also accepts the token as a command-line argument, exposing it to process inspection/shell history.

Required outcome: validate scheme/host/port; reject credentials in input URLs, local/file/remote-helper URLs, and unsupported hosts; use a short-lived repository-scoped credential through an ephemeral askpass/header mechanism; reset origin to a credential-free URL; redact every error/log/trace; receive secrets through env/stdin/secret storage rather than CLI arguments.

#### G-07: `TemporaryDirectory` is not an agent sandbox

The agent runs directly on the host in an untrusted checkout. With the currently resolved SDK, the observed options are `setting_sources=None`, `skills=None`, `sandbox=None`, `max_budget_usd=None`, and no allowed/disallowed tool policy. Current SDK defaults load user/project/local settings, so target-repo `CLAUDE.md`, skills, settings, hooks, and MCP configuration can affect the run. The SDK subprocess also inherits the parent process environment in addition to the explicitly supplied Respan/Anthropic variables.

Evidence:

- [`sandbox.py:23-33`](agent/src/respan_integration_agent/sandbox.py#L23-L33)
- [`agent.py:101-112`](agent/src/respan_integration_agent/agent.py#L101-L112)
- Credential-free option introspection against freshly resolved `claude-agent-sdk==0.2.143`
- [Claude Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python)

Required outcome: use a disposable container/VM as non-root; mount only the checkout; keep credentials outside the agent-readable filesystem/environment; use a read-only root, resource/time limits, egress policy, explicit settings/skills/MCP sources, explicit tools plus hooks, and symlink/path escape protection.

### P1 - correctness and operability gaps

#### G-08: gateway preflight is empty and conceptually underscoped

Current `_preflight(req, respan_api_key)` rejects an empty key and validates the bundled
skill, but performs no gateway I/O: its gateway branch is a literal `pass`. It also
receives neither the selected base URL nor orchestration model and remains conditional
on target product gateway/both, although the orchestration agent always uses the
gateway. Separate these checks:

1. orchestration gateway key/auth/reachability/model/funding for **every** run;
2. target-repo gateway funding/BYOK/provider/model readiness for gateway/both onboarding.

Also verify Git, the target branch, the Claude runtime, the pinned skill, and writable
workspace before spending a model turn. The exact backend/API gap and fail-closed plan
are recorded in
[`V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md`](V0_GATEWAY_PREFLIGHT_GAP_AND_PLAN.md).

#### G-09: config can silently lose or contradict user intent

Confirmed behavior:

- `repo_url="not-a-url"` is accepted;
- misspelled `trcaing` and an unknown top-level field are silently ignored;
- auto mode can carry full-mode-only fields that the prompt later ignores;
- irrelevant tracing/gateway sections are accepted;
- enterprise has no concrete URL;
- architecture asks for providers/models, while the model stores providers only.

Evidence: [`config.py`](agent/src/respan_integration_agent/config.py#L38-L88) and credential-free Pydantic probes.

Required outcome: strict models (`extra="forbid"`), URL/host/branch validation, discriminated product/mode variants, enterprise URL validation, provider+model schema, and documented CLI-over-config precedence.

#### G-10: v0b branch/PR semantics are not designed yet

The branch name is fixed per product, `open_pr()` has no base-branch argument, and there is no base SHA, session/run ID, existing branch/PR lookup, replay protection, rate-limit handling, or retry recovery.

Evidence:

- [`runner.py:42-52`](agent/src/respan_integration_agent/runner.py#L42-L52)
- [`github.py:40-50`](agent/src/respan_integration_agent/github.py#L40-L50)

Required outcome: unique and traceable branch naming, configured base branch/SHA, no force-push, idempotent lookup/reuse, typed handling for auth/not-found/conflict/validation/rate-limit/network failures, and a promised checklist/trace link in the PR body.

#### G-11: execution has no bounded operational failure model

Clone, Git operations, and agent execution have no timeouts. There is no cancellation, retry/backoff, typed/redacted error contract, progress event model, or durable recovery state. CLI catches only a missing API key; invalid JSON/config, clone/auth, SDK, Git, and REST failures become raw tracebacks.

Evidence:

- [`sandbox.py:28-31`](agent/src/respan_integration_agent/sandbox.py#L28-L31)
- [`github.py:21-24`](agent/src/respan_integration_agent/github.py#L21-L24)
- [`agent.py:92-115`](agent/src/respan_integration_agent/agent.py#L92-L115)
- [`cli.py:37-59`](agent/src/respan_integration_agent/cli.py#L37-L59)

#### G-12: no target-repo verification gate exists

Any changed path counts as success. The runner does not install/build/typecheck/test the target, verify requested Respan semantics, check for accidental workflow/config changes, or scan the patch for credentials. The agent's prose summary is trusted as the PR body.

Required outcome: detect the target's supported verification commands, run them within the sandbox, record results, inspect the patch against the request, scan secrets, and use a structured runner-generated PR body.

#### G-13: dependencies and model behavior are not reproducible

The project declares only lower bounds and no constraints/lock. A fresh temporary install on the audit date resolved:

- `claude-agent-sdk==0.2.143`
- `respan-ai==4.1.0`
- `respan-instrumentation-claude-agent-sdk==0.2.0`
- `pydantic==2.13.4`

The SDK minimum is `0.1.58`, but the resolved SDK is a later major/minor behavior surface. The model is the mutable alias `sonnet`; there is no `max_budget_usd`, token budget, timeout, emitted usage/cost, or compatibility matrix.

Evidence: [`pyproject.toml:5-17`](agent/pyproject.toml#L5-L17) and fresh dependency resolution.

Required outcome: pin/lock a tested Python/SDK/instrumentor/Respan/skill/model set, define update testing, record usage/cost/model in the session result, and enforce both turn and monetary/time budgets.

### P2 - test, CI, and documentation gaps

#### G-14: no tests or CI exist

There are no tracked test files or GitHub Actions workflows. Add:

- Python 3.11/3.12/3.13 unit matrix;
- Ruff plus a type checker;
- wheel build/install/import/CLI smoke and `pip check`;
- config, prompt, runner, patch, credential-redaction, agent-result, telemetry-lifecycle, and GitHub REST tests;
- dependency/security scanning;
- credential-gated live tests that are never required for fork PRs.

#### G-15: installation and operator documentation is incomplete

README omits the supported Python range, virtual environment, Git/runtime prerequisites, exact skill install/version, GitHub token scope and safe input method, privacy/security implications, supported repo hosts, failure modes, troubleshooting, and example configs. There is no tracked `config.example.json`. Raw diff and model output are printed by default and may enter retained terminal/CI logs.

## Credential-free checks run

| Check | Result |
|---|---|
| Parse all seven Python source files with Python 3.12 | Pass |
| Build wheel through declared Hatchling backend | Pass; `respan_integration_agent-0.0.1-py3-none-any.whl` |
| Fresh temporary dependency installation | Pass; versions recorded in G-13 |
| Import package plus Claude SDK, Respan, and instrumentor | Pass |
| CLI `--help` | Pass |
| `pip check` in the temporary runtime | Pass; no broken declared requirements |
| Missing `RESPAN_API_KEY` behavior | Pass; exits 2 with a clear message |
| Strict config behavior | Fail; invalid URL and unknown/misspelled fields accepted |
| Complete v0a patch | Fail; staged + untracked changes yielded an empty patch |
| Terminal agent status gate | Fail; simulated SDK error accepted as summary |
| Trace gate | Fail; runner accepted `trace_id=None`; telemetry handle was not flushed/shut down |
| Token safety | Fail; fake token sent to arbitrary HTTPS host and present in clone exception command |
| v0b PR path | Fail; `NotImplementedError` |
| Repository-owned unit/integration tests | Missing |
| CI workflows | Missing |
| Live v0a/v0b | v0a attempted and failed on 2026-08-21; v0b not run. See [`V0_SMOKE_RUN_2026-08-21.md`](V0_SMOKE_RUN_2026-08-21.md). |

Passing packaging/import checks prove only that the skeleton is installable. They do not satisfy v0a or v0b acceptance.

## Implementation plan

### Phase 0 - freeze the contract and executable tests

Goal: make the checklist objectively testable before changing runtime behavior.

1. Rewrite milestone definitions as v0a gate and v0b completion.
2. Define supported repo hosts/protocols, trusted vs untrusted repo policy, public/private behavior, and token scope.
3. Define strict config variants and exact precedence.
4. Define v0a patch, target verification, session trace, and idempotency acceptance.
5. Add unit-test scaffolding reproducing every failed credential-free probe above.

Exit gate: all current gaps have a failing automated test or an explicitly credential-gated acceptance case; README checkboxes link to evidence.

### Phase 1 - secure and deterministic v0a core

Goal: a clean installation can safely attempt one bounded integration.

1. Pin/lock compatible Python dependencies, exact model, and `/respan` skill artifact/version/hash.
2. Replace permissive config with strict validated variants.
3. Validate repo URL/host/branch; redesign auth so secrets never enter URL, argv, checkout config, agent environment, logs, or traces.
4. Introduce actual isolation and explicit SDK settings, skill, MCP, tool, environment, egress, resource, and timeout policy.
5. Implement two-layer preflight: orchestration gateway for every run, target gateway prep when requested, plus runtime/skill/Git/branch checks.
6. Require an explicit successful `ResultMessage`; capture structured status, session ID, usage, cost, turns, duration, permission denials, and errors.
7. Create a runner-level trace, capture its ID while active, attach an exact marker, flush/shutdown in `finally`, and expose a stable trace URL.
8. Generate a complete canonical patch and add path/size/binary/secret/semantic/build/test gates.
9. Add typed/redacted CLI errors and machine-readable result output.

Exit gate: unit/integration tests pass across Python 3.11-3.13; a mocked run cannot succeed with an error result, missing skill, missing trace, empty/incomplete patch, unsafe path, leaked token, or failed target verification.

### Phase 2 - prove v0a live

Goal: replace the unchecked smoke item with reviewable evidence.

1. Prepare a small throwaway public repo with a deterministic missing Respan integration and known test command.
2. Run from a clean environment with a unique exact marker and a constrained budget.
3. Review the entire produced patch, including new/binary/deleted/renamed files; confirm no secret or unrelated edit.
4. Install/build/test the modified repo.
5. Inspect the exact platform trace rather than only checking that a trace exists: tree hierarchy, span/log types, model/tool inputs and outputs, tokens/cost, terminal status, errors, duplicates, and absence of secrets.
6. Rerun against the result and verify the defined idempotent/no-op behavior.

Exit gate: archive command/config/commit marker, reviewed patch, target verification output, exact trace ID/URL, and trace-content inspection. Only then mark v0a smoke complete.

### Phase 3 - implement and test v0b delivery

Goal: add remote delivery without weakening v0a gates.

1. Generate a unique traceable branch from the requested base branch/SHA.
2. Commit only the already-reviewed allowlisted patch.
3. Push without force using a short-lived scoped credential kept outside the agent checkout.
4. Create the PR through GitHub REST with configured base/head, structured summary, verification results, operator checklist, and linked trace.
5. Make retry idempotent: reuse the matching branch/PR and never create duplicates.
6. Handle 401/403/404/409/422/rate-limit/network/partial-push outcomes with typed recovery behavior.
7. Test token redaction and cleanup on every success/failure path.

Exit gate: mocked REST/Git tests cover all outcomes and no failure can lose the recovery identifiers or expose a credential.

### Phase 4 - prove v0b and align documentation

Goal: meet the architecture's full v0 definition.

1. Run the exact v0a fixture with PR delivery enabled.
2. Verify one remote branch, one commit containing exactly the accepted patch, one PR against the correct base, and one exact linked trace.
3. Replay the same request and prove no duplicate branch/commit/PR.
4. Inspect credentials are absent from `.git/config`, process/log output, patch, PR, and trace payloads.
5. Update README/architecture/checklist and add example configs using the recorded verified behavior.

Exit gate: a real PR plus its real inspected trace from the same run, with replay and credential-cleanup evidence. Then v0 is complete.

## Definition of done

### v0a

- Clean, locked install on each supported Python version.
- Public throwaway repo needs only the documented Respan credential; all other prerequisites are provisioned or preflighted.
- Isolated, bounded runtime with explicit skill/settings/tools/network/environment policy.
- Successful terminal SDK result; no partial-error success.
- Complete reviewable patch and passing target verification/secret policy.
- Exact backend-visible session trace with required contents and explicit flush.
- Stable trace ID/URL and structured local result.
- Defined rerun behavior proven.

### v0b / v0 complete

- All v0a conditions.
- Least-privilege GitHub authorization supplied safely.
- Correct base/head, no force-push, exactly reviewed files committed.
- One idempotent PR with structured body, verification evidence, and trace link.
- Retry/recovery behavior proven for remote partial failures.
- No credential in argv, URL, checkout, logs, patch, PR, or trace.

## Explicitly deferred after v0

The setup web application, GitHub App installation lifecycle, Railway production orchestration, live progress UI, multi-user/per-user budgets, evals dataset/scorers, and comment-to-refine loop remain v1/v2 work. Their future status must not be used to waive the v0 safety, reproducibility, patch, trace, or PR acceptance gates above.
