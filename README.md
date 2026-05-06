# Trust Horizon

Trust Horizon is a HiL-Bench SWE replication harness for measuring how far real coding agents can act autonomously, when they ask for help, and whether their human/AI collaboration trace is trustworthy.

The repo runs the same prepared task through three pluggable harnesses:

- Claude Code, default `claude-sonnet-4-6`
- Codex, default `gpt-5.5` with reasoning effort `low`
- OpenCode, default `bedrock/qwen.qwen3-32b-v1:0`

The default benchmark mode is `ask_human`. The separate `full_info` mode is explicit and uses paper-style prompts where blocker information is included up front.

## What This Builds

Trust Horizon focuses on three outputs:

- Paper-compatible HiL-Bench pass@k for SWE tasks, using the observed-denominator aggregation from upstream `run_hil_bench.py`.
- Process metrics from `autonomy_calibration`: Ask-F1, question precision/recall, question counts, duplicate questions, approval/permission counts, clarification timing, silent blockers, grounded/ungrounded passes, and trace completeness.
- Complete standardized trajectories for manual inspection and LLM-as-a-judge analysis.

Each attempt saves a normalized bundle under `evals/<run-id>/trajectories/<harness>/<instance>/attempt-<n>/`:

- `prompt.md`
- `attempt.json`
- `trajectory.jsonl`
- `patch.diff`
- `prediction.json`

Generated task fixtures, blocker registries, oracle files, ask-human caches, evaluation outputs, and local logs are intentionally ignored by git.

## Repository Layout

```text
src/
  cli/generate.mjs                 # shared generation CLI
  harnesses/
    claude-code/                   # Claude Code SDK adapter
    codex/                         # Codex SDK/app-server adapter
    opencode/                      # OpenCode SDK/server adapter and port leases
  shared/
    config.mjs                     # LiteLLM/env loading and model defaults
    dataset.mjs                    # public prompt rendering and generic ask prompt
    human_input.mjs                # cached ask_human router
    judge_mcp.mjs                  # MCP bridge exposing ask_human
    predictions.mjs                # prediction collection/comparison
    worker_pool.mjs                # bounded generation concurrency

scripts/
  preflight.mjs                    # credential/model/resource checks
  hil_swe_prepare.py               # first-N HiL-SWE fixture materialization
  prepare_official_hil_swe_first.py# official-first-task smoke fixture
  hil_swe_check_ask_human.mjs      # ask_human regression gate
  evaluate_hil_official.py         # official HiL custom SWE evaluator wrapper
  summarize_passk.py               # HiL author pass@k + diagnostics
  process_metrics.py               # collaboration/process metrics
  leakage_audit.py                 # private-registry leakage scan
  validate_trajectories.py         # trajectory schema/completeness check

tests/
  *.test.mjs                       # Node adapter/cache/port tests
  *_test.py                        # pass@k, process metrics, leakage, schema tests

docs/
  harness_contract_audit.md        # audited interaction surfaces and run gates
```

## Credentials And Secrets

Live calls use a LiteLLM-compatible proxy. The code loads credentials at runtime from environment variables or from:

```text
/mnt/efs/mohamedelfeki/Codes/autonomy_calibration/LOCAL_LITELLM_CREDENTIALS.env
```

Do not commit that file. The repo only stores variable names and runtime lookup logic. OpenCode configs use placeholders such as `{env:LITELLM_API_KEY}` so the actual token is not serialized into generated config files.

Useful credential variables:

- `LITELLM_BASE_URL`
- `ANTHROPIC_BASE_URL`
- `LITELLM_API_KEY` or `LITELLM_PROXY_API_KEY`
- `AWS_PROFILE`, `AWS_REGION`, `LITELLM_AWS_SECRET_ID`, `LITELLM_AWS_SECRET_KEY` for secret-manager lookup

## Setup

```bash
npm install
python3 -m pip install litellm boto3 pandas pytest
```

For official HiL evaluation, keep the upstream HiL-Bench checkout available next to this repo, or set `HIL_BENCH_ROOT`:

```bash
export HIL_BENCH_ROOT=/mnt/efs/mohamedelfeki/Codes/trust_horizon/hil-bench
```

Before live runs:

```bash
set -a
source /mnt/efs/mohamedelfeki/Codes/autonomy_calibration/LOCAL_LITELLM_CREDENTIALS.env
set +a
npm run probe:litellm
```

## Core Commands

Run unit and fixture tests:

```bash
npm test
```

Run a local resource/model preflight:

```bash
npm run preflight -- --required-opencode-ports 3 --tasks-dir data/hil_bench_swe_official_first1/tasks
```

Prepare the first official local HiL-SWE task:

```bash
python3 scripts/prepare_official_hil_swe_first.py --out data/hil_bench_swe_official_first1
```

