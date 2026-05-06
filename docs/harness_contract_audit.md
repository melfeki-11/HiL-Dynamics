# Harness Contract Audit

This document records the human-interaction surfaces that Trust Horizon captures for real-agent HiL-SWE runs. It is intentionally implementation-facing: changes to any SDK adapter should update this audit and the trajectory validator.

## Shared Contract

Every harness adapter implements:

```js
{
  name,
  defaultModel,
  async runAttempt({ row, attemptIndex, args, runDir }) -> prediction
}
```

Every attempt writes:

- `prompt.md`
- `attempt.json`
- `trajectory.jsonl`
- `patch.diff`
- `prediction.json`

All trajectory writes go through `appendJsonl`, which redacts secret-shaped values and normalizes each event into the common schema consumed by `scripts/validate_trajectories.py`, `scripts/process_metrics.py`, and `scripts/summarize_passk.py`.

## Mode Boundary

- `ask_human`: mounts the shared `ask_human` service, snapshots the blocker registry into the private run directory, enables the deterministic cache, and includes a prompt instruction telling the agent when to ask targeted clarification questions.
- `full_info`: uses the prepared `input_full_info.jsonl` prompt content, disables the `ask_human` service even if `--human-kb` is supplied, and does not advertise any clarification tool.
- `baseline`: disables the `ask_human` service and does not add HiL clarification instructions.

The blocker registry is never copied into an agent workspace. It is only read by the harness process or the MCP bridge process.

## Claude Code

Implementation: `src/harnesses/claude-code/index.mjs`

Audited interaction paths:

- SDK messages from `@anthropic-ai/claude-agent-sdk` are logged as `sdk_message`.
- MCP clarification calls to `human_input.ask_human` are routed through `createHumanInputRouter.route` and logged as raw request, normalized request, and result.
- Tool permission requests are routed through `createHumanInputRouter.routeApproval`, not through `ask_human`.
- Tool calls, tool results, final result messages, SDK errors, patch submission, and attempt end are normalized.

Default model: `claude-sonnet-4-6`.

## Codex

Implementation: `src/harnesses/codex/index.mjs` and `src/harnesses/codex/app_server.mjs`

Audited interaction paths:

- In `ask_human` mode, Codex defaults to app-server transport so JSON-RPC requests can be captured.
- `item/tool/requestUserInput` and elicitation-like app-server requests are routed to `createHumanInputRouter.route`.
- Command/file approval and permission requests are routed to `createHumanInputRouter.routeApproval`.
- App-server requests/responses are logged as `codex_app_server_request` and `codex_app_server_response`.
- Codex SDK item events are logged as `sdk_event` and normalized into command, test, file edit, tool call/result, reasoning, final, and error events.

Default model: `gpt-5.5` with reasoning effort `low`.

## OpenCode

Implementation: `src/harnesses/opencode/index.mjs` and `src/harnesses/opencode/server_pool.mjs`

Audited interaction paths:

- OpenCode is launched with an isolated `HOME`, `XDG_CONFIG_HOME`, and `XDG_DATA_HOME` per attempt.
- The provider is LiteLLM through OpenAI-compatible OpenCode provider config.
- `human_input.ask_human` is exposed through `src/shared/judge_mcp.mjs`.
- SSE events from `client.event.subscribe()` are logged as `opencode_event`.
- `permission.updated` is handled by the harness and answered through `createHumanInputRouter.routeApproval`.
- Permission replies are logged as `opencode_permission_response`.
- OpenCode server ports are allocated through the port pool and released in `finally`.

Default model: `bedrock/qwen.qwen3-32b-v1:0`.

## Shared Ask-Human Service

Implementation: `src/shared/human_input.mjs` and `src/shared/judge_mcp.mjs`

The service:

- returns exact registry resolutions only after selecting one valid blocker id;
- returns `irrelevant question` for irrelevant, broad, exfiltration, malformed, no-candidate, duplicate-cap-exceeded, provider-failure, and non-clarification requests;
- caches raw selector decisions before per-attempt answer-cap logic;
- keys the cache by instance, request type, normalized question, options, registry hash, selector prompt hash, selector schema hash, selector version, and judge model;
- uses lock files plus in-process locks so concurrent harness workers cannot corrupt the cache or duplicate the same selector call.

## Required Verification

Before a benchmark run:

```bash
npm test
npm run preflight -- --skip-docker
```

After a generation run:

```bash
npm run validate-trajectories -- --run-id "$RUN_ID"
python3 scripts/leakage_audit.py --run-id "$RUN_ID" --human-kb evals/"$RUN_ID"/human-kb.json
python3 scripts/summarize_passk.py --run-id "$RUN_ID" --human-kb evals/"$RUN_ID"/human-kb.json --k 3
python3 scripts/process_metrics.py --run-id "$RUN_ID" --human-kb evals/"$RUN_ID"/human-kb.json
```
