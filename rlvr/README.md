# HiL-Bench RLVR: training open-weight agents to ask for help

A self-contained recipe for the RLVR experiment in the HiL-Bench paper (§4.5, App. H): train an open-weight model with
reinforcement learning from a verifiable reward to **ask the right clarification questions**, on the SQL tasks only,
and evaluate on held-out SQL tasks and on SWE tasks (transfer). Everything runs on one Linux GPU machine with Docker;
no hosted LLM API is required. The judge behind `ask_human` is a model you serve yourself, or any LiteLLM proxy.

Defaults: **Qwen3.5-2B** policy, LoRA GRPO with [SkyRL](https://github.com/NovaSky-AI/SkyRL), tasks executed as
[Harbor](https://github.com/laude-institute/harbor) trials from the [HiL-Bench](https://github.com/hilbenchauthors/hil-bench) `harbor_sql/` and `harbor_swe/` release,
and the paper's judge `casperhansen/llama-3.3-70b-instruct-awq` served with vLLM.

```
SkyRL trainer (FSDP + LoRA)  ──weights──►  vLLM policy server
      ▲                                          │ chat completions
      │ token ids, logprobs, R                   ▼
HarborGenerator ──► Harbor trial: Terminus-2 agent ──► Docker compose task
                                                        ├─ main container: SQL / repo, task CLIs
                                                        ├─ tool sidecars (sql-tools, business-info)
                                                        └─ ask-human sidecar ──► judge (serve_judge.sh)
                    verifier: official checker, then overlay/rl_reward.py ──► reward.txt = R
```

## Reward

Computed inside the task container after the official verifier (`overlay/rl_reward.py`), from the ask-human
sidecar's question log:

```
R = 0.3 · (new blockers hit) − 0.1 · (irrelevant + duplicate questions) + (d / |B| if d ≥ 1) + λ · solve
```

- *new blocker hit*: the judge matched the question to a blocker not discovered before in this trajectory;
- *irrelevant / duplicate*: no match, or a blocker already discovered (including the sidecar's forced "irrelevant"
  answer once a blocker's answer cap is reached);
- *d*: distinct blockers discovered, *|B|*: blockers in the task; the coverage term is 0 when *d = 0*;
- *solve*: the official verifier's pass/fail; **λ = 1 by default**, `build_tasks.py --lambda-solve 0` gives the pure
  shaped Ask-F1 reward;
- judge failures (`can't answer ...`) get 0 and are counted in `rl_n_judge_fail`, so infrastructure errors never
  look like bad questions.

`reward.json` keeps the official fields (`solve`, Ask precision / recall / F1) next to the decomposition
(`rl_n_hits`, `rl_n_misses`, `rl_terminal`, ...).

## Requirements

- Linux x86_64, NVIDIA GPU(s) with 80 GB (H100 tested), Docker with compose v2 (your user can run `docker`),
  `git`, [`uv`](https://docs.astral.sh/uv/), `curl`; `dpkg-deb` if the NVIDIA driver is older than 580.
- GPUs: one for the policy (trainer and vLLM share it), plus room for the judge. The paper judge (70B AWQ) needs about
  40 GB of weights, so give it its own GPU. For a quick pipeline check a small judge can share the policy GPU
  (see [Smoke test](#smoke-test)); its matching quality is lower than the paper's, so do not report numbers from it.
- Disk: ~25 GB for the Python environment and models, plus Docker images (≈2 GB per SQL database, more per SWE repo;
  pulled on first use from the public registry referenced by the task compose files).

## 1. Install

```bash
bash rlvr/install.sh          # SkyRL at a pinned commit + locked deps (torch, vLLM, Harbor) into rlvr/.work
source rlvr/env.sh            # every later command assumes this
uv run --no-project --with pytest --with pyyaml pytest -q rlvr/tests   # unit tests (task-build tests need HIL_BENCH_DIR, see step 2)
```

`RLVR_WORK=/big/disk/rlvr bash rlvr/install.sh` puts the environment elsewhere (export the same `RLVR_WORK` before
`source rlvr/env.sh`). The pinned wheels are CUDA 13; with a driver older than 580 the installer unpacks NVIDIA's
CUDA 13 forward-compatibility libraries locally (no root) and `env.sh` puts them on `LD_LIBRARY_PATH`.

## 2. Build the tasks

The tasks come from a HiL-Bench checkout; `build_tasks.py` reads its `harbor_sql/` and `harbor_swe/` directories.

```bash
git clone https://github.com/hilbenchauthors/hil-bench.git /path/to/hil-bench
export HIL_BENCH_DIR=/path/to/hil-bench      # or pass --hil-bench /path/to/hil-bench
python rlvr/build_tasks.py --all-modes
# {"train": 70, "val": 90, "swe_eval": 90}  -> rlvr/tasks/{train,val,swe_eval}, rlvr/tasks/manifest.json
bash rlvr/pull_images.sh rlvr/tasks      # optional: pre-pull the task images (otherwise pulled on first use)
```

- SQL `ask_human` tasks are split 70/30 into `train` / `val`, stratified by database (seed 0); `swe_eval` is a
  30-task SWE subset stratified by repository. Only `train` is used for gradient updates.
- `--all-modes` also builds the `baseline` and `full_info` variants of the eval tasks (without it: `ask_human` only).
- Task images, data and official verifiers are unchanged. The builder renames the official `tests/test.sh` to
  `tests/test_official.sh`, adds the reward overlay and `rl_config.json`, appends the `ask_human` guidance (same wording
  as HiL-Bench's `configs/*/ask_config_*.yaml`) to `instruction.md`, and maps `host.docker.internal` to the host in the ask-human
  service so the sidecar can reach the judge.
- The SQL task images ship an `ask_human` command; the SWE repo images do not. For SWE `ask_human` tasks the builder
  mounts `overlay/ask_human_cli.py` at `/usr/local/bin/ask_human` in the main container. It calls the same ask-human
  sidecar over MCP, so a terminal agent can ask on SWE exactly as on SQL (all SWE images have `python3` ≥ 3.8).

## 3. Serve the judge

```bash
CUDA_VISIBLE_DEVICES=1 bash rlvr/serve_judge.sh        # casperhansen/llama-3.3-70b-instruct-awq on :8808
```

The server binds to the Docker bridge address. The sidecar uses its existing `ASK_HUMAN_BACKEND=vllm` route with
`VLLM_BASE_URL` / `VLLM_MODEL` from `env.sh`. To use a judge running elsewhere, export `VLLM_BASE_URL` (no `/v1`),
`VLLM_MODEL` and, if needed, `VLLM_API_KEY` before `source rlvr/env.sh`.

**Or use a LiteLLM proxy** (no judge GPU needed; the sidecar's `litellm_proxy` backend). Export the route before
sourcing `env.sh`, keeping the key in your shell or a mode-600 file rather than in the repo:

```bash
export ASK_HUMAN_BACKEND=litellm_proxy
export ASK_HUMAN_MODEL=litellm_proxy/<llama-3.3-70b-instruct model name on your proxy>
export LITELLM_BASE_URL=<proxy url> LITELLM_API_KEY=<key>      # optional: LITELLM_USER=<attribution user>
source rlvr/env.sh
```

`train.py` forwards these variables to the workers that start the trials and never prints the key
(`config_summary.json` records only the backend, model and whether a key is set).

Then check environments, judge and reward end to end, without a policy model:

```bash
python rlvr/check_env.py rlvr/tasks/train/hil-bench-sql_<id>-ask_human
python rlvr/check_env.py rlvr/tasks/swe_eval/hil-bench-swe_<id>-ask_human   # SWE: gold = the task's oracle patch
```

It runs scripted solutions through the real sidecar, judge and verifier, and checks the hand-computed rewards
(`silent` = λ, `spammer` = −0.3, `nop` = 0). It also reports how many of each blocker's example questions the judge
matched (`asker`, `dup`). `check_env: passed` is required before training.

## 4. Train

```bash
CUDA_VISIBLE_DEVICES=0 python rlvr/train.py --validate-only        # config + imports, prints the agent fingerprint
CUDA_VISIBLE_DEVICES=0 python rlvr/train.py --run-name qwen2b-sql  # trains on tasks/train, evals on tasks/val
```

Defaults (`python rlvr/train.py -h`): Qwen3.5-2B, LoRA rank 32, GRPO with group size 8 (`--n-samples`), 4 tasks per
step (`--batch`), lr 5e-5, 50 steps, eval before training and every 10 steps on `tasks/val`, adapters exported every
10 steps. The agent is Terminus-2 with up to 30 turns and a 32k context. Rollouts are step-wise (every agent turn is
a training sample with its own token ids and logprobs), and each turn gets the trajectory's GRPO advantage.
`--max-concurrency` caps concurrent Docker trials. Any `key=value` argument is passed through as a SkyRL config
override, e.g. `trainer.policy.optimizer_config.lr=1e-5`. `--logger wandb` logs to Weights & Biases if you are
logged in.

Outputs in `rlvr/runs/<run-name>/`:

- `metrics.jsonl`: all trainer metrics per step (`reward/avg_raw_reward`, `policy/grad_norm`,
  `eval/all/avg_score`, ...);
- `harbor-trials/`: every trial with the agent log and `verifier/reward.json`;
- `exports/global_step_<N>/policy`: LoRA adapters;
- `config_summary.json`: the resolved agent / judge / batch configuration.

A failed trial (environment error, context overflow) masks its GRPO group for that step instead of being scored 0.

The trainer and vLLM share the policy GPU and hand its memory back and forth every step (vLLM sleeps during the
update and wakes up for rollouts). The GPU must be dedicated to the run: if another process allocates memory in
between, the next wake-up fails with `CUDA Error: out of memory` in `wake_up`.

## 5. Evaluate

Serve the base model and the trained adapter, then evaluate both with the same agent used in training:

```bash
CUDA_VISIBLE_DEVICES=0 bash rlvr/serve_policy.sh Qwen/Qwen3.5-2B rlvr/runs/qwen2b-sql/exports/global_step_50/policy 8011 &
python rlvr/eval.py rlvr/tasks/val rlvr/tasks/swe_eval --model hosted_vllm/policy  -k 3 --out rlvr/runs/eval/base
python rlvr/eval.py rlvr/tasks/val rlvr/tasks/swe_eval --model hosted_vllm/trained -k 3 --out rlvr/runs/eval/trained
```

`summary.json` reports per mode: pass@1..k, Ask precision / recall / F1 (pooled over trials as in
HiL-Bench's `run_hil_bench.py`), mean questions per trial and mean RLVR reward. Environment errors (image pull or
container start timeouts) are excluded; agent failures (context overflow, agent timeout) count as unsolved
attempts in pass@k and are reported as `n_agent_failures`, since their question log is not verified. SQL `val` measures in-domain generalization to
unseen tasks; `swe_eval` measures transfer to a domain never trained on. `agent_fingerprint` in `summary.json`
matches the one printed by `train.py --validate-only`.

## Smoke test

A small end-to-end run that exercises every component on one GPU. With a LiteLLM judge (above) skip the
`serve_judge.sh` line; otherwise a small judge can share the policy GPU:

```bash
python rlvr/build_tasks.py --n-train 4 --n-val 2 --n-swe 1 --out rlvr/tasks_smoke
export VLLM_MODEL=Qwen/Qwen3.5-4B && source rlvr/env.sh       # self-hosted small judge (skip with LiteLLM)
CUDA_VISIBLE_DEVICES=0 RLVR_JUDGE_GPU_UTIL=0.2 bash rlvr/serve_judge.sh \
  --default-chat-template-kwargs '{"enable_thinking": false}' &
python rlvr/check_env.py rlvr/tasks_smoke/train/$(ls rlvr/tasks_smoke/train | head -1)
CUDA_VISIBLE_DEVICES=0 python rlvr/train.py --run-name smoke --train-dir rlvr/tasks_smoke/train \
  --val-dir rlvr/tasks_smoke/val --max-steps 2 --batch 2 --n-samples 4 --eval-batch 2 --eval-interval 1 \
  --hf-save-interval 2 --gpu-memory-utilization 0.4
CUDA_VISIBLE_DEVICES=0 bash rlvr/serve_policy.sh Qwen/Qwen3.5-2B rlvr/runs/smoke/exports/global_step_2/policy 8011 &
python rlvr/eval.py rlvr/tasks_smoke/val rlvr/tasks_smoke/swe_eval --model hosted_vllm/policy  -k 2 --out rlvr/runs/eval_smoke/base
python rlvr/eval.py rlvr/tasks_smoke/val rlvr/tasks_smoke/swe_eval --model hosted_vllm/trained -k 2 --out rlvr/runs/eval_smoke/trained
```

On one H100 with a LiteLLM judge this takes about 10 min for `install.sh`, 2 min for `check_env.py`, 45 min for
the two training steps (first image pulls included) and 20 min per eval. The smoke run only checks the
pipeline; two steps on four tasks do not change the policy measurably.

## Files

| file | role |
| --- | --- |
| `install.sh`, `env.sh` | environment (pinned SkyRL + locked deps), CUDA / judge variables |
| `build_tasks.py` | splits + RL overlay on top of a HiL-Bench checkout's `harbor_sql/` and `harbor_swe/` |
| `overlay/rl_reward.py`, `overlay/test.sh` | reward (runs in the task container after the official verifier) |
| `overlay/ask_human_{sql,swe}.md` | `ask_human` guidance appended to `instruction.md` |
| `overlay/ask_human_cli.py` | `ask_human` terminal command for SWE images (mounted by `build_tasks.py`) |
| `harbor_trial.yaml`, `harbor_common.py` | the single agent / environment / verifier template used by training and eval |
| `train.py` | SkyRL LoRA GRPO over Harbor trials |
| `patches/` | SkyRL source patches applied by `install.sh` (LoRA weight-sync tensor names for multimodal checkpoints) |
| `vllm_adapter.py` | renames exported LoRA tensors so vLLM applies them to multimodal checkpoints (used by `serve_policy.sh`) |
| `site/` | optional exact trainer patch: logits only for scored tokens (`--no-response-logits-only` disables it) |
| `serve_judge.sh`, `serve_policy.sh`, `eval.py`, `metrics.py` | judge, evaluation and metrics |
| `check_env.py` | environment + judge + reward check with scripted agents |
| `tests/` | unit tests |

## Differences from the paper's setup

- **Agent harness:** the policy acts through Harbor's Terminus-2 terminal agent, which SkyRL's Harbor integration
  trains on directly, rather than the SWE-agent harness used by HiL-Bench's `run_hil_bench.py`. Training and `eval.py` use the
  identical agent configuration, so base vs trained comparisons are like for like. Numbers are not directly
  comparable to leaderboard results produced with `run_hil_bench.py`.
- **Budgets:** 30 agent turns and a 32k context per trial (`harbor_trial.yaml`), to keep a 2B policy's rollouts on
  one GPU.
- **Solve bonus:** λ · solve is on by default (λ = 1); set `--lambda-solve 0` for the shaped Ask-F1 reward alone.
