# Escalation Lens: Diagnosing Ask Behavior Across Agent Harnesses

Escalation Lens is a HiL-Bench diagnostic: it measures how `<model, harness>` pairs behave on under-specified coding tasks — when they ask for clarification, when they silently guess, and whether their questions actually resolve the blockers that block progress.

The tool runs a prepared benchmark task through pluggable harnesses, collects structured trajectories, and computes ask precision/recall/F1 alongside pass@k. The output shows how far each `<agent, harness>` pair is from the *selective escalation* ideal: asking exactly when needed, with questions that resolve actual blockers.

Supported harnesses:

| Harness | Default model | Default reasoning |
|---|---|---|
| Claude Code | `claude-opus-4-7` | `xhigh` |
| Codex | `gpt-5.5` | `xhigh` |
| ADK | `gemini/gemini-3.1-pro-preview-customtools` | `high` |
| OpenCode | `fireworks_ai/glm-5p1` | `xhigh` |

The default benchmark mode is `ask_human`. The `full_info` mode is the paper-style baseline where blocker information is given upfront.

## What This Measures

Three outputs per run:

- **pass@k** — paper-compatible HiL-Bench task resolution rate
- **Ask-F1** — ask precision (questions that hit a real blocker), ask recall (blockers that get a question), and F1
- **Trajectories** — complete `{thought, act, obs}` traces for manual inspection or LLM-as-a-judge analysis

Each attempt saves a normalized bundle under `runs/<run-id>/<uid>/<mode>/pass_<n>/`:

```
attempt.json      harness metadata
trajectory.json   [{act, obs, thought}, ...]  (SWE-agent-compatible format)
stats.json        {num_steps, num_questions, num_blockers_resolved, ...}
patch.diff        agent's git diff
result.json       solve outcome
eval_result.json  test pass/fail
```

## Quickstart

```bash
# 1. Check your setup
./bin/hilbench setup --sdk claude --slice smoke

# 2. Run a 3-task smoke test
./bin/hilbench run --harness claude --slice smoke

# 3. Generate a report
./bin/hilbench analyze --run-id <run-id printed in step 2>
```

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

Open `.env` and fill in:

```bash
LITELLM_BASE_URL="https://<your-litellm-endpoint>"
LITELLM_API_KEY="sk-..."
HF_TOKEN="hf_..."
```

Optional overrides (defaults shown):

```bash
# CLAUDE_MODEL="claude-opus-4-7"
# ASK_HUMAN_BASE_URL="http://localhost:8000/v1"
# ASK_HUMAN_MODEL="casperhansen/llama-3.3-70b-instruct-awq"
```

**2. Ingest benchmark tasks**

```bash
python3 scripts/ingest_hil_swe.py --p-set public
```

**3. Build Docker harness images**

```bash
# Build images for the Claude harness, public partition (100 tasks)
python3 scripts/build_harness_images.py --sdk claude --p-set public --workers 6
```

**4. Verify setup**

```bash
./bin/hilbench setup --sdk claude --slice smoke
```

Expected output when everything is ready:

```
  ✓ Python 3.11.4
  ✓ Node.js 20.17.0
  ✓ Docker running
  ✓ .env found at .env
  ✓ LITELLM credentials present
  ✓ tasks_index.json found (100 tasks)
  ✓ runs/ directory writable
  ✓ Docker image hilbench-swe-harness-claude:69a7a4d1617c0b97d4d6aacd
  ...

All checks passed. Ready to run.
```

## Running a Benchmark

### Smoke test — 3 tasks, 1 pass (~30–60 min per harness)

```bash
./bin/hilbench run --harness claude --slice smoke
```

### Canonical 20-task test set — 3 passes

```bash
./bin/hilbench run --harness claude --slice test20
```

### Full public set — 100 tasks, 3 passes

```bash
./bin/hilbench run --harness claude --slice full_public
```

### Preview the command without running

```bash
./bin/hilbench run --harness claude --slice smoke --dry-run
```

## Configuration Files

Harness and slice configs live in `configs/`:

```
configs/
  harnesses/
    claude.yaml     sdk: claude, model: claude-opus-4-7, reasoning_effort: xhigh
    codex.yaml      sdk: codex,  model: gpt-5.5,         reasoning_effort: xhigh
    adk.yaml        sdk: adk,    model: gemini/...,       reasoning_effort: high
    opencode.yaml   sdk: opencode, model: fireworks_ai/..., reasoning_effort: xhigh
  slices/
    smoke.yaml       3 UIDs,  1 pass  (quick validation)
    test20.yaml      20 UIDs, 3 passes (canonical benchmark)
    full_public.yaml 100 UIDs, 3 passes (full public partition)
```

**Harness config fields:**

| Field | Description |
|---|---|
| `sdk` | `claude`, `codex`, `adk`, or `opencode` |
| `model` | Model slug as understood by your LiteLLM proxy |
| `reasoning_effort` | `low`, `medium`, `high`, `xhigh`, `max` |

**Slice config fields:**

