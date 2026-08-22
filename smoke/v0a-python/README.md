# Respan v0a Python smoke fixture

A trusted Python 3.11–3.13 application with one OpenAI-compatible chat-completions
request routed through the Respan gateway. The baseline intentionally has no tracing SDK.

Required environment:

- `RESPAN_API_KEY`
- `RESPAN_EXAMPLE_RUN_ID`, formatted as `respan-v0a-<unique-id>`

Optional environment:

- `RESPAN_BASE_URL` — defaults to `https://api.respan.ai/api`
- `RESPAN_SMOKE_MODEL` — defaults to `gpt-4o-mini`

Successful stdout is exactly `SMOKE_OK`. The exact marker is intentionally not printed;
it should appear in the target LLM telemetry record instead.
