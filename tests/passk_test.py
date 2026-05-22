import unittest
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.passk import build_attempts, build_harness_attempts, compute_passk, summarize_rows, unbiased_estimate
from scripts.hil_swe_report import determine_readiness, oracle_patch_alignment
from scripts.summarize_passk import (
    collect_attempt_outputs,
    collect_official_results,
    filter_ambiguous_instance_results,
    has_official_hil_results,
    load_raw_samples,
    render_final_results_lines,
    render_metric_lines,
    required_test_passed,
    scheduled_tests_from_log,
    statuses_from_logs,
    statuses_from_output,
)


class PassKTest(unittest.TestCase):
    def import_upstream_run_hil_bench(self):
        repo_root = Path(__file__).resolve().parents[1]
        upstream_path = repo_root.parent / "hil-bench" / "run_hil_bench.py"
        if not upstream_path.exists():
            self.skipTest(f"upstream HiL-Bench run_hil_bench.py not found at {upstream_path}")

        old_modules = dict(sys.modules)
        chromadb = types.ModuleType("chromadb")
        chromadb.PersistentClient = lambda *args, **kwargs: None
        chromadb_utils = types.ModuleType("chromadb.utils")
        chromadb_embedding = types.ModuleType("chromadb.utils.embedding_functions")
        chromadb_embedding.SentenceTransformerEmbeddingFunction = lambda *args, **kwargs: None
        datasets = types.ModuleType("datasets")
        datasets.load_dataset = lambda *args, **kwargs: None
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        tqdm_mod = types.ModuleType("tqdm")
        tqdm_mod.tqdm = lambda *args, **kwargs: args[0] if args else []
        try:
            sys.modules.update(
                {
                    "chromadb": chromadb,
                    "chromadb.utils": chromadb_utils,
                    "chromadb.utils.embedding_functions": chromadb_embedding,
                    "datasets": datasets,
                    "dotenv": dotenv,
                    "tqdm": tqdm_mod,
                }
            )
            spec = importlib.util.spec_from_file_location("upstream_run_hil_bench_for_test", upstream_path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            return module
        finally:
            for name in ("chromadb", "chromadb.utils", "chromadb.utils.embedding_functions", "datasets", "dotenv", "tqdm"):
                if name in old_modules:
                    sys.modules[name] = old_modules[name]
                else:
                    sys.modules.pop(name, None)

    def write_parallel_trajectories(self, directory: Path, observations: list[str]) -> None:
        directory.mkdir(parents=True)
        steps = [{"action": "bash", "observation": observation} for observation in observations]
        (directory / "attempt.traj").write_text(json.dumps({"trajectory": steps}), encoding="utf-8")
        (directory / "trajectory.jsonl").write_text(
            "\n".join(json.dumps({"type": "sdk_event", "tool_name": "bash", "observation": observation}) for observation in observations) + "\n",
            encoding="utf-8",
        )

    def test_unbiased_formula(self):
        self.assertEqual(unbiased_estimate(3, 0, 2), 0.0)
        self.assertEqual(unbiased_estimate(3, 2, 2), 1.0)
        self.assertAlmostEqual(unbiased_estimate(4, 1, 2), 0.5)

    def test_author_summarize_rows_parity_with_imported_upstream(self):
        upstream = self.import_upstream_run_hil_bench()
        timeout_obs = "Command '['pytest']' timed out after 1 seconds"
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            valid_dirs = {}
            for name in ("a1", "a2", "a3", "b1", "c1", "c2", "c3", "d1", "d2", "d3"):
                valid_dirs[name] = root / name
                self.write_parallel_trajectories(valid_dirs[name], ["ok"])
            rerun_dir = root / "b3-rerun"
            self.write_parallel_trajectories(rerun_dir, [timeout_obs, timeout_obs, timeout_obs])
            rows = [
                {"task_name": "task-a", "model": "codex", "mode": "ask_human", "pass_num": 1, "status": "completed", "resolved": False, "trajectory_dir": str(valid_dirs["a1"]), "cost": 1, "num_steps": 2, "tokens_sent": 3, "tokens_received": 4, "num_questions": 1, "num_blockers_resolved": 0, "total_num_blockers": 2},
                {"task_name": "task-a", "model": "codex", "mode": "ask_human", "pass_num": 2, "status": "completed", "resolved": True, "trajectory_dir": str(valid_dirs["a2"]), "cost": 1, "num_steps": 2, "tokens_sent": 3, "tokens_received": 4, "num_questions": 1, "num_blockers_resolved": 1, "total_num_blockers": 2},
                {"task_name": "task-a", "model": "codex", "mode": "ask_human", "pass_num": 3, "status": "completed", "resolved": False, "trajectory_dir": str(valid_dirs["a3"]), "cost": 1, "num_steps": 2, "tokens_sent": 3, "tokens_received": 4, "num_questions": 0, "num_blockers_resolved": 0, "total_num_blockers": 2},
                {"task_name": "task-b", "model": "codex", "mode": "ask_human", "pass_num": 1, "status": "completed", "resolved": True, "trajectory_dir": str(valid_dirs["b1"]), "cost": 2, "num_steps": 2, "tokens_sent": 3, "tokens_received": 4, "num_questions": 1, "num_blockers_resolved": 1, "total_num_blockers": 1},
                {"task_name": "task-b", "model": "codex", "mode": "ask_human", "pass_num": 2, "status": "infra_error", "resolved": False, "trajectory_dir": str(root / "missing-infra"), "cost": 99, "num_steps": 99, "tokens_sent": 99, "tokens_received": 99, "num_questions": 99, "num_blockers_resolved": 99, "total_num_blockers": 99},
                {"task_name": "task-b", "model": "codex", "mode": "ask_human", "pass_num": 3, "status": "completed", "resolved": True, "trajectory_dir": str(rerun_dir), "cost": 99, "num_steps": 99, "tokens_sent": 99, "tokens_received": 99, "num_questions": 99, "num_blockers_resolved": 99, "total_num_blockers": 99},
                {"task_name": "task-c", "model": "claude-code", "mode": "full_info", "pass_num": 1, "status": "completed", "resolved": False, "trajectory_dir": str(valid_dirs["c1"]), "cost": 1, "num_steps": 1, "tokens_sent": 1, "tokens_received": 1, "num_questions": 0, "num_blockers_resolved": 0, "total_num_blockers": 0},
                {"task_name": "task-c", "model": "claude-code", "mode": "full_info", "pass_num": 2, "status": "completed", "resolved": False, "trajectory_dir": str(valid_dirs["c2"]), "cost": 1, "num_steps": 1, "tokens_sent": 1, "tokens_received": 1, "num_questions": 0, "num_blockers_resolved": 0, "total_num_blockers": 0},
                {"task_name": "task-c", "model": "claude-code", "mode": "full_info", "pass_num": 3, "status": "completed", "resolved": True, "trajectory_dir": str(valid_dirs["c3"]), "cost": 1, "num_steps": 1, "tokens_sent": 1, "tokens_received": 1, "num_questions": 0, "num_blockers_resolved": 0, "total_num_blockers": 0},
                {"task_name": "task-d", "model": "opencode", "mode": "ask_human", "pass_num": 1, "status": "completed", "resolved": False, "trajectory_dir": str(valid_dirs["d1"]), "cost": 0, "num_steps": 0, "tokens_sent": 0, "tokens_received": 0, "num_questions": 1, "num_blockers_resolved": 0, "total_num_blockers": 1},
                {"task_name": "task-d", "model": "opencode", "mode": "ask_human", "pass_num": 2, "status": "completed", "resolved": True, "trajectory_dir": str(valid_dirs["d2"]), "cost": 0, "num_steps": 0, "tokens_sent": 0, "tokens_received": 0, "num_questions": 1, "num_blockers_resolved": 1, "total_num_blockers": 1},
                {"task_name": "task-d", "model": "opencode", "mode": "ask_human", "pass_num": 3, "status": "completed", "resolved": True, "trajectory_dir": str(valid_dirs["d3"]), "cost": 0, "num_steps": 0, "tokens_sent": 0, "tokens_received": 0, "num_questions": 0, "num_blockers_resolved": 0, "total_num_blockers": 1},
            ]
            self.assertEqual(
                summarize_rows(rows, include_partial=False, expected_passes=3),
                upstream.summarize_rows(rows, include_partial=False, expected_passes=3),
            )
            self.assertEqual(
                summarize_rows(rows, include_partial=True, expected_passes=3),
                upstream.summarize_rows(rows, include_partial=True, expected_passes=3),
            )

    def test_author_summarize_rows_conditions_denominator_on_valid_passes(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            complete_a1 = root / "a1"
            complete_a2 = root / "a2"
            partial_b1 = root / "b1"
            for path in (complete_a1, complete_a2, partial_b1):
                path.mkdir()
                (path / "trajectory.jsonl").write_text(
                    json.dumps({"type": "attempt_start", "content": "ok"}) + "\n",
                    encoding="utf-8",
                )
            rows = [
                {
                    "task_name": "a",
                    "model": "codex",
                    "mode": "ask_human",
                    "pass_num": 1,
                    "resolved": False,
                    "trajectory_dir": str(complete_a1),
                    "num_questions": 1,
                    "num_blockers_resolved": 0,
                    "total_num_blockers": 2,
                },
                {
                    "task_name": "a",
                    "model": "codex",
                    "mode": "ask_human",
                    "pass_num": 2,
                    "resolved": True,
                    "trajectory_dir": str(complete_a2),
                    "num_questions": 1,
                    "num_blockers_resolved": 1,
                    "total_num_blockers": 2,
                },
                {
                    "task_name": "b",
                    "model": "codex",
                    "mode": "ask_human",
                    "pass_num": 1,
                    "resolved": True,
                    "trajectory_dir": str(partial_b1),
                    "num_questions": 1,
                    "num_blockers_resolved": 1,
                    "total_num_blockers": 1,
                },
            ]
            strict = summarize_rows(rows, include_partial=False, expected_passes=2)
            partial = summarize_rows(rows, include_partial=True, expected_passes=2)

        self.assertEqual(strict["ask_human"]["codex"]["pass_at_1_n"], 1)
        self.assertEqual(strict["ask_human"]["codex"]["pass_at_1"], 0.0)
        self.assertEqual(strict["ask_human"]["codex"]["pass_at_2_n"], 1)
        self.assertEqual(strict["ask_human"]["codex"]["pass_at_2"], 1.0)
        self.assertEqual(partial["ask_human"]["codex"]["pass_at_1_n"], 2)
        self.assertEqual(partial["ask_human"]["codex"]["pass_at_1"], 0.5)
        self.assertEqual(partial["ask_human"]["codex"]["pass_at_2_n"], 1)
        self.assertEqual(partial["ask_human"]["codex"]["pass_at_2"], 1.0)

    def test_all_failures(self):
        predictions = [{"instance_id": "a", "prefix": f"a-{i}", "attempt_index": i} for i in range(1, 4)]
        attempts = build_attempts(predictions, {p["prefix"]: False for p in predictions})
        metrics = compute_passk(attempts, [1, 2, 3])
        self.assertEqual(metrics["pass_at_k"], {"1": 0.0, "2": 0.0, "3": 0.0})

    def test_success_only_after_first_k(self):
        predictions = [{"instance_id": "a", "prefix": f"a-{i}", "attempt_index": i} for i in range(1, 4)]
        attempts = build_attempts(predictions, {"a-1": False, "a-2": False, "a-3": True})
        metrics = compute_passk(attempts, [1, 2, 3])
        self.assertEqual(metrics["pass_at_k"]["1"], 0.0)
        self.assertEqual(metrics["pass_at_k"]["2"], 0.0)
        self.assertEqual(metrics["pass_at_k"]["3"], 1.0)
        self.assertAlmostEqual(metrics["unbiased_pass_at_k"]["2"], 2 / 3)

    def test_one_success_in_first_k(self):
        predictions = [{"instance_id": "a", "prefix": f"a-{i}", "attempt_index": i} for i in range(1, 4)]
        attempts = build_attempts(predictions, {"a-1": False, "a-2": True, "a-3": False})
        metrics = compute_passk(attempts, [1, 2])
        self.assertEqual(metrics["pass_at_k"]["1"], 0.0)
        self.assertEqual(metrics["pass_at_k"]["2"], 1.0)
        self.assertAlmostEqual(metrics["unbiased_pass_at_k"]["2"], 2 / 3)

    def test_multiple_successes_with_n_greater_than_k(self):
        predictions = [{"instance_id": "a", "prefix": f"a-{i}", "attempt_index": i} for i in range(1, 5)]
        attempts = build_attempts(predictions, {"a-1": True, "a-2": False, "a-3": True, "a-4": False})
        metrics = compute_passk(attempts, [2, 3])
        self.assertEqual(metrics["pass_at_k"]["2"], 1.0)
        self.assertEqual(metrics["pass_at_k"]["3"], 1.0)
        self.assertAlmostEqual(metrics["unbiased_pass_at_k"]["2"], 5 / 6)
        self.assertEqual(metrics["unbiased_pass_at_k"]["3"], 1.0)

    def test_multiple_instances(self):
        predictions = [
            {"instance_id": "a", "prefix": "a-1", "attempt_index": 1},
            {"instance_id": "a", "prefix": "a-2", "attempt_index": 2},
            {"instance_id": "b", "prefix": "b-1", "attempt_index": 1},
            {"instance_id": "b", "prefix": "b-2", "attempt_index": 2},
        ]
        attempts = build_attempts(predictions, {"a-1": False, "a-2": True, "b-1": False, "b-2": False})
        metrics = compute_passk(attempts, [1, 2])
        self.assertEqual(metrics["pass_at_k"]["1"], 0.0)
        self.assertEqual(metrics["pass_at_k"]["2"], 0.5)
        self.assertEqual(metrics["unbiased_pass_at_k"]["2"], 0.5)

    def test_missing_eval_records_are_marked(self):
        attempts = build_attempts([{"instance_id": "a", "prefix": "a-1", "attempt_index": 1}], {})
        self.assertIsNone(attempts["a"][0]["resolved"])
        self.assertTrue(attempts["a"][0]["eval_missing"])

    def test_instance_id_fallback_only_when_unambiguous(self):
        single = build_attempts([{"instance_id": "a", "prefix": "a-1", "attempt_index": 1}], {"a": True})
        self.assertTrue(single["a"][0]["resolved"])

        multiple = build_attempts(
            [
                {"instance_id": "a", "prefix": "a-1", "attempt_index": 1},
                {"instance_id": "a", "prefix": "a-2", "attempt_index": 2},
            ],
            {"a": True},
        )
        self.assertIsNone(multiple["a"][0]["resolved"])
        self.assertIsNone(multiple["a"][1]["resolved"])

    def test_harness_attempts_are_separate(self):
        predictions = [
            {"harness": "claude-code", "instance_id": "a", "prefix": "claude-a-1", "attempt_index": 1},
            {"harness": "codex", "instance_id": "a", "prefix": "codex-a-1", "attempt_index": 1},
        ]
        groups = build_harness_attempts(predictions, {"claude-a-1": False, "codex-a-1": True})
        self.assertEqual(compute_passk(groups["claude-code"], [1])["pass_at_k"]["1"], 0.0)
        self.assertEqual(compute_passk(groups["codex"], [1])["pass_at_k"]["1"], 1.0)

    def test_current_test_pass_without_aligned_hil_evaluator_is_headline_fail(self):
        predictions = [{"instance_id": "a", "prefix": "a-1", "attempt_index": 1}]
        attempts = build_attempts(predictions, {"a-1": True}, {"a": "missing_aligned_tests"})
        metrics = compute_passk(attempts, [1])

        self.assertFalse(attempts["a"][0]["resolved"])
        self.assertTrue(attempts["a"][0]["swebench_pro_test_resolved"])
        self.assertEqual(metrics["pass_at_k"]["1"], 0.0)
        self.assertEqual(metrics["swebench_pro_test_pass_at_k"]["1"], 1.0)
        self.assertEqual(metrics["ungrounded_or_underconstrained_test_pass_count"], 1)
        self.assertEqual(metrics["missing_hil_aligned_eval_attempts"], 1)
        self.assertEqual(metrics["hil_evaluator_coverage"], 0.0)

    def test_current_test_pass_with_aligned_hil_evaluator_is_headline_pass(self):
        predictions = [{"instance_id": "a", "prefix": "a-1", "attempt_index": 1}]
        attempts = build_attempts(predictions, {"a-1": True}, {"a": "aligned"})
        metrics = compute_passk(attempts, [1])

        self.assertTrue(attempts["a"][0]["resolved"])
        self.assertEqual(metrics["pass_at_k"]["1"], 1.0)
        self.assertEqual(metrics["swebench_pro_test_pass_at_k"]["1"], 1.0)
        self.assertEqual(metrics["ungrounded_or_underconstrained_test_pass_count"], 0)
        self.assertEqual(metrics["missing_hil_aligned_eval_attempts"], 0)
        self.assertEqual(metrics["hil_evaluator_coverage"], 1.0)

    def test_current_test_fail_with_aligned_hil_evaluator_is_fail(self):
        predictions = [{"instance_id": "a", "prefix": "a-1", "attempt_index": 1}]
        attempts = build_attempts(predictions, {"a-1": False}, {"a": "aligned"})
        metrics = compute_passk(attempts, [1])

        self.assertFalse(attempts["a"][0]["resolved"])
        self.assertFalse(attempts["a"][0]["swebench_pro_test_resolved"])
        self.assertEqual(metrics["pass_at_k"]["1"], 0.0)
        self.assertEqual(metrics["swebench_pro_test_pass_at_k"]["1"], 0.0)

    def test_outcome_passk_uses_first_k_and_diagnostic_is_separate(self):
        predictions = [
            {"instance_id": "a", "prefix": "a-1", "attempt_index": 1},
            {"instance_id": "a", "prefix": "a-2", "attempt_index": 2},
            {"instance_id": "b", "prefix": "b-1", "attempt_index": 1},
            {"instance_id": "b", "prefix": "b-2", "attempt_index": 2},
        ]
        swebench_pro_tests = {"a-1": True, "a-2": False, "b-1": True, "b-2": True}
        statuses = {"a": "missing_aligned_tests", "b": "aligned"}
        attempts = build_attempts(predictions, swebench_pro_tests, statuses)
        metrics = compute_passk(attempts, [1, 2])

        self.assertEqual(metrics["pass_at_k"]["1"], 0.5)
        self.assertEqual(metrics["pass_at_k"]["2"], 0.5)
        self.assertEqual(metrics["swebench_pro_test_pass_at_k"]["1"], 1.0)
        self.assertEqual(metrics["swebench_pro_test_pass_at_k"]["2"], 1.0)
        self.assertEqual(metrics["ungrounded_or_underconstrained_test_pass_count"], 1)

    def test_instance_keyed_fallback_is_global_not_per_harness(self):
        predictions = [
            {"harness": "claude-code", "instance_id": "a", "prefix": "claude-a-1", "attempt_index": 1},
            {"harness": "codex", "instance_id": "a", "prefix": "codex-a-1", "attempt_index": 1},
            {"harness": "codex", "instance_id": "b", "prefix": "codex-b-1", "attempt_index": 1},
        ]
        filtered = filter_ambiguous_instance_results(predictions, {"a": True, "b": False, "codex-a-1": False})
        self.assertNotIn("a", filtered)
        self.assertEqual(filtered["b"], False)
        self.assertEqual(filtered["codex-a-1"], False)

    def test_oracle_patch_mismatch_is_reported_as_underconstrained(self):
        with TemporaryDirectory() as tmpdir:
            prepared = Path(tmpdir)
            (prepared / "oracle.jsonl").write_text(
                json.dumps({"instance_id": "a", "ground_truth_patch": "oracle"}) + "\n"
                + json.dumps({"instance_id": "b", "ground_truth_patch": "same"}) + "\n",
                encoding="utf-8",
            )
            (prepared / "samples.csv").write_text("instance_id,patch\na,sample\nb,same\n", encoding="utf-8")
            alignment = oracle_patch_alignment(prepared)

        self.assertEqual(alignment["compared"], 2)
        self.assertEqual(alignment["mismatch_count"], 1)
        metrics = {
            "harnesses": {
                "claude-code": {"missing_eval_attempts": 0},
                "codex": {"missing_eval_attempts": 0},
            }
        }
        process = {
            "trace_completeness": {
                "final_patch_submission_exists": True,
                "final_outcome_exists": True,
                "human_facing_events_have_responses": True,
                "human_facing_events_have_raw_native_payload": True,
                "human_facing_events_have_normalized_request_type": True,
                "ask_human_calls_have_audit_cache_metadata": True,
            },
            "harnesses": {"claude-code": {}, "codex": {}},
        }
        status, reason = determine_readiness(metrics, process, alignment)
        self.assertEqual(status, "FAIL")
        self.assertIn("underconstrained", reason)

    def test_output_status_aliases_parameterized_tests(self):
        statuses = statuses_from_output(
            {
                "tests": [
                    {
                        "name": "test/units/utils/test_vars.py::TestVariableUtils::test_merge_hash_non_recursive_and_list_append_rp[param]",
                        "status": "PASSED",
                    }
                ]
            }
        )
        self.assertTrue(
            required_test_passed(
                "test/units/utils/test_vars.py::TestVariableUtils::test_merge_hash_non_recursive_and_list_append_rp",
                statuses,
            )
        )

    def test_log_statuses_normalize_ansi_and_xdist_glued_lines(self):
        with TemporaryDirectory() as tmpdir:
            stdout = Path(tmpdir) / "stdout.log"
            stdout.write_text(
                "[gw4]\x1b[36m [ 93%] \x1b[0m\x1b[32mPASSED\x1b[0m "
                "test/units/utils/test_vars.py::TestVariableUtils::test_merge_hash_non_recursive_and_list_append_rp"
                "[g[gw8]\x1b[36m [100%] \x1b[0m\x1b[32mPASSED\x1b[0m "
                "test/units/utils/test_vars.py::TestVariableUtils::test_merge_hash_non_recursive_and_list_replace\n",
                encoding="utf-8",
            )
            statuses = statuses_from_logs(stdout)
        self.assertTrue(
            required_test_passed(
                "test/units/utils/test_vars.py::TestVariableUtils::test_merge_hash_non_recursive_and_list_append_rp",
                statuses,
            )
        )
        self.assertTrue(
            required_test_passed(
                "test/units/utils/test_vars.py::TestVariableUtils::test_merge_hash_non_recursive_and_list_replace",
                statuses,
            )
        )

    def test_log_statuses_use_all_passed_summary_when_node_count_matches(self):
        with TemporaryDirectory() as tmpdir:
            stdout = Path(tmpdir) / "stdout.log"
            stdout.write_text(
                "test/units/utils/test_vars.py::TestVariableUtils::test_one\n"
                "test/units/utils/test_vars.py::TestVariableUtils::test_two\n"
                "[gw0] [ 50%] PASSED test/units/utils/test_vars.py::TestVariableUtils::test_one"
                "[gw1] [100%] PASSED test/units/utils/test_vars.py::TestVariableUtils::test_one\n"
                "============================= 2 passed in 1.23s ==============================\n",
                encoding="utf-8",
            )
            statuses = statuses_from_logs(stdout)
        self.assertTrue(required_test_passed("test/units/utils/test_vars.py::TestVariableUtils::test_one", statuses))
        self.assertTrue(required_test_passed("test/units/utils/test_vars.py::TestVariableUtils::test_two", statuses))

    def test_scheduled_tests_ignore_interleaved_worker_output(self):
        text = (
            "scheduling tests via LoadScheduling\n\n"
            "test/units/utils/test_vars.py::TestVariableUtils::test_one \n"
            "test/units/utils/test_vars.py::TestVariableUtils::test_two \n"
            "[gw0] [ 50%] PASSED test/units/utils/test_vars.py::TestVariableUtils::test_one"
            "[gw1] [100%] PASSED test/units/utils/test_vars.py::TestVariableUtils::test_tw\n"
            "============================= 2 passed in 1.23s ==============================\n"
        )
        self.assertEqual(
            scheduled_tests_from_log(text),
            [
                "test/units/utils/test_vars.py::TestVariableUtils::test_one",
                "test/units/utils/test_vars.py::TestVariableUtils::test_two",
            ],
        )

    def test_stale_eval_outputs_are_rejected(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run"
            official = run_dir / "official-eval" / "inst"
            official.mkdir(parents=True)
            command = run_dir / "official-eval" / "command.json"
            command.write_text("[]", encoding="utf-8")
            output = official / "prefix_output.json"
            output.write_text(json.dumps({"tests": []}), encoding="utf-8")
            predictions = run_dir / "predictions.json"
            predictions.write_text("[]", encoding="utf-8")
            os.utime(output, (1, 1))
            os.utime(predictions, (2, 2))
            os.utime(command, (3, 3))
            with self.assertRaises(SystemExit):
                collect_attempt_outputs(run_dir, root / "samples.csv", predictions)

    def test_official_hil_results_take_precedence_over_swebench_outputs(self):
        with TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            hil_dir = run_dir / "official-hil-eval"
            swe_dir = run_dir / "official-eval" / "inst"
            hil_dir.mkdir(parents=True)
            swe_dir.mkdir(parents=True)
            (hil_dir / "results_by_prefix.json").write_text(json.dumps({"prefix": True}), encoding="utf-8")
            (swe_dir / "prefix_output.json").write_text(json.dumps({"prefix": "prefix", "resolved": False}), encoding="utf-8")

            self.assertTrue(has_official_hil_results(run_dir))
            self.assertEqual(collect_official_results(run_dir), {"prefix": True})

    def test_load_raw_samples_accepts_large_csv_fields(self):
        with TemporaryDirectory() as tmpdir:
            samples = Path(tmpdir) / "samples.csv"
            large_statement = "x" * 200000
            samples.write_text(f"instance_id,problem_statement,fail_to_pass,pass_to_pass\nlarge,{large_statement},[],[]\n", encoding="utf-8")
            rows = load_raw_samples(samples)
        self.assertEqual(rows["large"]["problem_statement"], large_statement)

    def test_summary_renders_per_task_success_and_tail_results(self):
        predictions = [
            {"harness": "codex", "instance_id": "a", "prefix": "a-1", "attempt_index": 1},
            {"harness": "codex", "instance_id": "a", "prefix": "a-2", "attempt_index": 2},
        ]
        metrics = compute_passk(build_attempts(predictions, {"a-1": False, "a-2": True}), [1, 2])
        lines = render_metric_lines(metrics)
        self.assertIn("- a: success=True; attempts: 1=False/diag=False, 2=True/diag=True", lines)
        final_lines = render_final_results_lines({"harnesses": {"codex": metrics}})
        self.assertEqual(
            final_lines[-1],
            "- codex: hil_swe_outcome_pass@1=0.0000, hil_swe_outcome_pass@2=1.0000; "
            "unbiased_hil_swe_outcome_pass@1=0.5000, unbiased_hil_swe_outcome_pass@2=1.0000; "
            "swebench_pro_test_pass@1=0.0000, swebench_pro_test_pass@2=1.0000; "
            "unbiased_swebench_pro_test_pass@1=0.5000, unbiased_swebench_pro_test_pass@2=1.0000; "
            "hil_evaluator_coverage=1.0000; underconstrained_test_pass_attempts=0; missing_eval_attempts=0; "
            "missing_hil_aligned_eval_attempts=0",
        )


if __name__ == "__main__":
    unittest.main()
