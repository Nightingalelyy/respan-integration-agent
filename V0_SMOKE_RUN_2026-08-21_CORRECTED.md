# Corrected v0a smoke run — 2026-08-21

> Historical evidence: automated backend acceptance was subsequently implemented and
> passed on 2026-08-22. See
> [`V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md`](V0_SMOKE_RUN_2026-08-22_BACKEND_VERIFIED.md)
> for the current trusted-v0a verdict.

## Verdict

**PASS for the trusted v0a demo path; not full v0/v0b release acceptance.**

The onboarding agent produced the expected two-file integration, every deterministic
patch gate passed, a fresh target environment installed and ran successfully, and both
the agent and target records were found and inspected by exact marker in Respan. No
GitHub token was used and no PR was attempted.

The remaining dependency, platform projection, automatic backend-verification,
isolation, preflight, and v0b gaps are listed below rather than hidden behind the pass.

## Reproducible inputs

- Repository base commit: `e7b3c4342ee7066c629f2813a7f0dd4f5be85c08`
- Implementation state: uncommitted working tree on that base; record the final commit
  SHA after review/commit
- Trusted fixture base commit: `7931337a2ae35d5a8400b95dd2f919c1fe6dec2d`
- Python: 3.12.13 (declared support: 3.11–3.13)
- Agent model alias: `sonnet`
- Agent gateway/telemetry endpoint: `https://api.respan.ai/api` (the passing run used this
  default; the final harness now pins it explicitly and ignores ambient overrides)
- Agent budget: 40 turns, USD 1.00, 300 seconds
- `claude-agent-sdk==0.2.143`
- Bundled Claude CLI: 2.1.238
- `respan-ai==4.1.0`
- `respan-instrumentation-claude-agent-sdk==0.2.0`
- `opentelemetry-claude-agent-sdk==0.1.4`
- Target `openai==1.99.9`
- Target `opentelemetry-instrumentation-openai==0.62.3`
- Target gateway: `https://api.respan.ai/api` (the passing run used this default; the final
  harness now pins it and ignores ambient overrides)
- Target model: `gpt-4o-mini` (the passing run used this default; the final harness now
  pins it and ignores ambient overrides)
- Target package index: `https://pypi.org/simple` via isolated pip with a credential-free
  environment; transitive artifact hashes remain a reproducibility gap
- Pinned Respan skill `SKILL.md` SHA-256:
  `362573c74407e81c9643443518ff00e09457302c279c9325c4461b5e8d1b1184`
- Final locally verified wheel SHA-256:
  `544882d875745a2a2b9e8d7e8d7c8dd130c670556598650dbf2228196bcc3dd0`

Only `RESPAN_API_KEY` was loaded from the ignored repository `.env`. The value was not
printed, placed in the target checkout, or passed to the editing agent as
`RESPAN_API_KEY`/`OPENAI_API_KEY`/`GITHUB_TOKEN`.

## Execution evidence

