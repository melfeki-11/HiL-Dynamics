# HiL-Dynamics

HiL-Dynamics is a HiL-Bench diagnostic: it measures how `<model, harness>` pairs behave on under-specified coding tasks — when they ask for clarification, when they silently guess, and whether their questions actually resolve the blockers that block progress.

The tool runs a prepared benchmark task through pluggable harnesses, collects structured trajectories, and computes ask precision/recall/F1 alongside pass@k. The output shows how far each `<model, harness>` pair is from the *selective escalation* ideal: asking exactly when needed, with questions that resolve actual blockers.

**Agent Ask Behavior on Under-Specified Tasks**

<table border="1" cellpadding="28" cellspacing="0" width="100%">
<tr>
  <td align="center" valign="middle" width="4%"><em><font color="#888888">Asks<br>when<br>needed</font></em></td>
  <td align="center" bgcolor="#ede9f6" width="48%">
    <strong><font color="#6b3fa0">Over-Asks</font></strong><br><br>
    <em><font color="#888888">Noisy, slower than manual</font></em>
  </td>
  <td align="center" bgcolor="#d5eee9" width="48%">
    <strong><font color="#1d7a6e">Good Agent ✓</font></strong><br><br>
    <em><font color="#888888">Selective escalation</font></em>
  </td>
</tr>
<tr>
  <td align="center" valign="middle"><em><font color="#888888">Doesn't<br>ask</font></em></td>
  <td align="center" bgcolor="#fce4e1">
    <strong><font color="#8b1712">Confident Hallucination</font></strong><br><br>
    <strong><font color="#c0392b">The dangerous quadrant</font></strong>
  </td>
  <td align="center" bgcolor="#fef8e0">
    <strong><font color="#9a7d0a">Lucky Guess</font></strong><br><br>
    <em><font color="#888888">Fragile, not reliable</font></em>
  </td>
</tr>
<tr>
  <td></td>
  <td align="center"><em><font color="#888888">Fails</font></em></td>
  <td align="center"><em><font color="#888888">Succeeds</font></em></td>
</tr>
</table>

Supported harness configs:

| Harness | Default model | Default reasoning |
|---|---|---|
| `claude` | `claude-opus-4-7` | `xhigh` |
| `codex` | `gpt-5.5` | `xhigh` |
| `adk` | `gemini/gemini-3.1-pro-preview-customtools` | `high` |
| `opencode` | `fireworks_ai/glm-5p1` | `high` |
| `antigravity` | `gemini/gemini-3.1-pro-preview-customtools` | `high` |

## Repository Layout

```
bin/                  `hilbench` entry point (setup/run/analyze)
configs/              harness YAML configs
docker/               harness Dockerfiles
scripts/              ingest / build / run / eval / metrics orchestration
src/hil_swe/          SDK runners (claude / codex / adk / opencode / antigravity)
data/hil_bench_swe/   ingested task metadata + tasks_index
runs/                 run outputs (one folder per run-id, gitignored)
docs/                 schema + behavior reference
```

## What This Measures

Three outputs per run:

- **pass@k** — task resolution rate
- **Ask-F1** — F1 calculated from question precision and blocker recall
- **Trajectories** — complete `{thought, act, obs}` traces for manual inspection or LLM-as-a-judge analysis

Each attempt saves a normalized bundle under `runs/<run-id>/<uid>/<mode>/pass_<n>/`:
```
attempt.json      task metadata
trajectory.json   [{thought, act, obs}, ...]
stats.json        num_steps, num_questions, num_blockers_resolved, ...
patch.diff        agent's git diff
result.json       solve outcome
eval_result.json  test pass/fail
```

## Key Findings

See [analysis/Insights.md](analysis/Insights.md) for the full model-harness analysis: how different `<model, harness, skill>` configurations perform on selective escalation, how Ask Precision and Blocker Recall trade off, and what harness interventions move the needle.

## Prerequisites

- Docker (running)
- Node.js 20+
- Python 3.10+

```bash
npm install
pip install litellm boto3 pandas pytest tqdm pyyaml
```

## First-Time Setup

**1. Configure credentials**

```bash
cp .env.example .env
```

Open `.env` and fill in the required fields:

```bash
# LiteLLM proxy — recommended when running multiple harnesses
LITELLM_BASE_URL="https://<your-litellm-endpoint>"
LITELLM_API_KEY="sk-..."

# HuggingFace token — needed to pull task Docker base images
HF_TOKEN="hf_..."

# Judge model — required for ask_human-based arms (default and enhanced)
# Any instruction-tuned model your LiteLLM proxy serves works.
# Paper results used: "casperhansen/llama-3.3-70b-instruct-awq"
ASK_HUMAN_MODEL="<your-judge-model>"
```

