#!/usr/bin/env python3
"""Build RLVR task directories from a HiL-Bench Harbor release (harbor_sql/, harbor_swe/).

The release is a checkout of https://github.com/hilbenchauthors/hil-bench, given by --hil-bench or $HIL_BENCH_DIR.

Images, data and official verifiers are untouched. Per task:
  - copy the task dir; the official tests/test.sh becomes tests/test_official.sh
  - install overlay/test.sh (official verifier, then overlay/rl_reward.py -> reward.txt = R)
  - write tests/rl_config.json {"lambda_solve": ...}
  - ask_human mode: append the ask_human guidance (same wording as configs/*/ask_config_*.yaml)
  - ask-human service: add host.docker.internal -> host gateway, so the sidecar reaches a judge
    served on the host (serve_judge.sh)

Splits (seeded, stratified): SQL ask_human tasks 70/30 into train/val by database; SWE eval
subset stratified by repo. --n-train/--n-val/--n-swe take the first N of each (for smoke runs).

  export HIL_BENCH_DIR=/path/to/hil-bench               # or pass --hil-bench
  python build_tasks.py                                  # full split -> tasks/{train,val,swe_eval}
  python build_tasks.py --n-train 4 --n-val 2 --n-swe 1 --out tasks_smoke
  python build_tasks.py --all-modes                      # val/swe_eval also in baseline + full_info
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import tomllib
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # replaced by --hil-bench / $HIL_BENCH_DIR in main()
OVL = HERE / "overlay"
MODES = ("baseline", "full_info", "ask_human")
# The host IP is interpolated by docker compose at trial time (env.sh sets RLVR_DOCKER_HOST_IP from docker0);
# the "host-gateway" fallback needs a Docker daemon that can resolve it (some 28.x daemons cannot).
JUDGE_HOST = "host.docker.internal:${RLVR_DOCKER_HOST_IP:-host-gateway}"


def src(kind: str, i: int, mode: str) -> Path:
    return REPO / f"harbor_{kind}" / f"hil-bench-{kind}_{i}-{mode}"


def _ids(kind: str, key: str) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for t in sorted((REPO / f"harbor_{kind}").glob("*-ask_human")):
        i = int(t.name.split(f"{kind}_")[1].split("-")[0])
        groups[tomllib.loads((t / "task.toml").read_text())["metadata"][key]].append(i)
    return groups


def _stratified(groups: dict[str, list[int]], n: int, rng: random.Random) -> list[int]:
    total = sum(len(v) for v in groups.values())
    out: list[int] = []
    for _, ids in sorted(groups.items()):
        ids = sorted(ids)
        rng.shuffle(ids)
        out += ids[: round(len(ids) * n / total)]
    return out


def split(seed: int = 0, val_frac: float = 0.3, n_swe: int = 30) -> dict[str, list[tuple[str, int]]]:
    rng = random.Random(seed)
    sql = _ids("sql", "database_name")
    n_sql = sum(len(v) for v in sql.values())
    val = sorted(_stratified(sql, round(n_sql * val_frac), rng))
    train = sorted(i for ids in sql.values() for i in ids if i not in val)
    swe = sorted(_stratified(_ids("swe", "repo_name"), n_swe, rng))
    assert not set(train) & set(val)
    return {"train": [("sql", i) for i in train], "val": [("sql", i) for i in val],
            "swe_eval": [("swe", i) for i in swe]}


def add_judge_host(compose: Path) -> bool:
    """Let the ask-human sidecar resolve host.docker.internal (Linux Docker needs host-gateway)."""
    import yaml

    doc = yaml.safe_load(compose.read_text())
    svc = (doc.get("services") or {}).get("ask-human")
    if svc is None:
        return False
    hosts = svc.setdefault("extra_hosts", [])
    if JUDGE_HOST not in hosts:
        hosts.append(JUDGE_HOST)
    compose.write_text(yaml.safe_dump(doc, sort_keys=False, width=1000))
    return True


def mount_ask_human_cli(env_dir: Path) -> None:
    """SWE repo images have no `ask_human` command (the SQL images ship one): mount overlay/ask_human_cli.py
    at /usr/local/bin/ask_human in the main container. It calls the same ask-human sidecar over MCP."""
    import yaml

    tools = env_dir / "rl_tools"
    tools.mkdir(exist_ok=True)
    shutil.copy2(OVL / "ask_human_cli.py", tools / "ask_human")
    (tools / "ask_human").chmod(0o755)
    compose = env_dir / "docker-compose.yaml"
    doc = yaml.safe_load(compose.read_text())
    vols = doc["services"]["main"].setdefault("volumes", [])
    mount = "${CONTEXT_DIR:-.}/rl_tools/ask_human:/usr/local/bin/ask_human:ro"
    if mount not in vols:
        vols.append(mount)
    compose.write_text(yaml.safe_dump(doc, sort_keys=False, width=1000))


def build_one(kind: str, i: int, mode: str, out_dir: Path, lam: float) -> Path:
    s = src(kind, i, mode)
    d = out_dir / s.name
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(s, d, symlinks=True)
    tests = d / "tests"
    (tests / "test.sh").rename(tests / "test_official.sh")
    shutil.copy2(OVL / "test.sh", tests / "test.sh")
    shutil.copy2(OVL / "rl_reward.py", tests / "rl_reward.py")
    (tests / "rl_config.json").write_text(json.dumps({"lambda_solve": lam}))
    if mode == "ask_human":
        instr = d / "instruction.md"
        instr.write_text(instr.read_text().rstrip("\n") + "\n" + (OVL / f"ask_human_{kind}.md").read_text())
    compose = d / "environment" / "docker-compose.yaml"
    if compose.exists():
        add_judge_host(compose)
    if kind == "swe" and mode == "ask_human":
        mount_ask_human_cli(d / "environment")
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hil-bench", default=os.environ.get("HIL_BENCH_DIR"),
                    help="checkout of github.com/hilbenchauthors/hil-bench (has harbor_sql/, harbor_swe/)")
    ap.add_argument("--out", default=str(HERE / "tasks"))
    ap.add_argument("--lambda-solve", type=float, default=1.0, help="solve-bonus weight (0 = pure shaped Ask-F1 reward)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--val-frac", type=float, default=0.3)
    ap.add_argument("--swe-eval", type=int, default=30, help="size of the SWE eval subset")
    ap.add_argument("--n-train", type=int, default=None)
    ap.add_argument("--n-val", type=int, default=None)
    ap.add_argument("--n-swe", type=int, default=None)
    ap.add_argument("--all-modes", action="store_true", help="also build baseline/full_info for val and swe_eval")
    a = ap.parse_args()
    global REPO
    if not a.hil_bench or not (Path(a.hil_bench) / "harbor_sql").is_dir():
        ap.error("--hil-bench / $HIL_BENCH_DIR must point to a hil-bench checkout containing harbor_sql/ and harbor_swe/")
    REPO = Path(a.hil_bench).resolve()
    sets = split(a.seed, a.val_frac, a.swe_eval)
    for name, n in (("train", a.n_train), ("val", a.n_val), ("swe_eval", a.n_swe)):
        if n is not None:
            sets[name] = sets[name][:n]
    out = Path(a.out)
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    manifest = {"seed": a.seed, "lambda_solve": a.lambda_solve, "repo_commit": commit or None, "sets": {}}
    for name, items in sets.items():
        modes = MODES if (a.all_modes and name != "train") else ("ask_human",)
        built = [build_one(kind, i, m, out / name, a.lambda_solve).name for kind, i in items for m in modes]
        manifest["sets"][name] = {"ids": [f"{k}_{i}" for k, i in items], "tasks": built,
                                  "sha1": hashlib.sha1(",".join(built).encode()).hexdigest()}
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: len(v["tasks"]) for k, v in manifest["sets"].items()}))


if __name__ == "__main__":
    main()