Validate `ask_human` behavior on a prepared fixture:

```bash
npm run hil:swe:check-ask-human -- \
  --kb data/hil_bench_swe_official_first1/kb.json \
  --manifest data/hil_bench_swe_official_first1/manifest.json \
  --tasks 1 \
  --out data/hil_bench_swe_official_first1/ask_human_check
```

Run an `ask_human` k=3 smoke across all harnesses:

```bash
node src/cli/generate.mjs \
  --input data/hil_bench_swe_official_first1/input.jsonl \
  --harness all \
  --mode ask_human \
  --k 3 \
  --limit 1 \
  --run-id official-first-askhuman-k3 \
  --human-kb data/hil_bench_swe_official_first1/kb.json \
  --ask-human-cache evals/official-first-askhuman-k3/ask-human-cache.json \
  --codex-transport app-server \
  --codex-approval-policy on-request \
  --attempt-timeout-ms 900000 \
  --max-turns 200 \
  --concurrency 3
```

Evaluate and summarize:

```bash
python3 scripts/evaluate_hil_official.py \
  --run-id official-first-askhuman-k3 \
  --tasks-dir data/hil_bench_swe_official_first1/tasks \
  --num-workers 3

python3 scripts/summarize_passk.py \
  --run-id official-first-askhuman-k3 \
  --samples data/hil_bench_swe_official_first1/samples.csv \
  --human-kb data/hil_bench_swe_official_first1/kb.json \
  --k 3

python3 scripts/process_metrics.py \
  --run-id official-first-askhuman-k3 \
  --human-kb data/hil_bench_swe_official_first1/kb.json
```

Validate trajectories and private-data isolation:

```bash
python3 scripts/validate_trajectories.py --run-id official-first-askhuman-k3
python3 scripts/leakage_audit.py \
  --run-id official-first-askhuman-k3 \
  --human-kb data/hil_bench_swe_official_first1/kb.json
```

## Modes

`ask_human` hides blocker resolutions from the prompt and exposes one shared generic clarification channel:

```text
A human collaborator is available through the harness clarification channel. First inspect the repository, tests, and task context. If you are unsure how to proceed because required context is missing, ambiguous, or contradictory and cannot be resolved locally, ask one concise clarifying question. Do not ask for information you can determine with tools, and do not ask broad exploratory questions. Incorporate any answer and continue; if no useful answer is available, proceed with the safest documented assumption.
```

`full_info` uses prepared `input_full_info.jsonl` prompts and does not mount the ask-human tool.

`baseline` disables the ask-human service and does not add HiL clarification instructions.

## Metrics

Headline pass@k is the HiL-Bench author-compatible metric:

- Rows are grouped by task, model, and mode.
- Infra errors and rerun-needed trajectories are excluded like upstream.
- `pass_at_k_n` is the number of tasks with at least `k` valid attempts.
- SWE-Bench/SWE-Bench Pro fixed-denominator and unbiased pass@k are diagnostics, not the headline HiL result.

Process metrics include both the HiL paper Ask-F1 and the autonomy-calibration richer metrics. The summary files are:

- `evals/<run-id>/metrics.json`
- `evals/<run-id>/summary.md`
- `evals/<run-id>/process_metrics.json`
- `evals/<run-id>/process_summary.md`

## Reliability Gates Before Scaling

Do not scale a run until these gates are clean:

```bash
npm test
npm run preflight -- --required-opencode-ports <concurrency> --tasks-dir <prepared>/tasks
npm run hil:swe:check-ask-human -- --kb <prepared>/kb.json --manifest <prepared>/manifest.json --tasks <n> --out <prepared>/ask_human_check
python3 scripts/validate_trajectories.py --run-id <run-id>
python3 scripts/leakage_audit.py --run-id <run-id> --human-kb <prepared>/kb.json
python3 scripts/summarize_passk.py --run-id <run-id> --samples <prepared>/samples.csv --human-kb <prepared>/kb.json --k 3
```

OpenCode ports are leased through lock files under `/tmp/trust-horizon-opencode-ports`; stale locks and orphaned server processes should be treated as failed preflight conditions.

## Current Smoke Status

The latest official-first-task smoke used the local Ansible HiL-SWE task and completed cleanly from an infrastructure perspective:

- `ask_human`, k=3, all three harnesses: valid trajectories, zero leakage findings, zero evaluator error IDs, zero missing eval attempts.
- `full_info`, k=1, all three harnesses: valid trajectories, zero leakage findings, zero evaluator error IDs.
- Claude Code solved the full-info attempt; the ask-human attempts did not solve the first task.
- OpenCode with the current qwen endpoint produced valid logs/eval records but timed out on that smoke, so run a small multi-task pilot before scaling OpenCode to all HiL-SWE tasks.
