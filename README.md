# Trust Horizon

Trust Horizon is a HiL-Bench SWE replication harness for measuring how far real coding agents can act autonomously, when they ask for help, and whether their human/AI collaboration trace is trustworthy.

The repo runs the same prepared task through three pluggable harnesses:

- Claude Code: `claude-opus-4-7` with reasoning effort `xhigh`
- Codex, `gpt-5.5` with reasoning effort `xhigh`
- ADK, `gemini/gemini-3.1-pro-preview-customtools` with reasoning effort `high`
- OpenCode, `fireworks_ai/glm-5p1` with reasoning effort `high`

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
docker/                            # harness Dockerfiles
scripts/                           # ingest/build/run/eval/metrics orchestrators
src/hil_swe/                       # SDK runners (claude/codex/adk/opencode)
data/hil_bench_swe/                # ingested task metadata/index
runs/                              # run outputs (one folder per run-id)
models/research_evals/hil_bench/   # evaluator + pipeline utilities
```

## Setup

```bash
npm install
python3 -m pip install litellm boto3 pandas pytest
```

Create a local `.env` file at repo root with these keys:

- `HF_TOKEN`
- `LITELLM_API_KEY`
- `LITELLM_BASE_URL`
- `CLAUDE_MODEL`
- `CODEX_MODEL`
- `ADK_MODEL`
- `OPENCODE_MODEL`
- `ASK_HUMAN_BASE_URL`
- `ASK_HUMAN_MODEL`

Create the run output directory at repo root:

```bash
mkdir -p runs
```

## Image Setup

We need one Docker image per `(attempt, SDK)` to support concurrent runs across SDK frameworks.

Ingest the public 100 tasks from Hugging Face:

```bash
python3 scripts/ingest_hil_swe.py --all
```

Build harness images (example: Claude + public set):

```bash
python3 scripts/build_harness_images.py --sdk claude --p-set public --workers 6
```

## To Run on Test Set

Start the ask-human server:

```bash
/mnt/efs/tutrinh/src/models/research_evals/hil_bench/.venv-vllm/bin/python \
  /mnt/efs/tutrinh/src/models/research_evals/hil_bench/.venv-vllm/bin/vllm serve \
  casperhansen/llama-3.3-70b-instruct-awq \
  --host 0.0.0.0 \
  --port 8808 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.7 \
  --tensor-parallel-size 4 \
  --pipeline-parallel-size 1 \
  --enforce-eager
```

Use these attempt IDs for quick test-set iterations. **Don't forget to set a unique run-id folder and use xhigh or high reasoning (depending on the model). 

```bash
python3 scripts/run_hil_swe.py \
  --run-id claude_swe_skill2 \
  --sdk claude \
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
```
