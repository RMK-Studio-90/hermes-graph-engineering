"""Engine lifecycle: transitions, gates, failures, retries, persistence, recovery, verification."""
import json

import pytest

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.engine import GraphEngine, explain_view, pending_work, plan_view, status_summary
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.executors import AgentExecutor, BuiltinExecutor, ExecutorCatalog
from ge_runtime.store import RunStore


def text_pipeline(text="  hello   world "):
    return {
        "schema_version": 1,
        "graph_id": "pipeline",
        "inputs": {"text": text},
        "nodes": [
            {"id": "read_input", "purpose": "read", "executor": "builtin", "operation": {"op": "identity"},
             "inputs": {"value": "graph.inputs.text"}, "outputs": {"value": "string"}},
            {"id": "validate_input", "purpose": "validate", "executor": "builtin", "depends_on": ["read_input"],
             "operation": {"op": "require", "args": {"type": "string", "non_empty": True}},
             "inputs": {"value": "read_input.value"}, "outputs": {"value": "string", "valid": "boolean"},
             "success_criteria": [{"output": "valid", "check": "true"}]},
            {"id": "transform_input", "purpose": "transform", "executor": "builtin", "depends_on": ["validate_input"],
             "operation": {"op": "text_transform", "args": {"steps": ["collapse_whitespace", "upper"]}},
             "inputs": {"value": "validate_input.value"}, "outputs": {"value": "string"}},
            {"id": "verify_result", "purpose": "verify", "executor": "builtin", "depends_on": ["transform_input"],
             "operation": {"op": "compare", "args": {"expected": "HELLO WORLD"}},
             "inputs": {"value": "transform_input.value"}, "outputs": {"matches": "boolean"},
             "success_criteria": [{"output": "matches", "check": "true"}]},
        ],
    }


def agent_node(node_id, depends_on=(), **extra):
    node = {"id": node_id, "purpose": "do %s" % node_id, "executor": "agent", "depends_on": list(depends_on),
            "outputs": {"result": "string"}, "success_criteria": [{"output": "result", "check": "non_empty"}]}
    node.update(extra)
    return node


def agent_spec(*nodes):
    return {"schema_version": 1, "graph_id": "agentic", "nodes": list(nodes)}


@pytest.fixture
def engine(tmp_path):
    return GraphEngine(RunStore(tmp_path))


def approved(engine, raw):
    run_id = engine.create(raw, created_by="test")["run_id"]
    engine.decide(run_id, "plan", approve=True, decided_by="tester")
    return run_id


def node_status(engine, run_id):
    state, _spec = engine.load(run_id)
    return {nid: ns["status"] for nid, ns in state["nodes"].items()}


# -- happy path ----------------------------------------------------------------------------------

def test_builtin_pipeline_runs_to_success_and_verifies(engine):
    run_id = engine.create(text_pipeline(), created_by="test")["run_id"]
    assert engine.load(run_id)[0]["status"] == "DRAFT"
    assert set(node_status(engine, run_id).values()) == {"PENDING"}
    engine.decide(run_id, "plan", approve=True, decided_by="tester")
    state = engine.execute(run_id)
    assert state["status"] == "SUCCEEDED"
    assert state["nodes"]["transform_input"]["outputs"] == {"value": "HELLO WORLD"}
    receipt = engine.verify(run_id)
    assert receipt["verified"] is True and receipt["terminal_outcome"] == "SUCCEEDED"
    ok, detail = engine.store.verify_trace(run_id)
    assert ok, detail
    assert (engine.store.run_dir(run_id) / "receipt.json").is_file()


def test_execution_requires_plan_approval(engine):
    run_id = engine.create(text_pipeline())["run_id"]
    with pytest.raises(GraphEngineeringError) as info:
        engine.execute(run_id)
    assert info.value.code is ErrorCode.GATE_FAILED
    assert set(node_status(engine, run_id).values()) == {"PENDING"}


