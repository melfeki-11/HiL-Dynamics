# HiL-Dynamics — Run Output Schema

All run outputs are written under `runs/`. Nothing in `runs/` is committed to git.

## Directory Layout

```
runs/
  <run_id>/                           e.g. "claude_smoke_20260518_153000"
    <attempt_uid>/                    e.g. "69bc1094b455a91fa20fb868"
      <mode>/                         "ask_human" | "full_info"
        pass_1/
          attempt.json
          trajectory.json
          patch.diff
          stats.json
          result.json
          eval_result.json
        pass_2/
          ...
        pass_3/
          ...
    metrics/
      pass_level.json                 One row per (attempt, mode, agent, model, pass)
      summary.json                    Aggregated after all passes complete
    report.md
    metadata.json
```

## File Formats

### `trajectory.json`
A JSON array of steps. Each step:
```json
[
  {
    "act": "str — the action taken (tool call name + args, or ask_human call)",
    "obs": "str — the observation/response",
    "thought": "str — (optional) agent reasoning / thinking block",
    "response": "str — (optional) raw LLM response text",
    "tool_calls": "str — (optional) JSON-encoded tool calls",
    "execution_time": "str — (optional) seconds"
  }
]
```
Clarification calls are normalized into ordinary steps. The `act` prefix distinguishes the surface:

- `ask_human [native] ...` for Claude `AskUserQuestion` and Codex `requestUserInput`
- `ask_human [custom_tool] ...` for explicit custom ask_human tool routing
- `ask_human [other] ...` for non-primary fallback tool-name paths

The matching `human_input_*` raw events retain the exact native event type:
```json
{
  "type": "human_input_raw_event",
  "request_type": "clarification",
  "native_event_type": "claude.AskUserQuestion.canUseTool | codex.item/tool/requestUserInput | claude.mcp.ask_human | codex.mcp.ask_human | adk.ask_human | antigravity.ask_question",
  "question": "<question text>"
}
```

### `patch.diff`
Plain text output of `git diff HEAD` from inside the Docker container at the end of the agent run.
Empty string if no changes were made.

### `stats.json`
```json
{
  "resolved": false,
  "status": "unresolved | resolved | infra_error",
  "num_questions": 2,
  "num_blockers_resolved": 1,
  "total_num_blockers": 4,
  "precision": 0.5,
  "recall": 0.25,
  "f1": 0.333,
  "cost_usd": 0.42,
  "num_steps": 28,
  "tokens_sent": 18400,
  "tokens_received": 3200,
  "wall_clock_ms": 312400,
  "num_llm_calls": 19,
  "num_tool_calls": 11,
  "num_turns_or_items": 42,
  "input_tokens": 18400,
  "output_tokens": 3200,
  "total_tokens": 21600,
  "agent": "claude-code",
  "model": "claude-opus-4-7",
  "mode": "ask_human",
  "attempt_uid": "69bc1094b455a91fa20fb868",
  "pass_num": 1,
  "run_id": "claude_smoke_20260518_153000",
  "duration_seconds": 312.4
}
```

### `metrics/pass_level.json`
Each row is a normalized pass-level record with solve/eval status, ask metrics, resource usage, and a `pass_dir` pointer:

```json
{
  "uid": "69bc1094b455a91fa20fb868",
  "mode": "ask_human",
  "agent": "claude",
  "model": "claude-opus-4-7",
  "pass_index": 1,
  "status": "unresolved",
  "resolved": false,
  "num_steps": 28,
  "num_questions": 2,
  "num_blockers_resolved": 1,
  "num_blockers_total": 4,
  "wall_clock_ms": 312400,
  "num_llm_calls": 19,
  "num_tool_calls": 11,
  "num_turns_or_items": 42,
  "input_tokens": 18400,
  "output_tokens": 3200,
  "total_tokens": 21600,
  "llm_proxy_error_count": 0,
  "llm_proxy_status_counts": {"200": 19},
  "llm_proxy_stripped_params": {"tool_choice": 19},
  "pass_dir": "runs/<run-id>/<uid>/ask_human/pass_1"
}
```

### `metrics/summary.json`
Written after all passes for a run_id complete:
```json
{
  "metadata": {
    "run_id": "...",
    "num_passes": 3,
    "include_partial": false,
    "generated_at": "2026-05-18T22:30:00Z",
    "formula": "micro/global totals (resolved = unique blocker IDs per pass)"
  },
  "by_mode_agent_model": {
    "ask_human/claude/claude-opus-4-7": {
      "num_attempts": 3,
      "pass_at_1": 0.33,
      "pass_at_3": 0.67,
      "ask_precision": 0.6,
      "ask_recall": 0.4,
      "ask_f1": 0.48,
      "avg_steps_per_pass": 24.1,
      "avg_questions_per_pass": 1.8,
      "avg_wall_clock_ms_per_pass": 312400,
      "avg_llm_calls_per_pass": 19,
      "avg_tool_calls_per_pass": 11,
      "avg_turns_or_items_per_pass": 42,
      "total_input_tokens": 55200,
      "total_output_tokens": 9600,
      "total_tokens": 64800
    }
  }
}
```

## Metrics Formulas

Ask metrics use **micro/global totals** over valid pass rows (matching `scripts/metrics_hil_swe.py`):

```
ask_precision = min(1, sum(num_blockers_resolved) / sum(num_questions))
ask_recall    = min(1, sum(num_blockers_resolved) / sum(total_num_blockers))
ask_f1        = harmonic_mean(ask_precision, ask_recall)

pass@k: fraction of attempts where ANY of the first k passes resolved the task
```

A pass is marked `infra_error` or excluded from aggregate metrics if the solve/eval path failed, or if its trajectory contains:
- 3+ timeout observations
- 1+ "can't answer (perhaps transient hiccup)" observations
- "Environment died unexpectedly" as the last observation
- "Exit due to unknown error" in the last step's response

Budgets are intentionally unbounded by default. Fairness is audited by reporting observed LLM calls, tool calls, turns/items, token usage, and wall-clock duration rather than forcing every harness into an artificial shared turn unit.

## Key Field Definitions

| Field | Description |
|-------|-------------|
| `attempt_uid` | The HF dataset `uid` field = the Docker image name suffix |
| `instance_id` | The HF dataset `task_id` field (e.g. `public_swe_0`) |
| `num_questions` | Number of times agent invoked `AskUserQuestion` / `ask_human` tool / `requestUserInput` |
| `num_blockers_resolved` | Number of distinct blocker IDs the LLM judge matched to agent questions |
| `total_num_blockers` | Number of blockers in `blocker_registry.json` for this task |
| `num_llm_calls` | Best-effort observed LLM call count from harness events/token usage records |
| `num_tool_calls` | Observed tool-call count from normalized trajectory and native events |
| `num_turns_or_items` | Harness-native turn/item count; comparable as an observed diagnostic, not a hard budget |
| `input_tokens`, `output_tokens`, `total_tokens` | Best-effort token usage; `null` when the provider/harness does not expose usage |
| `llm_proxy_error_count`, `llm_proxy_status_counts`, `llm_proxy_stripped_params` | OpenCode/LiteLLM shim diagnostics when present; absent or empty for non-OpenCode harnesses |
