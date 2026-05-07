# Trust Horizon — Run Output Schema

All run outputs are written under `trust_horizon/runs/`. Nothing in `runs/` is committed to git.

## Directory Layout

```
runs/
  <run_id>/                           e.g. "claude-code_ask_human_20260507_153000"
    <attempt_uid>/                    e.g. "69bc1094b455a91fa20fb868"
      <mode>/                         "ask_human" | "full_info"
        <agent>/                      "claude-code" | "codex" | "opencode" | "google-adk"
          <model>/                    e.g. "claude-sonnet-4-6" | "gpt-5.5"
            pass_1/
              trajectory.json
              patch.diff
              metrics.json
            pass_2/
              ...
            pass_3/
              ...
    summary_metrics.json              Aggregated after all passes complete
    pass_level_metrics.csv            One row per (attempt, mode, agent, model, pass)
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
For native `AskUserQuestion` events (claude-code), the step should also include:
```json
{
  "act": "AskUserQuestion: <question text>",
  "obs": "<answer from human router>",
  "nativeEventType": "claude.AskUserQuestion.canUseTool",
  "blockerResolved": "<blocker_id or null>"
}
```

### `patch.diff`
Plain text output of `git diff HEAD` from inside the Docker container at the end of the agent run.
Empty string if no changes were made.

### `metrics.json`
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
  "agent": "claude-code",
  "model": "claude-sonnet-4-6",
  "mode": "ask_human",
  "attempt_uid": "69bc1094b455a91fa20fb868",
  "pass_num": 1,
  "run_id": "claude-code_ask_human_20260507_153000",
  "duration_seconds": 312.4
}
```

### `summary_metrics.json`
Written after all passes for a run_id complete. Structure mirrors `run_hil_bench.py`'s `build_summary()`:
```json
{
  "metadata": {
    "run_id": "...",
    "num_passes": 3,
    "modes": ["ask_human"],
    "agents": ["claude-code"],
    "generated_at": "2026-05-07T22:30:00Z"
  },
  "SWE": {
    "ask_human": {
      "claude-code": {
        "claude-sonnet-4-6": {
          "num_included_attempts": 3,
          "pass_at_1": 0.33,
          "pass_at_3": 0.67,
          "ask_precision": 0.6,
          "ask_recall": 0.4,
          "ask_f1": 0.48,
          "avg_cost_per_pass": 0.38,
          "avg_steps_per_pass": 24.1,
          "avg_num_questions_per_pass": 1.8
        }
      }
    }
  }
}
```

## Metrics Formulas

All ask metrics use the **macro (average-of-ratios)** formula from the HiL-Bench paper
(matching `paper_pipeline.py`'s `run_paper_pipeline` comments):

```
precision_for_pass  = num_blockers_resolved / num_questions   (0 if num_questions == 0)
recall_for_pass     = num_blockers_resolved / total_num_blockers

ask_precision = mean(precision_for_pass)  across all valid (attempt × pass) pairs
ask_recall    = mean(recall_for_pass)     across all valid (attempt × pass) pairs
ask_f1        = mean(f1_for_pass)         where f1 = harmonic mean of per-pass precision/recall

pass@k: fraction of attempts where ANY of the first k passes resolved the task
```

A pass is **invalid** (excluded from metrics) if its trajectory contains:
- 3+ timeout observations
- 1+ "can't answer (perhaps transient hiccup)" observations
- "Environment died unexpectedly" as the last observation
- "Exit due to unknown error" in the last step's response

## Key Field Definitions

| Field | Description |
|-------|-------------|
| `attempt_uid` | The HF dataset `uid` field = the Docker image name suffix = CSV `attempt_id` |
| `instance_id` | The HF dataset `task_id` field (e.g. `public_swe_0`) |
| `num_questions` | Number of times agent invoked `AskUserQuestion` / `ask_human` tool / `requestUserInput` |
| `num_blockers_resolved` | Number of distinct blocker IDs the LLM judge matched to agent questions |
| `total_num_blockers` | Number of blockers in `blocker_registry.json` for this task |
