"""Paper reward (App. H): R = 0.3*new hits - 0.1*(irrelevant + duplicate) + (d/|B| if d >= 1) + lambda*solve."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("rl_reward", Path(__file__).parents[1] / "overlay" / "rl_reward.py")
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)

CANT = "can't answer (perhaps transient hiccup)"


def q(name, resp="res"):
    return {"question": "q", "response": resp if name else "irrelevant question", "blocker_name": name}


def test_silent_agent_gets_only_solve_bonus():
    assert rl.shaped_reward({"questions": [], "n_blockers": 4}, 1, 1.0)["reward"] == 1.0
    assert rl.shaped_reward(None, 0, 1.0)["reward"] == 0.0


def test_asker_hits_all_blockers():
    r = rl.shaped_reward({"questions": [q("a"), q("b"), q("c"), q("d")], "n_blockers": 4}, 1, 1.0)
    assert abs(r["reward"] - (4 * 0.3 + 1.0 + 1.0)) < 1e-9


def test_lambda0_and_terminal_gate():
    m = {"questions": [q(None), q(None)], "n_blockers": 3}  # no hit -> terminal stays 0
    assert abs(rl.shaped_reward(m, 1, 0.0)["reward"] - (-0.2)) < 1e-9


def test_duplicate_and_cap_count_as_miss():
    # 2nd hit on 'a' is a duplicate; the 3rd is the sidecar's forced irrelevant answer (None)
    r = rl.shaped_reward({"questions": [q("a"), q("a"), q(None)], "n_blockers": 2}, 0, 1.0)
    assert abs(r["reward"] - (0.3 - 0.1 - 0.1 + 0.5)) < 1e-9
    assert r["rl_n_hits"] == 1 and r["rl_n_misses"] == 2


def test_partial_coverage_terminal():
    r = rl.shaped_reward({"questions": [q("a"), q("b")], "n_blockers": 4}, 0, 1.0)
    assert abs(r["reward"] - (0.6 + 0.5)) < 1e-9


def test_judge_failure_is_neutral_and_flagged():
    m = {"questions": [{"question": "q", "response": CANT, "blocker_name": None}, q("a")], "n_blockers": 2}
    r = rl.shaped_reward(m, 0, 1.0)
    assert r["rl_n_judge_fail"] == 1 and abs(r["reward"] - (0.3 + 0.5)) < 1e-9


def test_outputs_are_flat_numbers():
    out = rl.shaped_reward({"n_blockers": 2, "questions": [q("a")]}, 1.0, 1.0)
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in out.values())


def test_main_writes_files(tmp_path):
    v, s, t = tmp_path / "v", tmp_path / "s", tmp_path / "t"
    for p in (v, s, t):
        p.mkdir()
    (v / "reward.json").write_text(json.dumps({"reward": 1, "solve": 1}))
    (s / "ask_human_metrics.json").write_text(json.dumps({"questions": [q("a")], "n_blockers": 2, "total_questions": 1,
                                                          "blockers_resolved": 1, "precision": 1.0, "recall": 0.5,
                                                          "f1": 0.667, "blocker_names": ["a", "b"]}))
    (t / "rl_config.json").write_text(json.dumps({"lambda_solve": 1.0}))
    rl.main(str(v), str(s), str(t))
    assert abs(float((v / "reward.txt").read_text()) - 1.8) < 1e-9
    out = json.loads((v / "reward.json").read_text())
    assert out["solve"] == 1 and out["official_reward"] == 1 and out["recall"] == 0.5
    assert all(isinstance(x, (int, float)) for x in out.values())  # Harbor VerifierResult contract


def test_missing_metric_fields_are_dropped(tmp_path):
    v, s, t = tmp_path / "v", tmp_path / "s", tmp_path / "t"
    for p in (v, s, t):
        p.mkdir()
    (v / "reward.json").write_text(json.dumps({"reward": 0, "solve": 0}))
    (s / "ask_human_metrics.json").write_text(json.dumps({"questions": [], "n_blockers": 3, "precision": None}))
    rl.main(str(v), str(s), str(t))
    out = json.loads((v / "reward.json").read_text())
    assert "precision" not in out and all(x is not None for x in out.values())