def test_plan_approval_can_be_disabled_by_configuration(tmp_path):
    engine = GraphEngine(RunStore(tmp_path), require_plan_approval=False)
    run_id = engine.create(text_pipeline())["run_id"]
    assert engine.execute(run_id)["status"] == "SUCCEEDED"
    assert engine.verify(run_id)["verified"] is True


def test_plan_denial_cancels_run(engine):
    run_id = engine.create(text_pipeline())["run_id"]
    state = engine.decide(run_id, "plan", approve=False, decided_by="tester")
    assert state["status"] == "CANCELLED"
    assert set(node_status(engine, run_id).values()) == {"SKIPPED"}


def test_terminal_run_cannot_run_again(engine):
    run_id = approved(engine, text_pipeline())
    engine.execute(run_id)
    with pytest.raises(GraphEngineeringError) as info:
        engine.execute(run_id)
    assert info.value.code is ErrorCode.INVALID_TRANSITION


# -- failures ------------------------------------------------------------------------------------

def test_criteria_failure_fails_node_and_blocks_downstream(engine):
    raw = text_pipeline()
    raw["nodes"][3]["operation"]["args"]["expected"] = "something else"
    raw["nodes"].append({"id": "after", "purpose": "after", "executor": "builtin", "depends_on": ["verify_result"],
                         "operation": {"op": "identity"}, "inputs": {"value": "graph.inputs.text"},
                         "outputs": {"value": "string"}})
    run_id = approved(engine, raw)
    state = engine.execute(run_id)
    assert state["status"] == "FAILED"
    statuses = node_status(engine, run_id)
    assert statuses["verify_result"] == "FAILED"
    assert statuses["after"] in ("BLOCKED", "SKIPPED")
    assert state["failure"]["code"] == "NODE_FAILED" and state["failure"]["node"] == "verify_result"
    assert "success criteria failed" in state["nodes"]["verify_result"]["last_error"]["message"]
    assert engine.verify(run_id)["verified"] is False


def test_exit_success_is_not_enough_without_passing_criteria(tmp_path):
    class LyingExecutor(BuiltinExecutor):
        def execute(self, request):
            claimed = {"value": "x", "valid": False}
            return ExecutorResult(ExecutorOutcome.SUCCESS,
                                  outputs={k: v for k, v in claimed.items() if k in request.contract["outputs"]})

    engine = GraphEngine(RunStore(tmp_path), ExecutorCatalog([LyingExecutor(), AgentExecutor()]))
    raw = text_pipeline()
    raw["nodes"] = raw["nodes"][:2]
    run_id = approved(engine, raw)
    state = engine.execute(run_id)
    assert state["nodes"]["validate_input"]["status"] == "FAILED"
    assert state["status"] == "FAILED"


def test_input_rejected_by_validation_fails_closed(engine):
    run_id = approved(engine, text_pipeline(text="   "))
    state = engine.execute(run_id)
    assert state["nodes"]["validate_input"]["status"] == "FAILED"
    assert "input rejected" in state["nodes"]["validate_input"]["last_error"]["message"]
    assert node_status(engine, run_id)["transform_input"] in ("SKIPPED", "BLOCKED")


def test_continue_policy_blocks_dependents_but_runs_other_branches(engine):
    raw = {
        "schema_version": 1, "graph_id": "branches", "inputs": {"text": "  "},
        "nodes": [
            {"id": "bad", "purpose": "fails", "executor": "builtin", "on_failure": "continue",
             "operation": {"op": "require", "args": {"non_empty": True}},
             "inputs": {"value": "graph.inputs.text"}, "outputs": {"valid": "boolean"}},
            {"id": "child", "purpose": "blocked", "executor": "builtin", "depends_on": ["bad"],
             "operation": {"op": "identity"}, "inputs": {"value": "graph.inputs.text"}, "outputs": {"value": "string"}},
            {"id": "other", "purpose": "independent", "executor": "builtin",
             "operation": {"op": "measure"}, "inputs": {"value": "graph.inputs.text"}, "outputs": {"length": "integer"}},
        ],
    }
    run_id = approved(engine, raw)
    state = engine.execute(run_id)
    assert node_status(engine, run_id) == {"bad": "FAILED", "child": "BLOCKED", "other": "SUCCEEDED"}
    assert state["status"] == "FAILED"
    summary = status_summary(*engine.load(run_id))
    assert summary["blocked_nodes"][0]["node"] == "child" and "bad" in summary["blocked_nodes"][0]["reason"]


