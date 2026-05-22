# Harness Contract Audit

This document records the human-interaction surfaces that HiL-Dynamics captures for real-agent HiL-SWE runs. It is intentionally implementation-facing: changes to any SDK adapter should update this audit and the trajectory validator.

## Shared Contract

Every harness adapter implements:

```js
{
  name,
  defaultModel,
  async runAttempt({ row, attemptIndex, args, runDir }) -> prediction
}
```

Every HiL-SWE attempt writes:

- `attempt.json`
- `trajectory.json`
- `stats.json`
- `patch.diff`
- `result.json`
- `eval_result.json` after evaluator execution

The harness entrypoints also keep an in-memory raw event stream during each pass. That stream is normalized into SWE-agent-style `{thought, act, obs}` steps plus `stats.json` counters consumed by `scripts/metrics_hil_swe.py` and `scripts/hilbench_analyze.py`.

## Mode Boundary

- `neutral`: mounts the shared `ask_human` service, snapshots the blocker registry into the private run directory for the harness/router only, enables the deterministic cache, and exposes the clarification tool with no additional help-seeking guidance or skill.
- `skill`: same as `neutral`, plus the domain-general clarification skill installed in the harness-specific skill location.
- `full_info`: uses prompt content that includes blocker resolutions upfront, disables the `ask_human` service, and does not advertise any clarification tool.
- `no_tool`: uses the task prompt without blocker resolutions and without the `ask_human` service; this is useful as a lower-bound control.

The blocker registry is never copied into an agent workspace. It is only read by the harness process or the MCP bridge process.

## Claude Code

Implementation: `src/hil_swe/run_claude.mjs`

Audited interaction paths:

- SDK messages from `@anthropic-ai/claude-agent-sdk` are logged as `sdk_message`.
- Native `AskUserQuestion` calls are intercepted in `canUseTool`, answered by `ask_human_sidecar.mjs`, and returned to Claude as `allow` with prefilled answers.
- Explicit MCP clarification calls to `human_input.ask_human` are exposed in `neutral`/`skill` by default and routed through the same sidecar backend.
- Tool permission requests are handled by the container policy, not by `ask_human`.
- Tool calls, tool results, final result messages, SDK errors, patch submission, and attempt end are normalized.

Default model for this branch's HiL-SWE runner: `claude-opus-4-7` with reasoning effort `xhigh`.

## Codex

Implementation: `src/hil_swe/run_codex.mjs`

Audited interaction paths:

- In `neutral`/`skill` mode, Codex defaults to app-server transport so JSON-RPC requests can be captured.
- Native `item/tool/requestUserInput` and elicitation-like app-server requests are routed to `ask_human_sidecar.mjs`.
- Explicit MCP clarification calls to `human_input.ask_human` are exposed in `neutral`/`skill` by default and routed through `ask_human_mcp_bridge.mjs` to the same sidecar.
- Command/file approval and permission requests are accepted inside the Docker sandbox.
- App-server requests are logged as `codex_server_request`.
- Codex SDK item events are logged as `sdk_event` and normalized into command, test, file edit, tool call/result, reasoning, final, and error events.

Default model: `gpt-5.5` with reasoning effort `xhigh`.

## OpenCode

Implementation: `src/hil_swe/run_opencode.mjs`

Audited interaction paths:

- OpenCode is launched with an isolated `HOME`, `XDG_CONFIG_HOME`, and `XDG_DATA_HOME` per attempt.
- The provider is LiteLLM through OpenAI-compatible OpenCode provider config.
- `human_input.ask_human` is exposed through `src/hil_swe/ask_human_mcp_bridge.mjs`, which calls the shared ask-human sidecar.
- SSE events from `client.event.subscribe()` are logged as `opencode_event`.
- Edit/bash permissions are allowed inside the Docker sandbox; web fetch and external-directory permissions are denied.
- OpenCode server ports are allocated through the port pool and released in `finally`.

Default model: `fireworks_ai/glm-5p1` with reasoning effort `high`. Additional OpenCode configs run `claude-opus-4-7`, `gpt-5.5`, and `gemini/gemini-3.1-pro` through the same OpenCode harness for same-model comparisons.

## Google ADK

Implementation: `src/hil_swe/run_adk.py`

Audited interaction paths:

- ADK is wrapped as an `LlmAgent` with bash/editor tools plus optional `ask_human`.
- Function-call and function-response events are normalized into the shared trajectory.
- The ask-human sidecar is started only in `neutral`/`skill` mode.
- The skill is installed only in `skill` mode.

Default model: `gemini/gemini-3.1-pro` with reasoning effort `high` because `high` is the highest supported setting for this Gemini route.

## Shared Ask-Human Service

Implementation: `src/shared/human_input.mjs`, `src/hil_swe/ask_human_sidecar.mjs`, `src/hil_swe/ask_human_sidecar_client.mjs`, and `src/hil_swe/ask_human_mcp_bridge.mjs`

The service:

- returns exact registry resolutions only after selecting one valid blocker id;
- returns `irrelevant question` for irrelevant, broad, exfiltration, malformed, no-candidate, duplicate-cap-exceeded, and non-clarification requests;
- returns `can't answer (perhaps transient hiccup)` for retryable provider/system failures;
- caches raw selector decisions before per-attempt answer-cap logic;
- keys the cache by instance, request type, normalized question, options, registry hash, selector prompt hash, selector schema hash, selector version, and judge model;
- uses lock files plus in-process locks so concurrent harness workers cannot corrupt the cache or duplicate the same selector call.

Judge model: configured via `ASK_HUMAN_MODEL` in `.env` (any LiteLLM-compatible model slug). Setup fails loudly if the judge probe cannot reach the configured route or returns non-canonical calibration responses.

## Budget and Resource Contract

Budgets are unbounded by default (`MAX_STEPS=0`) across harnesses. The fairness comparison relies on observed resource logging instead of trying to collapse different harness-native turn units into one artificial cap. Every pass should write best-effort:

- wall-clock duration
- LLM calls
- tool calls
- harness-native turns/items
- input, output, and total tokens when exposed by the provider/harness

## Required Verification

Before a benchmark run:

```bash
npm test
./bin/hilbench setup --strict --slice public3
```

After a generation run:

```bash
npm run validate-trajectories -- --run-id "$RUN_ID"
python3 scripts/metrics_hil_swe.py --run-id "$RUN_ID" --passes 3 --print
python3 scripts/hilbench_analyze.py --run-id "$RUN_ID" --passes 3
```
