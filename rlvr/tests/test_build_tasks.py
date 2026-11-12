import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
import build_tasks as bt  # noqa: E402

# These tests read a HiL-Bench checkout (github.com/hilbenchauthors/hil-bench); set HIL_BENCH_DIR to run them.
_HB = os.environ.get("HIL_BENCH_DIR")
pytestmark = pytest.mark.skipif(not _HB or not (Path(_HB) / "harbor_sql").is_dir(),
                                reason="HIL_BENCH_DIR (a hil-bench checkout) not set")
if _HB:
    bt.REPO = Path(_HB).resolve()


def test_split_is_disjoint_and_stratified():
    s = bt.split(seed=0)
    train, val = {i for _, i in s["train"]}, {i for _, i in s["val"]}
    assert train and val and not train & val
    assert abs(len(val) / (len(train) + len(val)) - 0.3) < 0.05
    assert 25 <= len(s["swe_eval"]) <= 35
    assert s == bt.split(seed=0)  # deterministic


def test_build_one_installs_overlay(tmp_path):
    kind, i = bt.split()["train"][0]
    d = bt.build_one(kind, i, "ask_human", tmp_path, 1.0)
    t = d / "tests"
    assert (t / "test_official.sh").is_file() and (t / "rl_reward.py").is_file()
    assert "rl_reward.py" in (t / "test.sh").read_text()
    assert json.loads((t / "rl_config.json").read_text()) == {"lambda_solve": 1.0}
    assert "ask_human" in (d / "instruction.md").read_text().split("# Asking the human expert")[-1]
    svc = yaml.safe_load((d / "environment" / "docker-compose.yaml").read_text())["services"]["ask-human"]
    assert bt.JUDGE_HOST in svc["extra_hosts"]
    assert any("ASK_HUMAN_BACKEND" in e for e in svc["environment"])  # judge route still env-driven


def test_swe_ask_human_gets_cli_mount(tmp_path):
    kind, i = bt.split()["swe_eval"][0]
    d = bt.build_one(kind, i, "ask_human", tmp_path, 1.0)
    cli = d / "environment" / "rl_tools" / "ask_human"
    assert cli.is_file() and cli.stat().st_mode & 0o111
    vols = yaml.safe_load((d / "environment" / "docker-compose.yaml").read_text())["services"]["main"]["volumes"]
    assert "${CONTEXT_DIR:-.}/rl_tools/ask_human:/usr/local/bin/ask_human:ro" in vols
    b = bt.build_one(kind, i, "baseline", tmp_path, 1.0)  # no asking in baseline mode
    assert not (b / "environment" / "rl_tools").exists()
