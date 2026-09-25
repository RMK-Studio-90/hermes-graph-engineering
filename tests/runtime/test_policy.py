"""Risk policy: classification, automatic approval of safe work, operator gates for risky work,
denial of destructive work, digest-bound policy records and risk-bound worker rights."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.dispatch import AutonomousDispatcher, WorkerContext
from ge_runtime.engine import GraphEngine
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.policy import (Decision, PolicyConfig, RiskClass, apply_policy, classify_command, classify_node,
                               evaluate_plan, worker_level)
from ge_runtime.spec import admit
from ge_runtime.store import RunStore


def agent(node_id, purpose, deps=(), **over):
    node = {"id": node_id, "purpose": purpose, "executor": "agent", "depends_on": list(deps),
            "inputs": {"task": {"from": "graph.inputs.task", "type": "string"}},
            "outputs": {"result": {"type": "string"}},
            "success_criteria": [{"output": "result", "check": "non_empty"}]}
    node.update(over)
    return node


def graph(*nodes, graph_id="policy"):
    return {"schema_version": 1, "graph_id": graph_id, "title": graph_id, "inputs": {"task": "t"},
            "nodes": list(nodes)}


# purpose text -> expected risk; the policy test matrix of the final report
MATRIX = [
    ("Summarize the findings in three sentences", {}, "READ_ONLY", "AUTO_APPROVE"),
    ("Review the plan and decide on the approach", {}, "READ_ONLY", "AUTO_APPROVE"),
    ("Implement the parser in src/parser.py", {"requires": ["agent.code"]}, "WORKSPACE_WRITE", "AUTO_APPROVE"),
    ("Write the report to out/report.md", {}, "WORKSPACE_WRITE", "AUTO_APPROVE"),
    ("Run the unit tests with pytest", {}, "LOCAL_EXEC", "AUTO_APPROVE"),
    ("Download the dataset from https://example.org/data.csv", {}, "NETWORK", "REQUIRE_APPROVAL"),
    ("Research current prices", {"requires": ["agent.research"]}, "NETWORK", "REQUIRE_APPROVAL"),
    ("Deploy the service to production", {}, "EXTERNAL_SIDE_EFFECT", "REQUIRE_APPROVAL"),
    ("Send an email to the customer", {}, "EXTERNAL_SIDE_EFFECT", "REQUIRE_APPROVAL"),
    ("git push the branch to origin", {}, "EXTERNAL_SIDE_EFFECT", "REQUIRE_APPROVAL"),
    ("Purchase a license for the tool", {}, "PAID", "REQUIRE_APPROVAL"),
    ("Delete all files in the build directory with rm -rf build", {}, "DESTRUCTIVE", "DENY"),
    ("Drop table users in the database", {}, "DESTRUCTIVE", "DENY"),
    ("Lösche die alten Backups", {}, "DESTRUCTIVE", "DENY"),
]


@pytest.mark.parametrize("purpose,over,risk,decision", MATRIX)
def test_policy_matrix(purpose, over, risk, decision):
    spec = admit(graph(agent("n", purpose, **over)))
    record = evaluate_plan(spec, PolicyConfig())
    assert (record["decisions"]["n"]["risk"], record["decisions"]["n"]["decision"]) == (risk, decision), \
        record["decisions"]["n"]["reasons"]


def test_builtin_nodes_are_read_only():
    node = {"id": "b", "purpose": "delete everything", "executor": "builtin", "operation": {"op": "identity"},
            "requires": ["compute.pure"]}
    assert classify_node(node)["risk"] == "READ_ONLY"  # pure operations cannot do what their text says


def test_self_declarations_can_raise_but_never_lower_the_risk():
    lying = classify_node(agent("x", "Deploy the new release to production", side_effects=False, risk="READ_ONLY"))
    assert lying["risk"] == "EXTERNAL_SIDE_EFFECT"
    assert any("ignored" in r for r in lying["reasons"]) and any("not trusted" in r for r in lying["reasons"])
    cautious = classify_node(agent("y", "Summarize", risk="PAID"))
    assert cautious["risk"] == "PAID"
    declared_effects = classify_node(agent("z", "Summarize", side_effects=True))
    assert declared_effects["risk"] == "EXTERNAL_SIDE_EFFECT"


def test_evidence_commands_count_towards_the_risk():
    assert classify_command(["python", "-m", "pytest"])[0] is RiskClass.LOCAL_EXEC
    assert classify_command(["curl", "https://example.org"])[0] is RiskClass.NETWORK
    assert classify_command(["rm", "-rf", "/tmp/x"])[0] is RiskClass.DESTRUCTIVE
    node = agent("n", "Summarize", evidence=[{"type": "command", "argv": ["git", "push", "origin"]}])
    assert classify_node(admit(graph(node)).node("n"))["risk"] == "EXTERNAL_SIDE_EFFECT"


def test_configuration_moves_the_auto_approval_ceiling():
    strict = PolicyConfig(auto_approve_max=RiskClass.READ_ONLY)
    assert strict.decide(RiskClass.WORKSPACE_WRITE) is Decision.REQUIRE_APPROVAL
    permissive = PolicyConfig(auto_approve_max=RiskClass.NETWORK, deny=frozenset())
    assert permissive.decide(RiskClass.NETWORK) is Decision.AUTO_APPROVE
    assert permissive.decide(RiskClass.DESTRUCTIVE) is Decision.REQUIRE_APPROVAL
    assert PolicyConfig.from_dict(PolicyConfig().to_dict()) == PolicyConfig()


# ------------------------------------------------------------------ applied to runs
class Worker:
    def __init__(self):
        self.calls = []

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        self.calls.append((order["node"], context))
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"result": "done " + order["node"]})


@pytest.fixture
def engine(tmp_path):
    return GraphEngine(RunStore(tmp_path))


def drive(engine, run_id, worker):
    return AutonomousDispatcher(engine, worker, state_key="k").drive(run_id)


def test_a_safe_plan_is_approved_by_policy_and_runs_without_an_operator(engine):
    run_id = engine.create(graph(agent("a", "Summarize the text"), agent("b", "Write out/summary.md", ["a"])),
                           created_by="t")["run_id"]
    record = apply_policy(engine, run_id, PolicyConfig())
    state = engine.load(run_id)[0]
    plan = state["approvals"]["plan"]
    assert state["status"] == "APPROVED" and plan["decided_by"] == "policy:auto-approval"
    assert plan["basis"]["policy_digest"] == record["digest"]
    engine.execute(run_id)
    worker = Worker()
    drive(engine, run_id, worker)
    assert [n for n, _c in worker.calls] == ["a", "b"]
    receipt = engine.store.read_receipt(run_id)
    checks = {c["check"]: c for c in receipt["checks"]}
    assert receipt["verified"] and checks["policy_decisions"]["passed"] and checks["plan_approval"]["passed"]
    assert "policy.evaluated" in [e["type"] for e in engine.store.read_trace(run_id)]


def test_a_risky_node_holds_for_the_operator_behind_a_digest_bound_policy_gate(engine):
    run_id = engine.create(graph(agent("a", "Summarize the release notes"),
                                 agent("b", "Deploy the service to production", ["a"])), created_by="t")["run_id"]
    apply_policy(engine, run_id, PolicyConfig())
    engine.execute(run_id)
    worker = Worker()
    report = drive(engine, run_id, worker)
    state = engine.load(run_id)[0]
    assert [n for n, _c in worker.calls] == ["a"] and report["stopped"] == "no_pending_work"
    assert state["holds"] == [{"kind": "WAITING_FOR_APPROVAL", "node": "b", "gate": "policy"}]
    with pytest.raises(GraphEngineeringError):
        engine.decide(run_id, "b:other", approve=True, decided_by="operator")
    engine.decide(run_id, "b:policy", approve=True, decided_by="operator")
    drive(engine, run_id, worker)
    assert [n for n, _c in worker.calls] == ["a", "b"]
    assert worker.calls[1][1].risk == "EXTERNAL_SIDE_EFFECT"
    assert engine.store.read_receipt(run_id)["verified"] is True


def test_a_destructive_plan_is_rejected_by_policy_and_nothing_runs(engine):
    run_id = engine.create(graph(agent("a", "Summarize"), agent("b", "rm -rf the build directory", ["a"])),
                           created_by="t")["run_id"]
    record = apply_policy(engine, run_id, PolicyConfig())
    state = engine.load(run_id)[0]
    assert record["plan_decision"] == "DENY"
    assert state["status"] == "CANCELLED" and state["terminal_outcome"] == "POLICY_DENIED"
    assert state["failure"]["code"] == "POLICY_DENIED" and "b" in state["failure"]["message"]
    assert all(n["attempts"] == 0 for n in state["nodes"].values())
    receipt = engine.store.read_receipt(run_id)
    assert receipt["terminal_outcome"] == "POLICY_DENIED" and receipt["verified"] is False


def test_an_operator_approved_plan_still_never_executes_a_denied_node(engine):
    run_id = engine.create(graph(agent("a", "Drop table users")), created_by="t")["run_id"]
    with engine.mutate(run_id) as (state, spec):
        state["policy"] = evaluate_plan(spec, PolicyConfig())
        engine._save(state)
    engine.decide(run_id, "plan", approve=True, decided_by="operator")
    state = engine.execute(run_id)
    assert state["status"] == "FAILED" and state["nodes"]["a"]["attempts"] == 0
    assert state["nodes"]["a"]["last_error"]["code"] == "POLICY_DENIED"


def test_a_tampered_policy_record_fails_verification(engine):
    run_id = engine.create(graph(agent("a", "Deploy to production")), created_by="t")["run_id"]
    apply_policy(engine, run_id, PolicyConfig())
    with engine.mutate(run_id) as (state, spec):
        state["policy"]["decisions"]["a"]["decision"] = "AUTO_APPROVE"  # forge the decision
        engine._save(state)
    receipt = engine.verify(run_id)
    check = next(c for c in receipt["checks"] if c["check"] == "policy_decisions")
    assert check["passed"] is False and "tampered" in check["detail"]


def test_a_policy_approval_does_not_transfer_to_a_revised_plan(engine):
    run_id = engine.create(graph(agent("a", "Summarize")), created_by="t")["run_id"]
    apply_policy(engine, run_id, PolicyConfig())
    state = engine.revise(run_id, graph(agent("a", "Summarize"), agent("b", "Deploy it", ["a"])),
                          reason="scope grew", decided_by="autopilot")
    assert state["status"] == "DRAFT" and "plan" not in state["approvals"]
    record = apply_policy(engine, run_id, PolicyConfig())
    state = engine.load(run_id)[0]
    assert state["approvals"]["plan"]["basis"]["policy_digest"] == record["digest"]
    assert record["decisions"]["b"]["decision"] == "REQUIRE_APPROVAL"


# ------------------------------------------------------------------ worker rights
def test_worker_rights_follow_the_risk_class():
    from ge_hermes.host import toolsets_for_risk

    assert toolsets_for_risk("READ_ONLY") == ("file",)
    assert toolsets_for_risk("LOCAL_EXEC") == ("file", "terminal")
    assert toolsets_for_risk("NETWORK") == ("file", "terminal", "web")
    assert toolsets_for_risk("EXTERNAL_SIDE_EFFECT") is None  # operator-approved: host permissions
    assert worker_level(None) is RiskClass.READ_ONLY  # unknown risk: least privilege


def context(risk, correlation="ge:r:a:1:1"):
    from ge_hermes.host import toolsets_for_risk

    return WorkerContext(run_id="r", graph_id="g", node_id="a", attempt=1, attempt_id="a:1", execution_id="r:a:1",
                         correlation_id=correlation, goal="goal", context="ctx", timeout_seconds=60, risk=risk,
                         allowed_toolsets=toolsets_for_risk(risk), context_digest="sha256:x")


def test_the_tool_guard_blocks_calls_beyond_the_risk_class():
    from ge_hermes.host import WorkerRegistry, WORKER_MARKER

    registry = WorkerRegistry()
    registry.expect(context("WORKSPACE_WRITE"))
    registry.on_subagent_start(child_goal="goal\n\n" + WORKER_MARKER + "ge:r:a:1:1", child_session_id="child-1",
                               parent_session_id="parent", child_subagent_id="sa-1")
    assert registry.on_pre_tool_call(tool_name="write_file", args={}, session_id="child-1") is None
    blocked = registry.on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf /"}, session_id="child-1")
    assert blocked["action"] == "block" and "WORKSPACE_WRITE" in blocked["message"]
    assert registry.on_pre_tool_call(tool_name="ge_graph", args={"action": "run"}, session_id="child-1")["action"] \
        == "block"
    assert registry.on_pre_tool_call(tool_name="ge_graph", args={"action": "status"}, session_id="child-1") is None
    assert registry.on_pre_tool_call(tool_name="delegate_task", args={}, session_id="child-1")["action"] == "block"
    assert registry.on_pre_tool_call(tool_name="terminal", args={}, session_id="parent") is None  # not a worker
    info = registry.forget("ge:r:a:1:1")
    assert info["blocked_calls"] == ["terminal", "ge_graph", "delegate_task"]
    assert info["child_session_id"] == "child-1" and info["parent_session_id"] == "parent"


def test_launch_narrows_toolsets_and_falls_back_to_the_guard_when_the_host_refuses(monkeypatch):
    import types as pytypes

    from ge_hermes.host import HermesHostExecutor, WorkerRegistry

    class LaunchError(ValueError):
        pass

    requests = []

    class Lifecycle:
        refuse = True

        def launch(self, request):
            requests.append(request)
            if request.allowed_toolsets and self.refuse:
                raise LaunchError("Requested toolsets would broaden parent permissions.")
            return SimpleNamespace(subagent_id="sa-9")

        def wait(self, handle, timeout_seconds=None):
            return SimpleNamespace(completed=True)

        def cancel(self, handle, reason):
            pass

        def result(self, handle):
            return SimpleNamespace(terminal_state="SUCCEEDED", summary='```json\n{"result": "x"}\n```',
                                   structured_payload=None)

    module = pytypes.ModuleType("agent.subagent_lifecycle")
    module.SubagentLaunchRequest = lambda **kw: SimpleNamespace(**kw)
    module.SubagentLifecycleError = LaunchError
    monkeypatch.setitem(sys.modules, "agent", pytypes.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", module)
    lifecycle = Lifecycle()
    executor = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=lifecycle), WorkerRegistry())
    order = {"node": "a", "outputs": {"result": {"type": "string"}}}
    ctx = context("LOCAL_EXEC")
    assert executor.execute_agent_work(order, ctx).outcome is ExecutorOutcome.SUCCESS
    assert [r.allowed_toolsets for r in requests] == [("file", "terminal"), None]
    info = executor.worker_info(ctx)
    assert info["restriction"].startswith("tool_guard (host refused") and info["level"] == "LOCAL_EXEC"
    lifecycle.refuse = False
    requests.clear()
    ctx2 = context("READ_ONLY", correlation="ge:r:a:1:2")
    executor.execute_agent_work(order, ctx2)
    assert [r.allowed_toolsets for r in requests] == [("file",)]
    assert executor.worker_info(ctx2)["restriction"] == "toolsets+tool_guard"
