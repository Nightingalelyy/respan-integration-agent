# v0b PR delivery gap and implementation plan

Date: 2026-08-23

Baseline: `e3e8212fe760bea6d5adc87aa56c1f3c67f5a930` on
`v0-checklist-implementation`

Status: offline implementation complete; no v0b fixture delivery branch or pull
request was created, and the protected live acceptance gate remains open

## Checklist position

The literal next unchecked README item is still gateway preflight. Its agent-side
gate is implemented, but the recorded request to the configured backend readiness
endpoint returned `404`, so a fresh live pass still depends on backend deployment.

The next repository-local item that can be implemented and tested meanwhile is:

> `open_pr`: push branch + create PR via REST (`github.py`) - v0b

This plan does not waive the preflight dependency or mark either checkbox complete.
A live v0b run must pass gateway readiness, agent, patch, target-runtime, and exact
backend trace gates before it may write to GitHub.

## Implementation outcome

The repository-local stages in this plan are now implemented and tested. Delivery is
separated from `run_session()`, bound to immutable prepared/verification contracts,
rebuilt from the exact accepted base and patch, journaled before mutation, and exposed
only through the protected `scripts/run_v0b_smoke.py` fixture entrypoint. The normal
CLI rejects the removed plaintext token option and remains v0a-only.

See [V0_PR_DELIVERY_IMPLEMENTATION_2026-08-23.md](V0_PR_DELIVERY_IMPLEMENTATION_2026-08-23.md)
for code boundaries, offline validation, and the exact live acceptance work still
required. The historical gaps below describe the baseline this implementation closed.

## Finding

At the recorded baseline, implementing the `open_pr()` stub in place would have made
the smoke unsafe and would not have produced valid v0b evidence. Baseline
`run_session()` committed and called the stub before `scripts/run_v0_smoke.py` applied
the captured patch to a fresh checkout, installed it, ran the target, and verified
both traces in the backend. A target or trace failure could therefore have occurred
after a branch or PR had already been published.

The required lifecycle is:

```text
gateway preflight
  -> agent run
  -> complete patch capture and semantic verification
  -> fresh target install/run
  -> exact backend trace-content gate
  -> bind acceptance receipt to the prepared patch
  -> create/reuse exact commit and branch
  -> create/reuse exact pull request
```

Every failure before the acceptance receipt must produce zero GitHub mutations.

## Confirmed gaps

| ID | Priority | Baseline behavior | Required behavior |
| --- | --- | --- | --- |
| PR-01 | P0 | `run_session()` performs delivery before the smoke's target and backend gates. | Move every GitHub write into a post-acceptance delivery phase. |
| PR-02 | P0 | The same token is passed into checkout and later delivery; `_authed_url()` embeds it in the clone URL and process arguments. | Clone the trusted public v0b fixture without a token. Isolate a read-only credential check before paid work, scrub the credential from non-delivery child environments, and expose it again only to the bounded push/REST boundary after acceptance. |
| PR-03 | P0 | `repo_url` and `base_branch` are unrestricted delivery inputs. | Accept only an explicitly authorized fixture slug with a canonical `https://github.com/{owner}/{repo}[.git]` URL and validated branch ref. Prove unauthenticated repository/base access, and reject credentials, local paths, SSH, ports, query/fragment, custom hosts, private repositories, and workflow-file patches. |
| PR-04 | P0 | The accepted patch records paths and diff but not repository, base, or content identity; `commit_branch()` later stages the whole worktree with `git add -A`. | Freeze repository identity, base ref/SHA, canonical patch bytes/hash, and changed paths. Reapply only that patch in a fresh checkout and prove the resulting index/tree matches it. |
| PR-05 | P0 | The branch is fixed per product, and there is no base-movement check, collision policy, remote-ref lookup, or replay identity. | Derive a content-addressed branch from product, base SHA, and patch hash; create it atomically without overwriting any existing ref; reuse only an exact remote state and fail closed on divergence. |
| PR-06 | P0 | `open_pr()` is a `NotImplementedError` and lacks repository/base/acceptance inputs. | Add a strict GitHub REST client, exact head/base lookup, create/reconcile behavior, and validated typed result. |
| PR-07 | P0 | A lost push or lost PR response has no recovery path; the temporary checkout disappears. | Atomically journal non-secret recovery identity outside the checkout before the first mutation, update it after each phase, and reconcile the exact remote ref and PR before any later attempt. |
| PR-08 | P1 | The raw model summary is used as the proposed PR body. | Generate title/body from verified runner facts only, including patch identity, exact checks, trace links, and operator steps. |
| PR-09 | P1 | Git subprocesses have no explicit deadlines or isolated config/hooks; REST failure types and delivery tests do not exist. | Add bounded, redacted Git/HTTP operations, stable error codes, fault-injection tests, and ordering tests. |
| PR-10 | Dependency | The recorded live readiness request returned `404`. | Complete the separate gateway-preflight deployment/acceptance before any live v0b proof. |

