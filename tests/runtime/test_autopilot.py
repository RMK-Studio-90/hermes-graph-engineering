"""Autopilot: one entry point from a task to a verified terminal result.

The workers here are deterministic stand-ins for a host agent that really change files in a real
workspace; every success is judged by deterministic evidence the runtime observes itself (real
files, real subprocess exit codes), never by what the worker says.
"""
import json
import re
import sys
from pathlib import Path

import pytest

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.autopilot import (CONTINUE, NEEDS_AGENT_TURN, NEEDS_OPERATOR, TERMINAL, Autopilot, AutopilotBudget,
                                  classify_failure, render_final_report)
from ge_runtime.engine import GraphEngine
from ge_runtime.planner import extract_evidence, plan_task
from ge_runtime.store import RunStore

PY = Path(sys.executable).name if Path(sys.executable).name.startswith("python") else "python3"
TASK = ('1. Create `hello.txt` containing "HELLO GE2" '
        '2. Verify it with `%s -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'hello.txt\').read_text()'
        '.strip() == \'HELLO GE2\' else 1)"`' % sys.executable)


class Worker:
    """Deterministic host-agent stand-in: acts in the workspace, then answers with JSON."""

    def __init__(self, workspace, act=None):
        self.workspace, self.act, self.calls = Path(workspace), act, []

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        self.calls.append({"node": order["node"], "attempt": order["attempt"], "context": context,
                           "diagnosis": order.get("diagnosis"), "inputs": order["inputs"]})
        if self.act is not None:
            outcome = self.act(self, order, context)
            if isinstance(outcome, ExecutorResult):
                return outcome
        outputs = {name: "done %s" % order["node"] for name in order["outputs"]}
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs=outputs)


def writes_hello(worker, order, context):
    if "hello" in order["node"]:
        (worker.workspace / "hello.txt").write_text("HELLO GE2\n", encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def autopilot(tmp_path, worker, **kwargs):
    engine = GraphEngine(RunStore(tmp_path / "store"))
    return Autopilot(engine, worker, sleep=lambda s: None, **kwargs)


# ------------------------------------------------------------------ planning
def test_the_planner_extracts_evidence_from_the_task_text():
    checks = extract_evidence(TASK)
    assert {"type": "file_contains", "path": "hello.txt", "text": "HELLO GE2"} in checks
    assert any(c["type"] == "command" and c["argv"][0] == sys.executable for c in checks)
    assert extract_evidence("run `git push origin main`") == []  # changing git state is work, not evidence
    planned = plan_task(TASK)
    ids = [n["id"] for n in planned["spec"]["nodes"]]
    assert ids[-1] == "acceptance" and planned["spec"]["require_evidence"] is True
    acceptance = planned["spec"]["nodes"][-1]
    assert acceptance["executor"] == "builtin" and len(acceptance["evidence"]) == 2
    assert all(n["idempotent"] and not n["side_effects"] for n in planned["spec"]["nodes"])


def test_the_planner_gates_steps_that_reach_outside_the_workspace():
    spec = plan_task("1. Build the site 2. Deploy it to production")["spec"]
    deploy = spec["nodes"][1]
    assert deploy["side_effects"] is True and deploy["idempotent"] is False
    assert deploy["gates"][0]["type"] == "approval" and deploy["rollback_boundary"]


# ------------------------------------------------------------------ autonomous runs
def test_a_safe_task_runs_to_a_verified_terminal_result_without_any_operator_step(tmp_path, workspace):
    worker = Worker(workspace, writes_hello)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL, report
    final = report["final_report"]
    assert final["outcome"] == "SUCCEEDED" and final["verified"] is True
    assert final["verification_level"] == "EVIDENCE"
    state, _spec = pilot.engine.load(run_id)
    plan = state["approvals"]["plan"]
    assert plan["decided_by"] == "policy:auto-approval"  # no operator approved anything
    assert [c["node"][:14] for c in worker.calls] == ["create_hello_t", "verify_it_with"]
    acceptance = state["nodes"]["acceptance"]["evidence"]
    assert all(e["passed"] for e in acceptance) and {e["type"] for e in acceptance} == {"file_contains", "command"}
    artifact = pilot.engine.store.read_artifact(run_id, acceptance[0]["artifact"])
    assert next(r for r in artifact["results"] if r["type"] == "command")["exit_code"] == 0
    phases = final["phases"]
    for phase in ("ANALYZE", "POLICY", "EXECUTE", "TERMINAL"):
        assert phase in phases or phase == "TERMINAL"
    assert pilot.engine.store.read_artifact(run_id, "final-report")["outcome"] == "SUCCEEDED"
    assert "VERIFIED" in render_final_report(final)
    types = [e["type"] for e in pilot.engine.store.read_trace(run_id)]
    assert "autopilot.finished" in types and "run.receipt_written" in types


def test_a_false_success_claim_is_caught_diagnosed_replanned_and_retested(tmp_path, workspace):
    def lazy_first_time(worker, order, context):
        # the first attempt claims success without doing the work; the repair round does it
        if "hello" in order["node"] and order["inputs"].get("repair_1"):
            (worker.workspace / "hello.txt").write_text("HELLO GE2\n", encoding="utf-8")

    worker = Worker(workspace, lazy_first_time)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL and report["final_report"]["verified"] is True, report
    final = report["final_report"]
    assert len(final["replans"]) == 1 and "reopened" in final["replans"][0]["reason"]
    reopened = [c for c in worker.calls if c["inputs"].get("repair_1")]
    assert reopened, worker.calls
    diagnosis = reopened[0]["inputs"]["repair_1"]
    assert diagnosis["category"] in ("EVIDENCE", "CONTRACT") and diagnosis["node"] == "acceptance"
    assert any("hello.txt" in json.dumps(c) for c in diagnosis["failed_checks"])
    assert {"DIAGNOSE", "REPLAN", "RETEST"} <= set(final["phases"])


def test_a_contract_failure_is_repaired_with_the_diagnosis_in_a_fresh_context(tmp_path, workspace):
    def bad_until_diagnosed(worker, order, context):
        writes_hello(worker, order, context)
        if "hello" in order["node"] and not order.get("diagnosis"):
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"wrong_key": "x"})

    worker = Worker(workspace, bad_until_diagnosed)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL and report["final_report"]["verified"] is True
    hello = [c for c in worker.calls if "hello" in c["node"]]
    assert len(hello) == 3  # attempt, engine retry (max_attempts 2), autopilot repair
    assert hello[2]["diagnosis"]["category"] == "STRUCTURAL" or hello[2]["diagnosis"]["category"] == "CONTRACT"
    assert "Diagnosis of the previous failed attempt" in hello[2]["context"].context
    assert "Diagnosis" not in hello[0]["context"].context
    digests = {c["context"].context_digest for c in hello}
    assert len(digests) == 3  # every attempt got its own, explicit context