- Start: `2026-08-21T09:37:28.144620Z`
- End: `2026-08-21T09:38:29.408425Z`
- Agent run marker: `respan-v0a-agent-d7e96720448f`
- Agent SDK session: `aa5f660b-5e62-4b7a-b6d0-1e2a5e393b89`
- Agent turns: 11
- Agent SDK reported cost: USD 0.105432
- Agent trace: [`64a18558bf683ca4218d64720aa1bb27`](https://platform.respan.ai/platform/traces?trace_unique_id=64a18558bf683ca4218d64720aa1bb27)
- Target run marker: `respan-v0a-target-d7e96720448f`
- Target trace: [`9064850f8f689aba0dc6257737d1ea63`](https://platform.respan.ai/platform/traces?trace_unique_id=9064850f8f689aba0dc6257737d1ea63)
- Changed paths: exactly `app.py` and `requirements.txt`
- Target stdout: exactly `SMOKE_OK`

Before the passing run, a no-prompt discovery attempt correctly failed because Claude's
`--bare` mode did not advertise the temporary user-directory skill. No model turn or
patch was accepted. The corrected boundary retains a temporary user config, only the
`user` setting source, empty MCP/plugin configuration, restricted tools, disabled
session persistence, and an exact `respan` discovery check before the first model turn.

## Accepted patch

```diff
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@
 from openai import OpenAI
+from respan import Respan
+
+respan = Respan(
+    app_name="respan-v0a-python-smoke",
+    environment="smoke",
+    metadata={"run_id": os.environ["RESPAN_EXAMPLE_RUN_ID"]},
+)
@@
 if __name__ == "__main__":
-    main()
+    try:
+        main()
+    finally:
+        respan.flush()
diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@
 openai==1.99.9
+respan-ai==4.1.0
+opentelemetry-instrumentation-openai==0.62.3
```

## Local acceptance

- The complete patch includes staged, unstaged, untracked, deleted, renamed, and binary
  states by construction and replayed with `git apply --check`.
- The changed-path allowlist, exact requirements, AST contract, initialization order,
  retained handle, `finally` flush, source compilation, and secret/binary policies passed.
- The existing `main()` AST was unchanged.
- A fresh target virtual environment installed the three exact dependencies.
- `pip check` passed.
- The real target call exited zero, printed only `SMOKE_OK`, and did not emit a traceback
  or the API key.
- Repository checks after the final false-pass fixes: Ruff passed and 48 tests passed.
- A built wheel contained the exact bundled skill and clean-installed with `pip check` and
  a working CLI.

## Agent trace inspection

The exact agent trace contains 11 spans with one root and no duplicate IDs:

- one workflow root;
- one `agent.respan-integration-agent` chat span;
- one `tool.Skill` span;
- three `tool.Read` spans;
- two `tool.Glob` spans;
- three `tool.Edit` spans.

All 11 spans are successful with status 200 and no recorded errors. All nine tool spans
have non-empty input and output. The skill span invoked `respan`, a Read span consumed the
pinned `references/tracing.md`, and no Bash, web, or sub-agent span exists. The trace has
689 prompt tokens, 1,386 completion tokens, and the exact run marker. Inspection found no
duplicate/orphan span, `Span not properly closed`, or `sk-`/`rk-`/`pk-`-shaped value.
All three recorded Edit inputs target only `app.py` or `requirements.txt`; no hidden file
write tool was available or invoked.

This corrects the earlier run's all-zero trace ID, 13 improperly closed spans, missing
tool outputs, web fallback, and unrestricted Bash behavior.

## Target trace inspection

The exact target trace contains one successful root chat span:

- span: `llm.gpt-4o-mini`;
- type: `chat`;
- provider: OpenAI;
- model: `gpt-4o-mini`;
- tokens: 28 prompt, 13 completion, 41 total;
- cost: USD 0.000012;
- input: the exact target marker request;
- output: exactly `respan-v0a-target-d7e96720448f`;
- status: success/200, no error;
- service metadata: `respan-v0a-python-smoke`;
- environment metadata: `smoke`;
- instrumentation scope: `opentelemetry.instrumentation.openai.v1==0.62.3`.

There is one root, no duplicate span, no target workflow/task/agent/tool span, no
improperly closed span, and no token-shaped credential value.

## Remaining gaps and plan

1. **Published SDK packaging:** `respan-ai` 4.2.1–4.2.3 cannot clean-install because
   their wheels require instrumentation versions not published on PyPI. The smoke uses
   4.1.0 plus the direct OpenAI OTel package. Publish/fix the missing dependencies, then
   remove this compatibility pin and rerun the same fixture against the current SDK.
2. **Environment projection:** both trace records expose top-level `environment=prod`
   even though their metadata correctly contains `respan.environment=onboarding` and
   `smoke`. Fix ingestion/UI projection or document the intended distinction, then add
   an assertion for the resolved contract.
3. **Agent cost projection:** the Claude SDK result and span metadata record USD
   0.105432, but the agent span and trace aggregate expose cost zero. Fix aggregation and
   require the platform value to equal the SDK terminal result within a documented rule.
4. **Target span contract:** the compatibility OTel path stores `llm.gpt-4o-mini`, not
   the native `openai.chat` name expected from the current curated Respan path. Recheck
   the name and attributes after the 4.2 packaging repair.
5. **Backend gate automation — resolved 2026-08-22:** the smoke now requires a bounded,
   exact-marker tree/list/full-detail gate and emits `BACKEND_VERIFIED_PASS` only after
   both trace contracts pass. See the current evidence linked above.
6. **Isolation:** this smoke is approved only for the checked-in trusted fixture. A
   disposable container/VM, egress controls, and safe private-clone credentials remain
   required before accepting untrusted repositories.
7. **Product completion:** orchestration gateway readiness preflight, multi-session
   process lifecycle, rerun/idempotency, and v0b PR delivery remain open.
8. **Reproducibility:** replace the mutable `sonnet` alias with an immutable supported
   model identifier and lock the tested transitive dependency set.

The earlier failed run and broad backlog remain recorded in
[`V0_SMOKE_RUN_2026-08-21.md`](V0_SMOKE_RUN_2026-08-21.md) and
[`V0_GAP_AND_PLAN.md`](V0_GAP_AND_PLAN.md).
