import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from metrics import summarize  # noqa: E402


def row(task, p, solve, nq=0, res=0, nb=0, mode="ask_human"):
    return {"task": task, "mode": mode, "pass_num": p, "solve": solve, "n_questions": nq,
            "blockers_resolved": res, "n_blockers": nb, "reward": 0.0}


def test_pass_at_k_and_pooled_ask_f1():
    rows = [row("t1", 1, 0, 2, 1, 2), row("t1", 2, 1, 2, 2, 2), row("t2", 1, 0, 4, 1, 3), row("t2", 2, 0, 0, 0, 3)]
    m = summarize(rows, 2)["ask_human"]
    assert m["pass_at_1"] == 0.0 and m["pass_at_2"] == 0.5
    p, r = 4 / 8, 4 / 10
    assert abs(m["ask_precision"] - p) < 1e-9 and abs(m["ask_recall"] - r) < 1e-9
    assert abs(m["ask_f1"] - 2 * p * r / (p + r)) < 1e-9


def test_infra_errors_excluded():
    m = summarize([row("t1", 1, None), row("t1", 2, 1)], 1)["ask_human"]
    assert m["n_passes"] == 1


def test_agent_failure_counts_as_unsolved_but_not_in_ask_sums():
    fail = row("t1", 1, 0.0, 0, 0, 2) | {"verified": False}
    m = summarize([fail, row("t1", 2, 1, 2, 2, 2)], 2)["ask_human"]
    assert m["pass_at_1"] == 0.0 and m["pass_at_2"] == 1.0 and m["n_agent_failures"] == 1
    assert m["ask_recall"] == 1.0  # the failed trial's unknown question log is not counted


def test_solve_of_classifies_exceptions():
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from eval import solve_of
    assert solve_of({"reward": 1.3}, {"solve": 1.0}) == 1.0
    assert solve_of({"reward": None, "exception_type": "ContextLengthExceededError"}, {}) == 0.0
    assert solve_of({"reward": None, "exception_type": "EnvironmentStartTimeoutError"}, {}) is None
    assert solve_of({"reward": None, "exception_type": "AgentSetupTimeoutError"}, {}) is None