def test_repeated_identical_failures_trigger_a_structural_replan_with_a_diagnosis_node(tmp_path, workspace):
    def stubborn(worker, order, context):
        if order["node"].endswith("_diag1"):
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"analysis": "write hello.txt first"})
        if "hello" in order["node"]:
            if "analysis" in order["inputs"]:
                writes_hello(worker, order, context)
                return None
            return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="tool crashed the same way")

    worker = Worker(workspace, stubborn)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL and report["final_report"]["verified"] is True, report
    state, spec = pilot.engine.load(run_id)
    diag = [n for n in spec.order if n.endswith("_diag1")]
    assert diag and state["nodes"][diag[0]]["status"] == "SUCCEEDED"
    policy = state["policy"]["decisions"][diag[0]]
    assert policy["risk"] == "READ_ONLY"  # the inserted diagnosis node may not change anything
    assert state["approvals"]["plan"]["basis"]["policy_digest"] == state["policy"]["digest"]  # re-approved


def test_hard_budgets_end_a_hopeless_run(tmp_path, workspace):
    def always_fails(worker, order, context):
        return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="failure %d" % len(worker.calls))

    worker = Worker(workspace, always_fails)
    pilot = autopilot(tmp_path, worker, budget=AutopilotBudget(max_attempts_total=6))
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL
    final = report["final_report"]
    assert final["outcome"] == "BUDGET_EXHAUSTED" and final["verified"] is False
    assert len(worker.calls) <= 7
    assert pilot.engine.store.read_receipt(run_id)["terminal_outcome"] == "BUDGET_EXHAUSTED"