def test_executor_crash_is_contained(tmp_path):
    class CrashingExecutor(BuiltinExecutor):
        def execute(self, request):
            raise RuntimeError("boom")

    engine = GraphEngine(RunStore(tmp_path), ExecutorCatalog([CrashingExecutor(), AgentExecutor()]))
    run_id = approved(engine, text_pipeline())
    state = engine.execute(run_id)
    assert state["nodes"]["read_input"]["status"] == "FAILED"
    assert "executor raised RuntimeError" in state["nodes"]["read_input"]["last_error"]["message"]


def test_retryable_failures_retry_up_to_max_attempts(tmp_path):
    calls = {"n": 0}

    class FlakyExecutor(BuiltinExecutor):
        def execute(self, request):
            calls["n"] += 1
            if calls["n"] < 3:
                return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="flaky")
            return super().execute(request)

    engine = GraphEngine(RunStore(tmp_path), ExecutorCatalog([FlakyExecutor(), AgentExecutor()]))
    raw = text_pipeline()
    raw["nodes"] = raw["nodes"][:1]
    raw["nodes"][0]["retry"] = {"max_attempts": 3}
    run_id = approved(engine, raw)
    state = engine.execute(run_id)
    assert state["status"] == "SUCCEEDED"
    assert state["nodes"]["read_input"]["attempts"] == 3
    assert [h["outcome"] for h in state["nodes"]["read_input"]["history"]] == [
        "RETRYABLE_FAILURE", "RETRYABLE_FAILURE", "SUCCESS"]


def test_disabled_executor_refuses_execution_without_state_change(tmp_path):
    store = RunStore(tmp_path)
    run_id = approved(GraphEngine(store), text_pipeline())
    restricted = GraphEngine(store, ExecutorCatalog(disabled=["builtin"]))
    with pytest.raises(GraphEngineeringError) as info:
        restricted.execute(run_id)
    assert info.value.code is ErrorCode.EXECUTOR_UNAVAILABLE
    assert set(node_status(restricted, run_id).values()) == {"PENDING"}


def test_step_budget_holds_run_for_resume(tmp_path):
    engine = GraphEngine(RunStore(tmp_path), max_steps=2)
    run_id = approved(engine, text_pipeline())
    state = engine.execute(run_id)
    assert state["status"] == "WAITING"
    assert any("budget" in (h.get("detail") or "") for h in state["holds"])
    assert engine.execute(run_id)["status"] == "SUCCEEDED"


# -- agent work, gates -----------------------------------------------------------------------------

def test_agent_nodes_wait_for_submission_and_validate_outputs(engine):
    run_id = approved(engine, agent_spec(agent_node("research"), agent_node("write_up", ["research"],
                                         inputs={"notes": "research.result"})))
    state = engine.execute(run_id)
    assert state["status"] == "WAITING"
    assert state["holds"] == [{"kind": "WAITING_FOR_SUBMISSION", "node": "research", "gate": None}]
    orders = pending_work(*engine.load(run_id))
    assert [o["node"] for o in orders] == ["research"]

    with pytest.raises(GraphEngineeringError) as info:
        engine.submit(run_id, "write_up", outputs={"result": "too early"})
    assert info.value.code is ErrorCode.INVALID_TRANSITION

    state = engine.submit(run_id, "research", outputs={"result": "findings"})
    assert state["nodes"]["research"]["status"] == "SUCCEEDED"
    orders = pending_work(*engine.load(run_id))
    assert orders[0]["node"] == "write_up" and orders[0]["inputs"] == {"notes": "findings"}
    state = engine.submit(run_id, "write_up", outputs={"result": "report"})
    assert state["status"] == "SUCCEEDED"
    assert engine.verify(run_id)["verified"] is True