## v0b scope and trust boundary

The first delivery implementation is deliberately narrow:

- trusted, disposable, public GitHub.com fixture repository only, enforced by an
  exact operator-provided owner/repository allowlist and unauthenticated access check;
- explicit delivery opt-in after all local and backend gates pass;
- a fine-grained credential scoped to that one fixture, with **Contents: write**
  for the push and **Pull requests: write** for PR creation;
- reject `.github/workflows/**` changes, so no Workflows permission is required;
- no private repositories, forks, GitHub Enterprise, custom Git hosts, SSH,
  auto-merge, labels, reviewers, branch overwrite, or unconditional force push;
- no branch or PR against `nightingalelyy/respan-integration-agent` or its upstream
  during implementation or smoke acceptance.

Private-repository credentials and untrusted repository execution remain coupled to
the following container/VM isolation checklist item.

## Proposed contracts

### 1. Prepared delivery

Refactor `run_session()` so it performs no remote write and returns an immutable
`PreparedDelivery` that is credential-free after bounded secret scanning, with at
least:

- canonical repository owner/name and base ref;
- exact base commit SHA;
- canonical patch bytes, SHA-256, and sorted changed paths;
- product/config fingerprint;
- agent run ID, trace ID, and trace URL;
- deterministic delivery fingerprint and proposed branch name.

Use a branch such as:

```text
respan/onboard-{product}-{base_sha_8}-{patch_sha256_12}
```

The parser must reject any value that cannot be represented unambiguously in the
GitHub API paths, Git ref, or `owner:branch` lookup.

The patch may still contain sensitive source even after bounded scanning. Keep patch
bytes only in restricted in-memory or mode-`0600` temporary storage, never in the
durable recovery journal, and clean them up on every normal exit.

### 2. Acceptance receipt

After the fresh target run and exact backend inspection, the smoke orchestrator
creates a frozen `VerificationReceipt`. It must bind to the prepared delivery
fingerprint and record only facts that actually passed:

- semantic verification;
- fresh dependency install and target command;
- agent and target run/trace IDs and trace URLs;
- gateway-readiness report identity;
- backend verification timestamp and evidence schema;
- patch SHA-256 and exact changed paths.

Delivery rejects a missing, failed, future-dated, or mismatched receipt before enabling
any GitHub mutation. An older exact receipt remains usable for deterministic recovery
because the frozen base is rechecked at every mutation boundary; expiring it would
make response-loss recovery impossible. A separate pre-paid readiness step may briefly
authenticate for read-only user/repository/base checks, but it must not create or
update any remote object and must scrub the token before the agent, install, target,
or telemetry path.

### 3. Delivery manifest and result

`open_pr(prepared, receipt, credential, journal)` returns either a redacted typed
error or a `DeliveryManifest` containing repository, base SHA, branch, commit SHA, PR
number/URL, delivery fingerprint, and whether each remote object was created or
reused. The strict REST boundary represents validated API results as
`PullRequestRecord` values.

Before the first remote mutation, atomically create a mode-`0600` journal in an
explicit operator-provided state directory outside the target checkout. Update and
`fsync` it after local commit, remote-ref observation/push, PR observation/create, and
completion, using atomic replace. It contains identifiers and phase only - never the
credential, patch bytes, source, request/response bodies, or raw Git output. An
injected journal interface makes kill-point and recovery tests deterministic.

## Git and credential design

1. Require the configured target to match the exact authorized fixture slug. Prove
   the repository/base are readable without authentication, clone without a token,
   and verify that `HEAD` equals the frozen base SHA. If any check needs the delivery
   credential or the base moved, stop.
2. Apply the frozen patch to a fresh checkout/index, rerun patch safety checks, and
   prove the canonical staged diff/hash/path set is identical. Reject ignored,
   staged, untracked, or late changes not represented by the accepted patch.
3. Disable hooks, signing, prompts, ambient credential helpers, and repository-local
   command overrides for commit/push. Set an explicit app identity and deadlines.
4. Create a commit whose sole parent is the frozen base and whose tree is the
   accepted tree. Record the safe provenance fingerprint in commit trailers.
5. Re-read the remote base immediately before push. Inspect the remote branch and
   reuse it only when its commit has the expected base/tree/fingerprint; otherwise
   return `G_BRANCH_COLLISION`. For an absent ref, use only the exact empty-old-ref
   lease `--force-with-lease=refs/heads/{branch}:` as a create-if-absent compare-and-
   swap. This lease cannot overwrite an existing branch; never use unconditional
   force or a lease that authorizes replacing an existing ref.
