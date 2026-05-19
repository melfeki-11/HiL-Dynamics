# Harness Asymmetries

This project compares real native harness behavior, so the adapters are intentionally not identical. These are the known asymmetries that must be kept visible in reports and PRs.

## Reasoning Effort

| Harness/config | Model | Reasoning effort |
|---|---|---|
| `claude.yaml` | `claude-opus-4-7` | `xhigh` |
| `codex.yaml` | `gpt-5.5` | `xhigh` |
| `adk.yaml` | `gemini/gemini-3.1-pro` | `high` |
| `opencode.yaml` | `fireworks_ai/glm-5p1` | `high` |
| `opencode_claude.yaml` | `claude-opus-4-7` | `xhigh` |
| `opencode_codex.yaml` | `gpt-5.5` | `xhigh` |
| `opencode_gemini.yaml` | `gemini/gemini-3.1-pro` | `high` |

Gemini uses `high` because that is the highest supported reasoning effort for the configured Gemini 3.1 Pro route. The configured effort is recorded in attempt metadata. For ADK and OpenCode Gemini routes, the harness does not forward OpenAI-style `reasoning_effort` / `reasoning` transport parameters when LiteLLM rejects them for Gemini; those unsupported fields are stripped or omitted and logged as transport compatibility behavior, not as model performance.

## Budgets

Budgets are unbounded by default (`MAX_STEPS=0`). The harnesses expose different native concepts of a turn, item, model call, and tool call, so this branch avoids forcing a misleading common cap. Fairness is audited by logging observed:

- wall-clock duration
- LLM calls
- tool calls
- harness-native turns/items
- input/output/total tokens when available

The run report surfaces these fields before the scorecard is interpreted.

## Clarification Surfaces

The user-facing question surfaces differ by harness, but they now converge on the same backend:

| Harness | Question surface(s) | Backend |
|---|---|---|
| Claude Code | Native `AskUserQuestion` plus explicit `human_input.ask_human` MCP in `neutral`/`skill` | `ask_human_sidecar.mjs` |
| Codex | Native `requestUserInput` plus explicit `human_input.ask_human` MCP in `neutral`/`skill` | `ask_human_sidecar.mjs` |
| ADK | Python `ask_human` function tool | `ask_human_sidecar.mjs` |
| OpenCode | MCP `human_input.ask_human` | `ask_human_sidecar.mjs` |

For Claude/Codex, `WITH_CUSTOM_TOOL=0` can hide the explicit MCP tool while preserving native question interception. Reports should still distinguish native vs. MCP calls in trajectory `act` strings (`[native]` vs. `[custom_mcp]`), but both count as clarification attempts when they reach the sidecar.

## ADK

The ADK runner uses a wrapped `LlmAgent` with bash/editor tools and an optional `ask_human` sidecar. This is not a fully native coding-agent product surface in the same sense as Claude Code or Codex, so ADK results should be interpreted as "ADK harness with Gemini" rather than "a first-party coding CLI."

## OpenCode

OpenCode is evaluated both with its configured open model (`fireworks_ai/glm-5p1`) and with same-model configs for Claude, GPT, and Gemini. OpenCode may route through compatibility shims for LiteLLM/OpenAI-compatible providers; stripped or unsupported parameters must be logged when discovered.

The OpenCode LiteLLM shim exposes per-run proxy diagnostics, including upstream status counts, stripped parameter counts, recent upstream errors, LLM request count, and token usage when the upstream response includes `usage`.

## Skill Arm

The `skill` arm differs from `neutral` only by installing the domain-general clarification skill. The system/developer prompt does not include the old `ask_human_guidance.txt` text unless `ASK_HUMAN_GUIDANCE=1` is explicitly set for a diagnostic run.