def test_agent_submission_violating_contract_fails_node(engine):
    run_id = approved(engine, agent_spec(agent_node("work")))
    engine.execute(run_id)
    state = engine.submit(run_id, "work", outputs={"result": 42, "extra": True})
    assert state["nodes"]["work"]["status"] == "FAILED"
    assert "output contract violated" in state["nodes"]["work"]["last_error"]["message"]
    assert state["status"] == "FAILED"


def test_agent_can_report_failure(engine):
    run_id = approved(engine, agent_spec(agent_node("work")))
    engine.execute(run_id)
    state = engine.submit(run_id, "work", failed=True, detail="no access")
    assert state["nodes"]["work"]["status"] == "FAILED" and state["status"] == "FAILED"


def test_approval_gate_holds_until_operator_decides(engine):
    node = agent_node("deploy", side_effects=True, rollback_boundary="release",
                      gates=[{"id": "approve", "type": "approval"}])
    run_id = approved(engine, agent_spec(agent_node("build"), dict(node, depends_on=["build"])))
    engine.execute(run_id)
    state = engine.submit(run_id, "build", outputs={"result": "artifact"})
    assert state["status"] == "WAITING"
    assert state["holds"] == [{"kind": "WAITING_FOR_APPROVAL", "node": "deploy", "gate": "approve"}]
    assert state["nodes"]["deploy"]["status"] == "READY" and state["nodes"]["deploy"]["attempts"] == 0
    summary = status_summary(*engine.load(run_id))
    assert {"target": "deploy:approve", "type": "approval", "status": "PENDING"} in summary["gates"]

    with pytest.raises(GraphEngineeringError):
        engine.decide(run_id, "deploy:nonexistent", approve=True, decided_by="op")
    state = engine.decide(run_id, "deploy:approve", approve=True, decided_by="op")
    assert state["nodes"]["deploy"]["status"] == "RUNNING" and state["nodes"]["deploy"]["wait"] == "submission"
    state = engine.submit(run_id, "deploy", outputs={"result": "deployed"})
    assert state["status"] == "SUCCEEDED"
    receipt = engine.verify(run_id)
    assert receipt["verified"] is True
    assert any(c["check"] == "gate:deploy:approve" and c["passed"] for c in receipt["checks"])


def test_denied_gate_fails_node_with_gate_failed(engine):
    node = agent_node("deploy", side_effects=True, rollback_boundary="release",
                      gates=[{"id": "approve", "type": "approval"}])
    run_id = approved(engine, agent_spec(node))
    engine.execute(run_id)
    state = engine.decide(run_id, "deploy:approve", approve=False, decided_by="op")
    assert state["nodes"]["deploy"]["status"] == "FAILED"
    assert state["failure"]["code"] == "GATE_FAILED" and state["status"] == "FAILED"


def test_review_gate_requires_decision_after_outputs(engine):
    run_id = approved(engine, agent_spec(agent_node("draft", gates=[{"id": "review", "type": "review"}])))
    engine.execute(run_id)
    state = engine.submit(run_id, "draft", outputs={"result": "text"})
    assert state["holds"][0]["kind"] == "WAITING_FOR_REVIEW"
    assert state["nodes"]["draft"]["status"] == "RUNNING"
    state = engine.decide(run_id, "draft:review", approve=True, decided_by="op")
    assert state["status"] == "SUCCEEDED" and engine.verify(run_id)["verified"] is True


def test_tampered_approval_binding_fails_verification(engine):
    run_id = approved(engine, text_pipeline())
    engine.execute(run_id)
    state = engine.store.load(run_id)
    state["approvals"]["plan"]["binding_digest"] = "sha256:" + "0" * 64
    engine.store.save(run_id, state)  # a consistent checksum, but a forged approval
    receipt = engine.verify(run_id)
    assert receipt["verified"] is False
    assert [c["check"] for c in receipt["checks"] if not c["passed"]] == ["plan_approval"]


