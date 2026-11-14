#!/usr/bin/env bash
# Run the official HiL-Bench verifier unchanged, then write the RLVR reward (rl_reward.py).
bash /tests/test_official.sh
PY=$(command -v python3 || command -v python)
"$PY" /tests/rl_reward.py || echo "rl_reward.py failed; keeping official reward" >&2
exit 0
