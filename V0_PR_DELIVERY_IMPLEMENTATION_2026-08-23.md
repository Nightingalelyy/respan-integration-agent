# v0b PR delivery implementation evidence

Date: 2026-08-23

Branch: `v0-checklist-implementation`

Baseline: `e3e8212fe760bea6d5adc87aa56c1f3c67f5a930`

Verdict: `OFFLINE_IMPLEMENTED_LIVE_ACCEPTANCE_PENDING`

## Outcome

The repository now has a protected post-acceptance delivery path for the v0b
`open_pr` checklist line. It can rebuild one immutable accepted patch, create or reuse
one content-addressed branch, and create or reconcile one exact pull request through
the GitHub REST API. The normal v0a CLI and `run_session()` perform no GitHub write.

No v0b fixture delivery branch or pull request was created while implementing or
validating this slice. The existing `v0-checklist-implementation` source branch is the
only remote publication in scope, and no pull request is opened from it. In
particular, `nightingalelyy/respan-integration-agent` is hard-denied by the protected
smoke entrypoint.

## Implemented boundaries

- `delivery.py` defines strict `RepositoryTarget`, `PreparedDelivery`,
  `VerificationReceipt`, `DeliveryManifest`, redacted errors, and a monotonic atomic
  recovery journal. Patch bytes and credentials are excluded from durable evidence.
- `runner.py` now freezes and rechecks the exact base commit and returns only a
  validated patch. Its legacy `github_token` parameter fails before preflight, clone,
  or model execution.
- `sandbox.py` accepts local paths or credential-free canonical HTTPS clones, rejects
  SSH/scp and credential-bearing URLs, uses an empty Git home, disables ambient Git
  configuration and prompts, and binds the checkout to a nonzero commit SHA.
- `scripts/run_v0_smoke.py` performs the fresh replay/install/target/backend sequence,
  validates the exact trace-gate schema and trace identities, and only then constructs
  the prepared delivery and matching verification receipt.
- `github.py` uses a strict direct-TLS REST client plus isolated Git operations. It
  validates authenticated and anonymous public-repository/base identity, reconstructs
  the exact accepted tree, and creates a deterministic commit. Git runs through a
  fixed executable and path with selector-drained output caps and deadlines; a
  timeout or overflow kills and reaps the isolated Git process group, including
  credential-bearing helpers. REST applies bounded connect/read/overall deadlines and
  response-size caps. A missing branch is created with an exact empty-old-ref lease
  that cannot overwrite a concurrently created ref. It also
  checks for base movement and branch collisions, rejects future-dated or mismatched
  verification receipts while retaining older exact evidence for recovery, validates
  the exact generated PR title/body on creation and reuse, reconciles ambiguous PR
  creation, preserves conflicting PR identities and observed SHAs, and returns
  complete recovery identity if journaling fails after a confirmed remote mutation.
- `scripts/run_v0b_smoke.py` enforces one explicit public fixture allowlist, performs
  read-only GitHub preflight before paid work, strips GitHub credentials from all
  non-delivery environments, requires the same preflight base SHA at acceptance, and
  invokes delivery only with the immutable receipt.
- The CLI rejects every formerly accepted `--token` spelling without reflecting the
  supplied value and remains a mutation-free v0a interface.

## Offline acceptance evidence

The automated suite covers hostile URL/ref/token inputs, strict contracts and journal
permissions/transitions, local bare-remote commit/push creation and replay, base races,
branch collisions, Git isolation, REST schemas/pagination/retries/status mapping,
ambiguous POST recovery, PR creation/reuse/conflict, and complete protected-runner
ordering.

Validation from the implementation worktree:

```text
python -m pytest -q                         565 passed
python -m pytest -q tests/test_github.py     62 passed
ruff check agent/src agent/tests scripts     passed
ruff format --check <changed Python files>  passed
python -m compileall -q agent/src scripts    passed
python -m pip check                          no broken requirements
git diff --check                            passed
run_v0b_smoke.py without a GitHub token      G_CONFIG, exit 2
```

This is offline implementation evidence. It is not a claim that GitHub accepted a
real branch or PR, and it does not satisfy the live checklist exit gate.

## Remaining live acceptance gap

The README checkbox stays open until one explicitly authorized disposable public
fixture run proves, from the same delivery fingerprint:

1. live gateway readiness passes before any paid execution;
2. the agent patch, fresh target, and exact agent/target backend trace-content gates
   all pass;
3. exactly one branch and deterministic commit are present above the frozen base;
4. exactly one PR targets the expected base with the generated evidence body;
5. replay returns the same branch SHA and PR number/URL without a second mutation; and
6. the GitHub credential is absent from arguments, Git config, non-delivery children,
   evidence, journal, commit, PR body, and trace data.

GitHub does not offer a conditional PR-create operation bound to a base commit SHA.
The client therefore rechecks the base immediately before its single POST and validates
the returned base SHA. If the base moves in that final interval, it fails with
`G_BASE_MOVED` and durably journals the created PR identity for recovery. The first
live fixture must also keep its base branch quiescent during delivery.

The separate gateway-readiness checklist dependency must first have a deployed,
working backend endpoint. Private repositories and untrusted-repository execution
remain out of scope until the following container/VM isolation milestone.