6. Push exactly `HEAD:refs/heads/{branch}` to the validated credential-free URL with
   that create-only lease.
   Pass `RESPAN_GITHUB_TOKEN` only to that bounded process through a temporary
   `GIT_ASKPASS` helper; set
   `GIT_TERMINAL_PROMPT=0` and an empty credential-helper chain.
7. Re-read the remote base before PR creation and validate the returned/reused PR's
   base ref and SHA. If it moves after a branch push, journal the branch, create no PR,
   and return `G_BASE_MOVED` for safe recovery.
8. Remove the current `--token VALUE` path. The v0b smoke reads
   `RESPAN_GITHUB_TOKEN` or an injected non-argument secret provider. A pre-paid
   read-only check may authenticate the user/repository/base but does not prove write
   permission. Remove the variable from `os.environ` before agent execution and
   construct explicit token-free environments for Claude, pip, the target, telemetry,
   and every subprocess except the bounded push/askpass pair. The REST client receives
   the credential directly in memory only after acceptance.

The code and tests must use hostile sentinel tokens to prove the value never appears
in command arguments, persisted environment or `.git/config`, non-delivery child
environments, exception text/causes, stdout/stderr, evidence, commit data, patch data,
PR text, or telemetry.

## GitHub REST design

Use a small injected client following the repository's existing direct, verified-TLS
transport pattern. Pin the origin to `https://api.github.com`, set a supported
`X-GitHub-Api-Version`, identify the client with `User-Agent`, bound connect/read/
overall time and response size, reject redirects, and strictly validate JSON fields.
Authorization exists only in an in-memory header whose representation is redacted.

Idempotent PR flow:

1. Query
   `GET /repos/{owner}/{repo}/pulls?state=all&head={owner}:{branch}&base={base}` with
   `per_page=100`. Follow strictly validated same-origin pagination to exhaustion
   within a fixed page limit.
2. Validate every returned repository, head/base ref, head/base SHA, and hidden
   delivery fingerprint.
3. Reuse one exact open PR. Treat an exact merged PR as completed. Treat a closed,
   unmerged PR, divergent result, or duplicate exact candidates as a typed conflict.
4. Only when no candidate exists, send
   `POST /repos/{owner}/{repo}/pulls` with exact `title`, `body`, `head`, and `base`.
5. After `422`, perform the exact lookup once before returning a conflict. After an
   ambiguous timeout or connection loss, reconcile with safe lookups until the shared
   deadline. If no exact PR becomes visible, return `G_RECOVERY_REQUIRED`; never issue
   a second POST in the same attempt. A later replay always begins with lookup.

Safe GETs use at most three attempts within a 30-second shared deadline and retry only
transport failures, `429`, and `500`, `502`, `503`, or `504`; a `403` is retryable only
when rate-limit headers prove exhaustion. Clamp `Retry-After`/reset delays to the
remaining deadline. POST is attempted at most once. Public failures expose stable
stage/code/status fields, not response bodies, URLs containing credentials, subprocess
commands, or exception chains.

The CLI emits `respan-v0b-smoke-evidence/v1` JSON only: exit `0` for created/reused
success, `2` for input/credential setup errors, `3` for transient or recovery-required
outcomes, and `4` for permanent integrity/authorization/conflict failures. Tests must
prove hostile Git stderr and HTTP bodies never enter that JSON or an exception chain.

Initial error families:

- `G_CONFIG`, `G_AUTH`, `G_FORBIDDEN`, `G_REPOSITORY_NOT_FOUND`;
- `G_BASE_MOVED`, `G_PATCH_MISMATCH`, `G_BRANCH_COLLISION`;
- `G_RATE_LIMIT`, `G_TRANSPORT`, `G_RESPONSE_SCHEMA`;
- `G_PR_CONFLICT`, `G_RECOVERY_REQUIRED`.

## Runner-generated PR body

Do not publish the model's prose. Generate a deterministic body containing:

- hidden delivery fingerprint for recovery;
- repository, base/head, base SHA, commit SHA, and patch SHA-256;
- exact changed-file list;
- only the gateway, semantic, target, and backend checks that passed;
- agent and target trace links;
- funding-aware operator instructions (do not claim credits were added for BYOK);
- a short disclosure that the change was generated and must be reviewed before merge.

## Implementation sequence

Each stage starts with failing tests and keeps delivery disabled until its exit gate
passes.

1. **Types and validation**
   - Add strict `RepositoryTarget`, `PreparedDelivery`, `VerificationReceipt`,
     `DeliveryManifest`, validated pull-request records, and redacted delivery errors.
   - Add canonical repository/ref parsing, exact fixture allowlisting,
     unauthenticated public-access proof, workflow-path rejection, and deterministic
     fingerprints.
   - Exit: hostile URL/ref/config and secret-representation tests pass.
