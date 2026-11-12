#!/usr/bin/env python3
"""End-to-end check of environment + judge + reward, without a policy model.

Runs Harbor's oracle agent on scripted variants of one built ask_human task (SQL or SWE). Each variant
replaces solution/solve.sh, so the real ask-human sidecar, judge, official verifier and
rl_reward.py all run exactly as in training:
  silent   submits the gold answer, asks nothing         expect R = lambda
  spammer  asks 3 off-topic questions, submits nothing   expect R = -0.3
  dup      asks the same blocker question 3 times        expect 1 hit + 2 misses (if the judge matches it)
  asker    asks each blocker's example question, then submits the gold answer
           expect hits = |B| and R = 0.3|B| + 1 + lambda (each hit depends on the judge)
nop on the plain task must score 0. Exit code 0 only if every hard expectation holds
(silent, spammer, nop, no judge failures); asker/dup hit counts are reported as judge quality.

  python check_env.py tasks/train/hil-bench-sql_20-ask_human
  python check_env.py tasks/swe_eval/hil-bench-swe_0-ask_human
The gold answer is the gold SQL query (SQL) or the task's own oracle patch script (SWE).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harbor_common as hc  # noqa: E402

OFFTOPIC = ["What is your favorite programming language?", "How is the weather today?",
            "Which database engine is the fastest in general?"]
GOLD = "cp /solution/ground_truth.sql /harbor_shared/submitted_query.sql"


def ask(q: str) -> str:
    return f"ask_human {shlex.quote(q)}; echo"


def variants(task: Path, out: Path) -> dict[str, tuple[Path, int]]:
    swe = (task / "tests" / "test_patch.diff").exists()
    gold = "bash /solution/gold.sh" if swe else GOLD
    reg = json.loads((task / "environment" / "ask-human-data" / "blocker_registry.json").read_text())
    blockers = reg.get("blockers", reg if isinstance(reg, list) else [])
    first = [b["example_questions"][0].lstrip("- ").strip() for b in blockers]
    plan = {"silent": [gold], "spammer": [ask(q) for q in OFFTOPIC], "dup": [ask(first[0])] * 3,
            "asker": [ask(q) for q in first] + [gold]}
    made = {}
    for name, lines in plan.items():
        d = out / name / task.name
        shutil.copytree(task, d, symlinks=True)
        if swe:
            shutil.copy2(d / "solution" / "solve.sh", d / "solution" / "gold.sh")
        (d / "solution" / "solve.sh").write_text("#!/usr/bin/env bash\nset -uo pipefail\n" + "\n".join(lines) + "\n")
        made[name] = (d, len(blockers))
    return made


def judge_reachable() -> str | None:
    """List the judge's models (vllm backend). For litellm_proxy only checks the route is configured."""
    import os
    import subprocess

    if os.environ.get("ASK_HUMAN_BACKEND") == "litellm_proxy":
        missing = [k for k in ("ASK_HUMAN_MODEL", "LITELLM_BASE_URL", "LITELLM_API_KEY") if not os.environ.get(k)]
        if missing:
            print(f"litellm_proxy judge: missing {', '.join(missing)}", file=sys.stderr)
            return None
        return os.environ["ASK_HUMAN_MODEL"]
    url = os.environ.get("VLLM_BASE_URL", "")
    if "host.docker.internal" in url:  # resolve like the containers do (Docker bridge gateway)
        ip = subprocess.run("ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1", shell=True,
                            capture_output=True, text=True).stdout.strip()
        url = url.replace("host.docker.internal", ip or "127.0.0.1")
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/v1/models", timeout=10) as r:
            return ",".join(m["id"] for m in json.loads(r.read())["data"])
    except Exception as exc:
        print(f"judge not reachable at {url}: {exc}", file=sys.stderr)
        return None


async def run(configs):
    return await asyncio.gather(*(hc.run_trial(c) for c in configs))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", help="a built ask_human task dir, SQL or SWE (build_tasks.py output)")
    ap.add_argument("--out", default=None, help="trials dir (default: a temp dir)")
    a = ap.parse_args()
    task = Path(a.task).resolve()
    lam = json.loads((task / "tests" / "rl_config.json").read_text())["lambda_solve"]
    models = judge_reachable()
    if not models:
        raise SystemExit(1)
    print(json.dumps({"judge_models": models}))
    work = Path(a.out or tempfile.mkdtemp(prefix="rlvr_check_"))
    made = variants(task, work / "tasks")
    template = hc.load_template()
    template["trials_dir"] = str(work / "trials")
    names = list(made) + ["nop"]
    configs = [hc.build_trial_config(template, made[n][0], model_name=None, api_base=None, agent_name="oracle") for n in made]
    configs.append(hc.build_trial_config(template, task, model_name=None, api_base=None, agent_name="nop"))
    for n, c in zip(names, configs):
        c["trial_name"] = f"check-{n}-{task.name}"[:120]
    results = asyncio.run(run(configs))
    ok, report = True, {}
    for n, r in zip(names, results):
        rj = {}
        if r.get("trial_dir"):
            f = Path(r["trial_dir"]) / "verifier" / "reward.json"
            rj = json.loads(f.read_text()) if f.exists() else {}
        row = {"reward": r.get("reward"), "solve": rj.get("solve"), "hits": rj.get("rl_n_hits"),
               "misses": rj.get("rl_n_misses"), "judge_fail": rj.get("rl_n_judge_fail"),
               "n_blockers": rj.get("rl_n_blockers"), "exception": r.get("exception_type")}
        if n == "silent":
            row["pass"] = row["reward"] is not None and abs(row["reward"] - lam) < 1e-6
        elif n == "spammer":
            row["pass"] = row["reward"] is not None and abs(row["reward"] + 0.3) < 1e-6
        elif n == "nop":
            row["pass"] = row["reward"] == 0.0
        else:
            nb = made[n][1]
            row["judge_quality"] = f"{row['hits']}/{1 if n == 'dup' else nb} expected hits"
            row["pass"] = row["reward"] is not None and not row["exception"]
        if row.get("judge_fail"):
            row["pass"] = False
        ok &= bool(row["pass"])
        report[n] = row
        print(json.dumps({n: row}))
    print(json.dumps({"check_env": "passed" if ok else "failed", "trials_dir": str(work / "trials")}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