If you have a direct API key rather than a LiteLLM proxy, you can omit `LITELLM_BASE_URL` and use provider-specific variables instead:

| Provider | Variables |
|---|---|
| Anthropic | `ANTHROPIC_AUTH_TOKEN=sk-ant-...` and `ANTHROPIC_BASE_URL=https://api.anthropic.com` |
| OpenAI | `OPENAI_API_KEY=sk-...` and `OPENAI_BASE_URL=https://api.openai.com/v1` |

The tool resolves credentials in this priority order: `LITELLM_API_KEY` → `ANTHROPIC_AUTH_TOKEN` → `OPENAI_API_KEY`. A LiteLLM proxy is recommended when running multiple harnesses (claude + codex + gemini) from a single endpoint.

**2. Ingest benchmark tasks**

```bash
# Example: ingest all 100 public set tasks at once
python3 scripts/ingest_hil_swe.py --all --p-set public

# Example: ingest a subset of the tasks by UID
python3 scripts/ingest_hil_swe.py --uids UID1 UID2 UID3
```

**3. Build Docker harness images**

```bash
# Example: build the claude-code harness for all 100 public set tasks with increased workers
python3 scripts/build_harness_images.py --sdk claude --p-set public --workers 8

# Example: build all harnesses for a subset of the tasks
python3 scripts/build_harness_images.py --sdk all --uids UID1 UID2 UID3
```

**4. Verify setup**

```bash
# Baseline check (deps, creds, tasks_index, runs/)
./bin/hilbench setup

# Optional: strict check adds a live ask_human judge probe
# (requires ASK_HUMAN_MODEL + working model credentials)
./bin/hilbench setup --strict
```

Example output when everything is ready:

```
  ✓ Python 3.11.4
  ✓ Node.js 20.17.0
  ✓ Docker running
  ✓ credential env found at .env
  ✓ LITELLM credentials present
  ✓ tasks_index.json found
  ✓ runs/ directory writable

All checks passed. Ready to run.
```

## Using HiL-Dynamics

### Getting Agent Results

```bash
# 1) default arm: ask_human mode only
./bin/hilbench run --harness claude --p-set public --arm default --passes 3

# 2) enhanced arm: ask_human + skill + guidance (+ custom tool where supported)
./bin/hilbench run --harness codex --p-set public --arm enhanced --passes 3

# 3) full_info arm
./bin/hilbench run --harness antigravity --uids UID1 UID2 UID3 --arm full_info --passes 3

# Optional: use a saved UID list file
./bin/hilbench run --harness claude --uid-file data/hil_swe_20_attempt_test_set_uids.txt --arm default
```

### Doing Agent Analyses

```bash
# Build report + metadata for one run
./bin/hilbench analyze --run-id <run-id>

# Inspect run-level summary (machine-readable)
python3 -m json.tool runs/<run-id>/metadata.json

# View aggregate metrics used by the report
python3 -m json.tool runs/<run-id>/metrics/summary.json
```

## Configuration Files

Harness configs live in `configs/harnesses/` and are the only configs required for normal usage:

```
configs/harnesses/
  claude.yaml
  codex.yaml
  adk.yaml
  opencode.yaml
  antigravity.yaml
```

**Harness config fields:**

| Field | Description |
|---|---|
| `sdk` | `claude`, `codex`, `adk`, `opencode`, or `antigravity` |
| `model` | Model slug as understood by your LiteLLM proxy |
| `reasoning_effort` | `low`, `medium`, `high`, `xhigh`, `max` |

## Clarification Routing

All harnesses route through the same sidecar backend (`src/hil_swe/ask_human_sidecar.mjs`), but their question surfaces differ:

| Harness | Native question surface | Extra custom tool option |
|---|---|---|
| Claude Code | `AskUserQuestion` | yes (`ask_human` custom tool) |
| Codex | `requestUserInput` | yes (`ask_human` custom tool) |
| ADK | native `ask_human` tool | no separate toggle |
| OpenCode | no native surface; uses `ask_human` tool path | no separate toggle |
| Antigravity | native ask + optional custom tool path | yes (`ask_human` custom tool) |

The blocker registry is never copied into the agent workspace; it is only mounted for the sidecar.

For full output schema and details, see [docs/run_output_schema.md](docs/run_output_schema.md).
For harness caveats and interpretation notes, see [docs/harness_asymmetries.md](docs/harness_asymmetries.md).