2. **Separate preparation from acceptance**
   - Remove commit/PR work and the checkout token from `run_session()`.
   - Return the immutable prepared artifact; bind the smoke's successful target and
     backend evidence to it.
   - Exit: an ordering test proves every earlier failure makes zero Git/REST writes.
3. **Exact commit and push**
   - Recreate the accepted tree from the frozen base/patch, isolate Git behavior,
     implement branch lookup/collision/reuse, and atomically create the branch with
     the exact empty-old-ref lease (never overwriting an existing ref).
   - Exit: a local bare-remote suite proves one exact commit, exact base-race behavior,
     and credential-clean replay.
4. **Strict PR client and recovery**
   - Add exact lookup/create/reconcile behavior, safe status mapping, deterministic
     PR text, bounded pagination/retries, and the atomic recovery journal.
   - Exit: transport fault injection always ends with zero mutations or exactly one
     accepted branch, commit, and PR.
5. **CLI/smoke integration**
   - Add `scripts/run_v0b_smoke.py` as an explicit post-acceptance wrapper using the
     same preparation/verification core. Preserve `scripts/run_v0_smoke.py` as a
     zero-GitHub-mutation v0a path. Emit sanitized machine-readable
     `respan-v0b-smoke-evidence/v1` and remove plaintext token arguments.
   - Exit: offline end-to-end smoke with an injected readiness backend and fake
     Git/REST boundaries passes, while normal v0a behavior remains unchanged.
6. **Protected live acceptance**
   - First obtain a real gateway `PREFLIGHT_PASS`.
   - With explicit authorization, run against a dedicated disposable public fixture
     and a short-lived repository-scoped token, never the upstream/origin repo.
   - Replay the same prepared artifact and prove the same externally visible branch
     SHA and PR number are reused. Offline transport instrumentation must prove there
     was no second mutating request.
   - Leave the unmerged fixture PR as evidence and archive only sanitized identifiers.

## Offline test matrix

- repository URL, exact fixture allowlist, unauthenticated public status, ref,
  workflow-path, title/body, fingerprint, and receipt validation;
- exact base parent/tree/patch/path identity, including base movement before push,
  movement after push, and late files;
- hooks/signing/global config/credential helpers/prompts disabled and bounded Git
  timeouts;
- remote branch absent, exact reuse, divergent collision, and concurrent creation;
- exact REST method/path/query/headers/body and strict response parsing;
- empty, one, duplicate, open, merged, closed-unmerged, mismatched, and later-page PR
  lookups;
- `401`, non-rate-limit `403`, hidden `404`, `409`, `422`, `429`, `5xx`, invalid JSON,
  oversized content, redirect, timeout, and response loss;
- ambiguous push and POST reconciliation, interruption at every journal update,
  interruption after push, and interruption after PR creation;
- replay guarantees: final state is zero mutations or one accepted branch/commit/PR;
- token sentinel absent from argv, persisted configuration/environment, every
  non-delivery child, output, artifacts, errors, PR data, and trace surfaces;
- full ordering:
  `preflight -> agent -> patch -> target -> backend -> commit -> push -> PR`.

The offline suite should inject a passing readiness backend. It proves the delivery
implementation independently but does not replace the blocked live preflight or live
v0b acceptance.

## Checklist exit gate

Keep the README `open_pr` checkbox unchecked until a protected live run proves all of
the following from the same accepted artifact:

- successful gateway readiness and exact agent/target backend trace gates;
- unchanged expected base;
- exactly one remote branch and one commit above that base;
- committed tree and changed files exactly match the accepted patch;
- exactly one PR against the correct base with the generated evidence body and links;
- replay returns `reused` with the identical branch SHA and PR number/URL;
- no credential appears in Git config, arguments, persisted environment, non-delivery
  children, output, evidence, journal, commit, patch, PR, or trace data.

Only then mark `open_pr` complete. Do not claim private/untrusted repository support or
full checklist completion; isolation remains the following milestone.

## Primary references

- [GitHub REST pull request endpoints](https://docs.github.com/en/rest/pulls/pulls)
  for exact head/base lookup, creation, response states, and fine-grained token
  requirements.
- [GitHub App permission selection](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)
  for Git-over-HTTP Contents permission and avoiding unneeded Workflows permission.
- [GitHub REST rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
  for `Retry-After` and reset-aware failure handling.
- [GitHub credential security guidance](https://docs.github.com/en/rest/authentication/keeping-your-api-credentials-secure)
  for least privilege, secure storage, and avoiding plaintext command-line tokens.
- [Git credential interface](https://git-scm.com/docs/gitcredentials) for the
  `GIT_ASKPASS` and credential-helper behavior used by the proposed push boundary.