def test_cancel_skips_open_nodes(engine):
    run_id = approved(engine, agent_spec(agent_node("a"), agent_node("b", ["a"])))
    engine.execute(run_id)
    state = engine.cancel(run_id, decided_by="op")
    assert state["status"] == "CANCELLED" and set(node_status(engine, run_id).values()) == {"SKIPPED"}
    with pytest.raises(GraphEngineeringError):
        engine.submit(run_id, "a", outputs={"result": "late"})


# -- persistence and recovery ---------------------------------------------------------------------

def test_state_survives_new_engine_instance(tmp_path):
    run_id = approved(GraphEngine(RunStore(tmp_path)), agent_spec(agent_node("a"), agent_node("b", ["a"])))
    GraphEngine(RunStore(tmp_path)).execute(run_id)
    fresh = GraphEngine(RunStore(tmp_path))
    fresh.submit(run_id, "a", outputs={"result": "x"})
    assert GraphEngine(RunStore(tmp_path)).status(run_id)["current_nodes"] == ["b"]


def test_interrupted_idempotent_node_is_requeued_on_resume(engine):
    run_id = approved(engine, text_pipeline())
    state = engine.store.load(run_id)
    state["status"] = "RUNNING"
    state["nodes"]["read_input"].update(status="RUNNING", attempts=1)
    engine.store.save(run_id, state)  # simulate a crash right after the write-ahead record
    state = engine.resume(run_id)
    assert {"node": "read_input", "action": "requeued"} in state["recovered"]
    assert state["status"] == "SUCCEEDED"
    assert state["nodes"]["read_input"]["attempts"] == 2


def test_interrupted_non_idempotent_node_is_never_replayed(engine):
    node = agent_node("deploy", side_effects=True, rollback_boundary="release",
                      gates=[{"id": "approve", "type": "approval"}])
    run_id = approved(engine, agent_spec(node))
    state = engine.store.load(run_id)
    state["status"] = "RUNNING"
    state["nodes"]["deploy"].update(status="RUNNING", attempts=1, wait=None)
    engine.store.save(run_id, state)
    state = engine.resume(run_id)
    assert {"node": "deploy", "action": "held_for_operator"} in state["recovered"]
    assert state["status"] == "WAITING" and state["holds"][0]["kind"] == "NEEDS_ATTENTION"
    assert state["nodes"]["deploy"]["attempts"] == 1
    with pytest.raises(GraphEngineeringError):
        engine.submit(run_id, "deploy", outputs={"result": "x"})
    state = engine.retry_node(run_id, "deploy", decided_by="op")
    assert state["nodes"]["deploy"]["wait"] in ("approval", "submission")