| Field | Description |
|---|---|
| `uids` | List of task UIDs to run (mutually exclusive with `p_set`) |
| `p_set` | Partition name: `public`, `private`, or `both` |
| `modes` | `ask_human` (default), `full_info`, or both |
| `passes` | Number of passes per task (for pass@k) |
| `workers` | Max concurrent Docker containers |

## Generating a Report

```bash
./bin/hilbench analyze --run-id <run-id>
```

Writes two files to `runs/<run-id>/`:

- **`report.md`** — human-readable scorecard (pass@k, ask-P/R/F1, failure examples)
- **`metadata.json`** — machine-readable summary (sdk, model, num_uids, metrics path)

## Ask-Human Judge Server

The `ask_human` mode routes the agent's clarification questions through an LLM judge that matches them against the task's blocker registry. Set the judge endpoint in `.env`:

```bash
ASK_HUMAN_BASE_URL="http://localhost:8000/v1"
ASK_HUMAN_MODEL="casperhansen/llama-3.3-70b-instruct-awq"
```

Any LiteLLM-compatible endpoint works. For local inference, vLLM with `llama-3.3-70b-instruct-awq` on 4 GPUs is the reference configuration.

## Output Structure

```
runs/<run-id>/
  <uid>/
    ask_human/
      pass_1/
        attempt.json      harness metadata
        trajectory.json   [{act, obs, thought}, ...]
        stats.json        {num_steps, num_questions, num_blockers_resolved, ...}
        patch.diff        agent's git diff
        result.json       solve outcome
        eval_result.json  test pass/fail (Phase 2)
      pass_2/  pass_3/
    full_info/           (if --modes ask_human full_info)
  metrics/
    pass_level.json      per-(uid, mode, pass) rows
    summary.json         aggregated pass@k + ask-F1 metrics
  report.md              generated by hilbench analyze
  metadata.json          generated by hilbench analyze
```

For the full schema, see [docs/run_output_schema.md](docs/run_output_schema.md).

## Repository Layout

```
bin/                  hilbench entry point
configs/              harness and slice YAML configs
docker/               harness Dockerfiles
scripts/              ingest / build / run / eval / metrics orchestrators
src/hil_swe/          SDK runners (claude / codex / adk / opencode)
data/hil_bench_swe/   ingested task metadata and index
runs/                 run outputs (one folder per run-id, gitignored)
docs/                 schema reference and evaluation reports
```

## Advanced: Direct Script Usage

The `bin/hilbench` wrappers call `scripts/run_hil_swe.py` directly. You can call it yourself for finer control:

```bash
# Single task, ask_human mode, 1 pass
python3 scripts/run_hil_swe.py \
  --run-id my-first-run \
  --uids 69bc1094b455a91fa20fb868 \
  --modes ask_human \
  --passes 1

# 20-task test set, 3 passes, 5 concurrent containers
python3 scripts/run_hil_swe.py \
  --run-id my-run \
  --uids \
    69bc1094b455a91fa20fb868 \
    698139c7dc5e90df07566a6c \
    69a9e77602049c14d2793bb5 \
    69c60cc7b6a31e9900faa779 \
    69c6ac9f46a2e65fc3988794 \
    69bcc6360c872b9773cce01d \
    69be1b17ed0dad79557a9d20 \
    69a7a4d1617c0b97d4d6aacd \
    69c0073e28d67846c637cb7e \
    69c3c5e0b961752c24493b50 \
    69c3f3301734592b5a14a3b9 \
    69c0ead7ef94e54e9dc6a130 \
    69be580e4bde28908b05c56f \
    69c6079bcb74caaa66c49c87 \
    69b20af8600119b97e678c5b \
    69c3277deb9e9972372b30fc \
    69c2af94ae34531293e5f7ec \
    69b3ab1df8d713deb4c0087d \
    69c196fa0b42d9b078f32b2e \
    69b1031f73a8f5979167a774 \
  --modes ask_human \
  --passes 3 \
  --workers 5 \
  --reasoning-effort xhigh

# Solve only (skip eval and metrics)
python3 scripts/run_hil_swe.py --run-id pilot --uids ... --skip-eval --skip-metrics

# All 100 public tasks
python3 scripts/run_hil_swe.py --run-id pub100 --p-set public --modes ask_human --passes 3
```

## Interpreting Results

| Metric | Meaning |
|---|---|
| **pass@1** | Fraction of tasks resolved on the first attempt |
| **pass@k** | Fraction of tasks resolved in at least one of k attempts |
| **gated pass@k** | pass@k restricted to attempts where all expected passes are present |
| **Ask Precision** | Fraction of questions that hit a real blocker |
| **Ask Recall** | Fraction of blockers that received a relevant question |
| **Ask F1** | Harmonic mean of precision and recall |
| **Avg questions/pass** | Mean clarification questions per attempt |

A *selective escalation ideal* scores Ask-F1 = 1.0 with the minimum questions needed.

**Failure modes to watch for:**

- **Silent guessing** — low recall; agent never asks, guesses based on incomplete spec
- **Over-asking** — low precision; agent asks about things that are not actual blockers
- **Vague questions** — recall appears low even though questions are asked; judge cannot match to a blocker
- **Ask-capped** — questions suppressed by the per-pass ask limit (visible in `stats.json`)
