import json
from pathlib import Path

from scripts.metrics_hil_swe import load_pass_rows
from scripts.run_hil_swe import result_is_complete


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_result_is_complete_false_when_sdk_error_present(tmp_path: Path) -> None:
    out_dir = tmp_path / "run" / "uid" / "ask_human" / "pass_1"
    _write_json(out_dir / "result.json", {"sdk_error": "network failure", "stop_reason": "complete"})
    assert result_is_complete(out_dir) is False


def test_result_is_complete_false_when_infra_stop_reason_present(tmp_path: Path) -> None:
    out_dir = tmp_path / "run" / "uid" / "ask_human" / "pass_1"
    _write_json(out_dir / "result.json", {"sdk_error": None, "stop_reason": "sidecar_start_failed"})
    assert result_is_complete(out_dir) is False


def test_load_pass_rows_marks_sdk_error_as_infra_even_with_eval_unresolved(tmp_path: Path) -> None:
    pass_dir = tmp_path / "run" / "uid-1" / "ask_human" / "pass_1"
    _write_json(pass_dir / "attempt.json", {"harness": "adk", "model": "test-model"})
    _write_json(pass_dir / "stats.json", {"num_steps": 0})
    _write_json(pass_dir / "result.json", {"sdk_error": "schema parse failed", "stop_reason": "sdk_error"})
    _write_json(
        pass_dir / "eval_result.json",
        {
            "eval_status": "unresolved",
            "resolved": False,
            "test_ran": True,
        },
    )

    rows = load_pass_rows(tmp_path / "run")
    assert len(rows) == 1
    assert rows[0]["status"] == "infra_error"


def test_load_pass_rows_marks_stop_reason_infra_without_sdk_error(tmp_path: Path) -> None:
    pass_dir = tmp_path / "run" / "uid-2" / "ask_human" / "pass_1"
    _write_json(pass_dir / "attempt.json", {"harness": "opencode", "model": "test-model"})
    _write_json(pass_dir / "stats.json", {"num_steps": 0})
    _write_json(pass_dir / "result.json", {"sdk_error": None, "stop_reason": "proxy_start_failed"})
    _write_json(
        pass_dir / "eval_result.json",
        {
            "eval_status": "unresolved",
            "resolved": False,
            "test_ran": True,
        },
    )

    rows = load_pass_rows(tmp_path / "run")
    assert len(rows) == 1
    assert rows[0]["status"] == "infra_error"