def test_waiting_submission_survives_resume(engine):
    run_id = approved(engine, agent_spec(agent_node("a")))
    engine.execute(run_id)
    state = engine.resume(run_id)
    assert state["recovered"] == [] and state["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION"


@pytest.mark.parametrize("corrupt", [
    lambda text: text[: len(text) // 2],
    lambda text: text.replace('"status": "DRAFT"', '"status": "SUCCEEDED"'),
    lambda text: json.dumps(dict(json.loads(text), schema_version=99)),
    lambda text: json.dumps(["not", "an", "envelope"]),
])
def test_corrupt_state_is_rejected_and_left_untouched(engine, corrupt):
    run_id = engine.create(text_pipeline())["run_id"]
    path = engine.store.run_dir(run_id) / "state.json"
    damaged = corrupt(path.read_text(encoding="utf-8"))
    path.write_text(damaged, encoding="utf-8")
    for action in (lambda: engine.status(run_id), lambda: engine.execute(run_id), lambda: engine.resume(run_id)):
        with pytest.raises(GraphEngineeringError) as info:
            action()
        assert info.value.code is ErrorCode.STATE_CORRUPT
    assert path.read_text(encoding="utf-8") == damaged


def test_state_with_tampered_spec_is_corrupt(engine):
    run_id = engine.create(text_pipeline())["run_id"]
    state = engine.store.load(run_id)
    state["spec"]["nodes"][0]["purpose"] = "changed after approval"
    engine.store.save(run_id, state)
    with pytest.raises(GraphEngineeringError) as info:
        engine.load(run_id)
    assert info.value.code is ErrorCode.STATE_CORRUPT


def test_unknown_run_and_invalid_ids(engine):
    with pytest.raises(GraphEngineeringError) as info:
        engine.status("pipeline-20260101t000000z-abcdef")
    assert info.value.code is ErrorCode.RUN_NOT_FOUND
    with pytest.raises(GraphEngineeringError) as info:
        engine.status("../../etc")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_trace_tampering_is_detected(engine):
    run_id = approved(engine, text_pipeline())
    engine.execute(run_id)
    path = engine.store.run_dir(run_id) / "trace.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[1])
    event["type"] = "forged"
    lines[1] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    receipt = engine.verify(run_id)
    assert receipt["verified"] is False
    assert any(c["check"] == "trace_chain" and not c["passed"] for c in receipt["checks"])


def test_run_lock_is_exclusive(engine):
    run_id = engine.create(text_pipeline())["run_id"]
    import threading

    entered = threading.Event()
    release = threading.Event()
    errors = []

    def holder():
        with engine.store.lock(run_id):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    entered.wait(5)
    try:
        with engine.store.lock(run_id, timeout=0.2):
            pass
    except GraphEngineeringError as exc:
        errors.append(exc.code)
    release.set()
    thread.join()
    assert errors == [ErrorCode.RUN_LOCKED]


# -- views -------------------------------------------------------------------------------------------

def test_plan_and_explain_views(engine):
    raw = agent_spec(agent_node("a"), agent_node("b", ["a"], inputs={"x": "a.result"}), agent_node("c", ["a"]),
                     agent_node("d", ["b", "c"], side_effects=True, rollback_boundary="publish",
                                gates=[{"id": "go", "type": "approval", "description": "publishes"}]))
    spec = engine.validate(raw)
    plan = plan_view(spec)
    assert plan["layers"] == [["a"], ["b", "c"], ["d"]]
    assert plan["rollback_boundaries"] == {"publish": ["d"]}
    assert {"target": "d:go", "type": "approval", "owner": "operator"} in plan["gates"]
    explained = {n["id"]: n for n in explain_view(spec)["nodes"]}
    assert explained["b"]["dependencies"] == [{"node": "a", "reason": "consumes x <- a.result"}]
    assert "ordering constraint" in explained["c"]["dependencies"][0]["reason"]
    assert "side effects require explicit human approval" in explained["d"]["gates"][0]["reason"]


def test_filesystem_write_failure_is_actionable_not_internal(tmp_path, monkeypatch):
    import ge_runtime.store as store_module

    def refuse(path, text):
        raise FileNotFoundError("simulated: path too long")

    monkeypatch.setattr(store_module, "atomic_write_text", refuse)
    engine = GraphEngine(RunStore(tmp_path))
    with pytest.raises(GraphEngineeringError) as info:
        engine.create(text_pipeline())
    error = info.value
    assert error.code is ErrorCode.STATE_WRITE_FAILED
    assert "could not write the run state" in error.message and "FileNotFoundError" in error.message
    assert str(tmp_path) not in error.message  # never leak absolute paths into chat
    assert error.details["path_length"] > 0
    assert engine.store.list_runs() == []  # nothing half-created is listed


def test_long_path_hint_on_windows(monkeypatch):
    import os as real_os
    import ge_runtime.store as store_module
    from pathlib import Path

    class _FakeOS:
        name = "nt"

    # Replace the module-local `os` reference only; never mutate the real,
    # process-wide os module, or pathlib picks a platform Path class it
    # cannot instantiate on this host (see cpython pathlib flavour caching).
    monkeypatch.setattr(store_module, "os", _FakeOS())
    assert "Windows limits paths" in store_module._path_hint(Path("x" * 240))
    assert store_module._path_hint(Path("x" * 100)) == ""

    # Regression: the simulation above must not have poisoned real platform
    # state. The process's actual os.name is untouched, and constructing a
    # native Path on this host still works.
    assert real_os.name in ("nt", "posix")
    Path("still-constructible-on-this-platform")