def test_the_runtime_budget_is_enforced_across_calls(tmp_path, workspace):
    now = [1000.0]
    pilot = autopilot(tmp_path, None, budget=AutopilotBudget(max_runtime_seconds=60), clock=lambda: now[0])
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    assert pilot.drive(run_id)["status"] == NEEDS_AGENT_TURN
    now[0] += 61
    report = pilot.drive(run_id)
    assert report["final_report"]["outcome"] == "BUDGET_EXHAUSTED"
    assert "runtime budget" in report["final_report"]["failure"]["message"]


def test_risky_work_stops_at_a_named_operator_action_and_continues_after_it(tmp_path, workspace):
    worker = Worker(workspace)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task="1. Summarize the release notes 2. Deploy the service to production",
                         workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == NEEDS_OPERATOR
    [action] = report["next"]["operator"]
    assert action["command"] == "/ge-approve %s deploy_the_service_to_production:approval" % run_id
    assert [c["node"] for c in worker.calls] == ["summarize_the_release_notes"]
    pilot.engine.decide(run_id, "deploy_the_service_to_production:approval", approve=True, decided_by="operator")
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL and report["final_report"]["outcome"] == "SUCCEEDED"


def test_a_destructive_task_is_denied_by_policy_before_anything_runs(tmp_path, workspace):
    worker = Worker(workspace)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task="Delete all files in the repository with rm -rf .", workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == TERMINAL and report["final_report"]["outcome"] == "POLICY_DENIED"
    assert worker.calls == []


def test_the_run_continues_across_calls_processes_and_a_crash(tmp_path, workspace):
    def dies_once(worker, order, context):
        if len(worker.calls) == 1:
            raise KeyboardInterrupt  # the process goes away mid-dispatch
        writes_hello(worker, order, context)

    first = autopilot(tmp_path, Worker(workspace, dies_once))
    run_id = first.start(task=TASK, workspace=str(workspace))
    assert first.drive(run_id, call_budget_seconds=0)["status"] == CONTINUE  # a call budget boundary
    with pytest.raises(KeyboardInterrupt):
        first.drive(run_id)
    fresh_worker = Worker(workspace, writes_hello)
    second = autopilot(tmp_path, fresh_worker)  # a new process: new engine, store handle and worker
    report = second.drive(run_id)
    assert report["status"] == TERMINAL and report["final_report"]["verified"] is True
    assert fresh_worker.calls[0]["context"].correlation_id.endswith(":2")  # the abandoned claim was reclaimed
    usage = report["final_report"]["usage"]
    assert usage["drive_calls"] == 3


def test_without_a_worker_the_autopilot_asks_for_an_agent_turn_and_resumes_there(tmp_path, workspace):
    pilot = autopilot(tmp_path, None)
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    report = pilot.drive(run_id)
    assert report["status"] == NEEDS_AGENT_TURN and report["next"]["action"] == "auto"
    pilot.worker = Worker(workspace, writes_hello)
    assert pilot.drive(run_id)["status"] == TERMINAL


def test_every_node_gets_a_fresh_context_with_explicit_artifacts_only(tmp_path, workspace):
    worker = Worker(workspace, writes_hello)
    pilot = autopilot(tmp_path, worker)
    run_id = pilot.start(task=TASK, workspace=str(workspace))
    pilot.drive(run_id)
    first, second = worker.calls
    assert first["context"].correlation_id != second["context"].correlation_id
    assert set(second["inputs"]) == {"task", "previous"}  # only declared inputs, no conversation
    assert second["inputs"]["previous"] == "done %s" % first["node"]
    assert first["context"].context_sources == ("inputs:task",)
    trace = pilot.engine.store.read_trace(run_id)
    finished = [e for e in trace if e["type"] == "node.worker_finished"]
    assert [e["context_digest"] for e in finished] == [c["context"].context_digest for c in worker.calls]


def test_failure_classification():
    assert classify_failure({"last_error": {"code": "POLICY_VIOLATION"}}, []) == "POLICY"
    assert classify_failure({"last_error": {"code": "EVIDENCE_FAILED", "message": "x"}}, []) == "EVIDENCE"
    assert classify_failure({"last_error": {"code": "NODE_FAILED", "message": "x"}}, ["x", "x"]) == "STRUCTURAL"
    assert classify_failure({"last_error": {"code": "NODE_FAILED", "message": "host execution failed (x)"}},
                            []) == "TRANSIENT"
    assert classify_failure({"last_error": {"code": "NODE_FAILED", "message": "output contract violated"}},
                            []) == "CONTRACT"
