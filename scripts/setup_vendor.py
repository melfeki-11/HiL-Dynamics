#!/usr/bin/env python3
"""Install/update the official SWE-bench Pro evaluator checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "SWE-bench_Pro-os"
REPO = os.getenv("SWEBENCH_PRO_REPO", "https://github.com/scaleapi/SWE-bench_Pro-os.git")


def default_branch(cwd: Path) -> str:
    try:
        subprocess.check_call(["git", "remote", "set-head", "origin", "-a"], cwd=cwd, stdout=subprocess.DEVNULL)
        head = subprocess.check_output(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cwd, text=True).strip()
        return head.split("/", 1)[1]
    except Exception:
        return "main"


def main() -> None:
    VENDOR.parent.mkdir(parents=True, exist_ok=True)
    if (VENDOR / ".git").exists():
        subprocess.check_call(["git", "remote", "set-url", "origin", REPO], cwd=VENDOR)
        subprocess.check_call(["git", "fetch", "--depth", "1", "origin"], cwd=VENDOR)
        branch = default_branch(VENDOR)
        subprocess.check_call(["git", "checkout", branch], cwd=VENDOR)
        subprocess.check_call(["git", "pull", "--ff-only", "origin", branch], cwd=VENDOR)
    else:
        if VENDOR.exists() and any(VENDOR.iterdir()):
            raise SystemExit(f"{VENDOR} exists but is not a git checkout. Remove or move it before running setup-vendor.")
        subprocess.check_call(["git", "clone", "--depth", "1", REPO, str(VENDOR)])
    print(VENDOR)


if __name__ == "__main__":
    main()
