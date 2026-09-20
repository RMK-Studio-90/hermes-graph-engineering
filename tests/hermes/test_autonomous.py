"""Autonomous agent execution: generic dispatcher, host abstraction, guards and the Hermes executor.

No test needs a model, a provider or a network. The dispatcher is exercised with a fake generic
host; ``HermesHostExecutor`` is exercised against a faithful fake of the documented public
subagent lifecycle API.
"""
import ast
import contextvars
import json
import sys
import threading
import types
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

import ge_hermes
from ge_hermes.commands import CommandSet
from ge_hermes.config import parse_config
from ge_hermes.dispatch import (AutonomousDispatcher, HostAgentExecutor, WorkerContext, build_worker_context,
                                extract_outputs)
from ge_hermes.guard import READ_ONLY_ACTIONS, WorkerScope, current_worker, exclusive_dispatch, worker_scope
from ge_hermes.host import HermesHostExecutor
from ge_hermes.service import GraphService
from ge_hermes.tool import make_tool_handler
from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.errors import ErrorCode, GraphEngineeringError

# ------------------------------------------------------------------------------ helpers
def node(node_id, deps=(), **over):
    spec = {
        "id": node_id, "name": node_id, "purpose": "do %s" % node_id, "depends_on": list(deps), "executor": "agent",
        "requires": ["agent.reasoning"], "operation": None, "instructions": "Work on %s." % node_id,
        "inputs": {"task": {"from": "graph.inputs.task", "type": "string", "required": True}},
        "outputs": {"result": {"type": "string", "required": True, "description": "result"}},
        "success_criteria": [{"output": "result", "check": "non_empty"}], "on_failure": "stop",
        "retry": {"max_attempts": 1}, "idempotent": False, "side_effects": False, "gates": [], "owner": "agent",
        "rollback_boundary": None,
    }
    spec.update(over)
    return spec


def chain(node_id, previous):
    return node(node_id, [previous], inputs={
        "task": {"from": "graph.inputs.task", "type": "string", "required": True},
        "previous": {"from": "%s.result" % previous, "type": "string", "required": True}})


def graph(*nodes):
    return {"schema_version": 1, "graph_id": "auto-test", "title": "auto test", "description": "t",
            "inputs": {"task": "go"}, "nodes": list(nodes)}


def gate(kind):
    return {"id": kind, "type": kind, "owner": "operator", "description": "test gate"}


def ok(outputs):
    return ExecutorResult(ExecutorOutcome.SUCCESS, outputs=outputs)


def respond_ok(order, context):
    outputs = {"result": "out-" + order["node"]}
    if "passed" in order["outputs"]:
        outputs["passed"] = True
    return ok(outputs)


class FakeHost:
    """A generic host: no provider, model, endpoint or credential anywhere."""

    def __init__(self, respond=respond_ok, supported=True):
        self.calls, self.respond, self.supported = [], respond, supported

    def supports_agent_execution(self):
        return self.supported

    def execute_agent_work(self, work_order, context):
        self.calls.append((dict(work_order), context))
        return self.respond(work_order, context)

    @property
    def nodes(self):
        return [order["node"] for order, _ctx in self.calls]


def make_service(tmp_path, host, **settings):
    values = {"require_plan_approval": False, "autonomous_agent_execution": True}
    values.update(settings)
    return GraphService(tmp_path, lambda key, default=None: values.get(key, default), host_executor=host)


def create(service, *nodes):
    return service.engine().create(graph(*nodes), created_by="test")["run_id"]


def tool_for(service):
    handler = make_tool_handler(service)
    return lambda **args: json.loads(handler(args))


def status_of(service, run_id, node_id=None):
    state = service.engine().load(run_id)[0]
    return state["nodes"][node_id]["status"] if node_id else state["status"]


def trace_types(service, run_id):
    return [e["type"] for e in service.engine().store.read_trace(run_id)]


# ------------------------------------------------------------------------------ config
def test_config_default_is_manual_and_invalid_values_fail_closed():
    cfg = parse_config(lambda key, default=None: default)
    assert cfg.autonomous_agent_execution is False and cfg.autonomous_node_timeout_seconds == 3600
    assert cfg.autonomous_call_budget_seconds == 300
    for key, value in (("autonomous_agent_execution", "yes"), ("autonomous_node_timeout_seconds", 5),
                       ("autonomous_node_timeout_seconds", True), ("autonomous_node_timeout_seconds", 90_000),
                       ("autonomous_call_budget_seconds", 5), ("autonomous_call_budget_seconds", "300")):
        with pytest.raises(GraphEngineeringError) as info:
            parse_config(lambda k, default=None, key=key, value=value: value if k == key else default)
        assert info.value.code is ErrorCode.PLUGIN_CONFIGURATION_INVALID and key in info.value.message


# ------------------------------------------------------------------------------ A: manual mode
def test_manual_mode_waits_for_submission_and_never_calls_the_host(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host, autonomous_agent_execution=False)
    run_id = create(service, node("a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert host.calls == [] and "autonomous" not in result
    assert result["status"]["holds"] == [{"kind": "WAITING_FOR_SUBMISSION", "node": "a", "gate": None}]
    done = tool_for(service)(action="submit", run_id=run_id, node="a", outputs={"result": "manual"})
    assert done["status"]["status"] == "SUCCEEDED"


# ------------------------------------------------------------------------------ B / D: dispatch + sequence
def test_autonomous_dispatch_submits_and_verifies(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert result["status"]["status"] == "SUCCEEDED" and host.nodes == ["a"]
    assert result["autonomous"]["stopped"] == "run_finished"
    assert result["autonomous"]["dispatched"] == [
        {"node": "a", "attempt": 1, "outcome": "SUCCESS", "submitted": True}]
    events = service.engine().store.read_trace(run_id)
    assert [e["type"] for e in events if e["type"].startswith("node.")] == [
        "node.started", "node.awaiting_submission", "node.dispatched", "node.submitted", "node.succeeded"]
    assert next(e for e in events if e["type"] == "node.submitted")["submitted_by"] == "agent:host-autonomous"
    assert service.engine().verify(run_id)["verified"] is True


def test_sequential_chain_executes_in_dependency_order_one_at_a_time(tmp_path):
    active, overlap = [], []

    def respond(order, context):
        active.append(order["node"])
        overlap.append(len(active))
        outputs = respond_ok(order, context).outputs
        active.pop()
        return ok(outputs)

    host = FakeHost(respond)
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"), chain("b", "a"), chain("c", "b"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert host.nodes == ["a", "b", "c"] and max(overlap) == 1
    assert result["status"]["status"] == "SUCCEEDED"
    assert host.calls[2][0]["inputs"]["previous"] == "out-b"  # b's outputs reached c through the engine
    # parallel layers are still planned, just not exploited
    fan = create(service, node("x"), node("y"))
    tool_for(service)(action="run", run_id=fan)
    assert host.nodes[3:] == ["x", "y"]


# ------------------------------------------------------------------------------ C: no provider knowledge
def test_no_provider_model_endpoint_or_credential_anywhere(tmp_path, src_dir):
    host = FakeHost()
    service = make_service(tmp_path, host)  # configuration carries nothing but generic settings
    run_id = create(service, node("a"))
    tool_for(service)(action="run", run_id=run_id)
    order, context = host.calls[0]
    assert {f for f in vars(context)} == {
        "run_id", "graph_id", "node_id", "attempt", "attempt_id", "execution_id", "correlation_id", "goal",
        "context", "timeout_seconds"}
    text = json.dumps([order, vars(context)]).lower()
    for word in ("provider", "model", "endpoint", "api_key", "apikey", "base_url"):
        assert word not in text, word
    banned = ("omniroute", "openrouter", "lmstudio", "lm studio", "freellmapi", "nararouter", "anthropic", "claude",
              "openai", "nvidia", "projectatlas", "openviking", "rmk")
    for package in ("ge_hermes", "ge_runtime"):
        for path in (src_dir / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8").lower()
            assert not [w for w in banned if w in source], (path.name, [w for w in banned if w in source])
    tree = ast.parse((src_dir / "ge_hermes" / "host.py").read_text(encoding="utf-8"))
    launch = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SubagentLaunchRequest"]
    assert len(launch) == 1
    assert not {k.arg for k in launch[0].keywords} & {"model", "allowed_toolsets", "blocked_tools", "working_directory"}


def test_task_text_is_bounded_guidance_about_this_node_only(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a", purpose="Summarise the input", requires=["agent.research", "agent.tools"]),
                    chain("b", "a"))
    tool_for(service)(action="run", run_id=run_id)
    _order, context = host.calls[0]
    for needle in ("one node from an approved Graph Engineering run", "Do not modify the graph, approve gates",
                   "Summarise the input", "Work on a.", "agent.research, agent.tools", "- result: string",
                   '"check": "non_empty"', "```json"):
        assert needle in context.goal, needle
    assert '"task": "go"' in context.context
    assert "Work on b." not in context.goal  # other nodes are not leaked
    assert context.correlation_id == "ge:%s:a:1:1" % run_id and context.attempt_id == "a:1"
    oversized = dict(host.calls[0][0], inputs={"blob": "x" * 40_000})
    with pytest.raises(GraphEngineeringError):
        build_worker_context({"graph_id": "g"}, oversized, dispatch=1, timeout_seconds=10)


# ------------------------------------------------------------------------------ E / F / G: governance
def test_plan_gate_cannot_be_bypassed(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host, require_plan_approval=True)
    run_id = create(service, node("a"))
    assert tool_for(service)(action="run", run_id=run_id)["error"] == "GATE_FAILED"
    report = AutonomousDispatcher(service.engine(), host, state_key="k").drive(run_id)
    assert report["stopped"] == "plan_not_approved" and host.calls == []
    assert status_of(service, run_id) == "DRAFT"


def test_approval_gate_stops_the_dispatcher_until_the_operator_approves(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"), dict(chain("b", "a"), gates=[gate("approval")]), chain("c", "b"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert host.nodes == ["a"] and result["autonomous"]["stopped"] == "no_pending_work"
    assert result["status"]["holds"] == [{"kind": "WAITING_FOR_APPROVAL", "node": "b", "gate": "approval"}]
    assert "b:approval" not in service.engine().load(run_id)[0]["approvals"]  # the worker never decides
    service.engine().decide(run_id, "b:approval", approve=True, decided_by="operator")
    result = tool_for(service)(action="run", run_id=run_id)
    assert host.nodes == ["a", "b", "c"] and result["status"]["status"] == "SUCCEEDED"
    assert service.engine().verify(run_id)["verified"] is True


def test_review_gate_stops_after_submission_until_reviewed(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a", gates=[gate("review")]), chain("b", "a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert host.nodes == ["a"]
    assert result["status"]["holds"] == [{"kind": "WAITING_FOR_REVIEW", "node": "a", "gate": "review"}]
    assert status_of(service, run_id, "a") == "RUNNING" and status_of(service, run_id, "b") == "PENDING"
    service.engine().decide(run_id, "a:review", approve=True, decided_by="operator")
    tool_for(service)(action="run", run_id=run_id)
    assert host.nodes == ["a", "b"] and status_of(service, run_id) == "SUCCEEDED"


def test_dispatcher_only_reads_and_submits(tmp_path):
    class Spy:
        def __init__(self, engine):
            self._engine, self.used = engine, set()

        def __getattr__(self, name):
            attr = getattr(self._engine, name)
            if callable(attr):
                self.used.add(name)
            return attr

    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"), chain("b", "a"))
    engine = service.engine()
    engine.execute(run_id)
    spy = Spy(engine)
    AutonomousDispatcher(spy, host, state_key="k").drive(run_id)
    assert spy.used == {"load", "submit"}  # no decide / retry_node / cancel / execute / resume / create
    assert status_of(service, run_id) == "SUCCEEDED"


def test_needs_attention_hold_is_respected_and_replay_needs_the_operator(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"))
    engine = service.engine()
    engine.execute(run_id)  # a: RUNNING + waiting for submission
    state = engine.store.load(run_id)
    state["nodes"]["a"]["wait"] = None  # simulate a process that died mid-node (no submission wait recorded)
    engine.store.save(run_id, state)
    resumed = tool_for(service)(action="resume", run_id=run_id)
    assert resumed["recovered"] == [{"node": "a", "action": "held_for_operator"}]
    assert resumed["status"]["holds"][0]["kind"] == "NEEDS_ATTENTION" and "autonomous" not in resumed
    assert host.calls == []
    engine.retry_node(run_id, "a", decided_by="operator")  # only the operator can authorize the replay
    assert tool_for(service)(action="run", run_id=run_id)["autonomous"]["dispatched"][0]["node"] == "a"
    assert status_of(service, run_id) == "SUCCEEDED"


# ------------------------------------------------------------------------------ H: contracts
@pytest.mark.parametrize("outputs, fragment", [
    ({"result": 5}, "output contract violated"),
    ({"other": "x"}, "output contract violated"),
    ({"result": "x", "extra": 1}, "output contract violated"),
    ({"result": "   "}, "success criteria failed"),
])
def test_engine_stays_the_authority_on_contracts_and_criteria(tmp_path, outputs, fragment):
    host = FakeHost(lambda order, context: ok(outputs))
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"), chain("b", "a"))
    result = tool_for(service)(action="run", run_id=run_id)
    state = service.engine().load(run_id)[0]
    assert state["status"] == "FAILED" and state["nodes"]["a"]["status"] == "FAILED"
    assert fragment in state["nodes"]["a"]["last_error"]["message"]
    assert state["nodes"]["b"]["status"] in ("SKIPPED", "BLOCKED") and host.nodes == ["a"]
    assert result["autonomous"]["dispatched"][0]["submitted"] is True  # the host said "success"; the engine said no
    assert service.engine().verify(run_id)["verified"] is False


@pytest.mark.parametrize("outcome", [ExecutorOutcome.RETRYABLE_FAILURE, ExecutorOutcome.NON_RETRYABLE_FAILURE,
                                      ExecutorOutcome.TIMEOUT])
def test_host_failures_become_failed_submissions_without_provider_branches(tmp_path, outcome):
    host = FakeHost(lambda order, context: ExecutorResult(outcome, detail="host execution failed (whatever)"))
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert status_of(service, run_id, "a") == "FAILED"
    assert result["autonomous"]["dispatched"][0]["error"] == "HOST_EXECUTION_FAILED"


def test_host_exception_fails_only_the_attempt_and_retries_follow_the_node_policy(tmp_path):
    attempts = []

    def respond(order, context):
        attempts.append(order["attempt"])
        if len(attempts) == 1:
            raise RuntimeError("boom with details that must not leak into state")
        return respond_ok(order, context)

    host = FakeHost(respond)
    service = make_service(tmp_path, host)
    run_id = create(service, node("a", idempotent=True, retry={"max_attempts": 2}))
    result = tool_for(service)(action="run", run_id=run_id)
    assert attempts == [1, 2] and result["status"]["status"] == "SUCCEEDED"
    history = service.engine().load(run_id)[0]["nodes"]["a"]["history"]
    assert history[0]["detail"] == "host execution failed (RuntimeError)" and "boom" not in json.dumps(history)


def test_side_effect_nodes_are_never_retried_after_a_host_failure(tmp_path):
    host = FakeHost(lambda order, context: ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="failed"))
    service = make_service(tmp_path, host)
    run_id = create(service, node("a", side_effects=True, gates=[gate("approval")], rollback_boundary="b1"))
    service.engine().execute(run_id)
    service.engine().decide(run_id, "a:approval", approve=True, decided_by="operator")
    tool_for(service)(action="run", run_id=run_id)
    assert len(host.calls) == 1 and status_of(service, run_id, "a") == "FAILED"


# ------------------------------------------------------------------------------ I: host unavailable
@pytest.mark.parametrize("make_host", [
    lambda: None,
    lambda: FakeHost(supported=False),
    lambda: FakeHost(lambda o, c: ExecutorResult(ExecutorOutcome.EXECUTOR_UNAVAILABLE, detail="nothing started")),
])
def test_host_unavailable_is_explicit_and_manual_mode_still_works(tmp_path, make_host):
    service = make_service(tmp_path, make_host())
    run_id = create(service, node("a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert result["ok"] is True and result["autonomous"]["error"] == "HOST_EXECUTION_UNAVAILABLE"
    assert result["autonomous"]["stopped"] == "host_unavailable"
    assert result["status"]["holds"] == [{"kind": "WAITING_FOR_SUBMISSION", "node": "a", "gate": None}]
    handlers = {name: handler for name, handler, _description, _hint in CommandSet(service).handlers()}
    assert "HOST_EXECUTION_UNAVAILABLE" in handlers["ge-run"](run_id)
    done = tool_for(service)(action="submit", run_id=run_id, node="a", outputs={"result": "by hand"})
    assert done["status"]["status"] == "SUCCEEDED"


def test_unavailable_host_releases_the_claim_so_a_later_dispatch_is_not_treated_as_a_replay(tmp_path):
    state = {"available": False}

    def respond(order, context):
        if not state["available"]:
            return ExecutorResult(ExecutorOutcome.EXECUTOR_UNAVAILABLE, detail="no session")
        return respond_ok(order, context)

    host = FakeHost(respond)
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"))  # not idempotent: an unreleased claim would block it
    tool_for(service)(action="run", run_id=run_id)
    assert trace_types(service, run_id)[-2:] == ["node.dispatched", "node.dispatch_released"]
    state["available"] = True
    tool_for(service)(action="resume", run_id=run_id)
    assert status_of(service, run_id) == "SUCCEEDED"


def test_a_host_that_stops_midway_leaves_the_node_waiting(tmp_path):
    host = FakeHost(lambda o, c: ExecutorResult(ExecutorOutcome.CANCELLED, detail="host worker was cancelled"))
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert result["autonomous"]["stopped"] == "host_declined" and result["autonomous"]["dispatched"][0]["submitted"] is False
    assert result["status"]["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION"


# ------------------------------------------------------------------------------ J: recursion + lease
def test_worker_context_blocks_every_state_changing_action_but_not_reads(tmp_path):
    service = make_service(tmp_path, FakeHost())
    run_id = create(service, node("a"))
    call = tool_for(service)
    with worker_scope(WorkerScope(run_id, "a")):
        for action, extra in (("analyze", {"task": "1. x"}), ("create", {"template": "text-pipeline"}),
                              ("run", {"run_id": run_id}), ("resume", {"run_id": run_id}),
                              ("submit", {"run_id": run_id, "node": "a", "outputs": {}}),
                              ("verify", {"run_id": run_id})):
            refused = call(action=action, **extra)
            assert refused["error"] == "WORKER_CONTEXT_RESTRICTED" and refused["details"]["node"] == "a", action
        assert set(READ_ONLY_ACTIONS) == {"validate", "plan", "status", "work", "explain", "list"}
        assert call(action="status", run_id=run_id)["ok"] and call(action="work", run_id=run_id)["ok"]
        assert call(action="list")["ok"] and call(action="plan", run_id=run_id)["ok"]
    assert len(service.engine().list_runs()) == 1  # nothing was created by the refused calls
    assert call(action="run", run_id=run_id)["ok"] is True  # guard is scoped: normal sessions are unaffected


def test_a_worker_cannot_start_or_drive_graphs_even_from_a_copied_thread_context(tmp_path):
    seen = {}
    service = make_service(tmp_path, None)
    call = tool_for(service)

    def respond(order, context):
        def in_worker_thread():
            seen["run"] = call(action="run", run_id=context.run_id)
            seen["analyze"] = call(action="analyze", task="1. recurse")
            seen["submit"] = call(action="submit", run_id=context.run_id, node=context.node_id,
                                  outputs={"result": "forged"})

        copied = contextvars.copy_context()  # what the host's executor does when it starts a worker
        thread = threading.Thread(target=lambda: copied.run(in_worker_thread))
        thread.start()
        thread.join()
        return respond_ok(order, context)

    service.host_executor = FakeHost(respond)
    run_id = create(service, node("a"))
    result = call(action="run", run_id=run_id)
    assert {k: v["error"] for k, v in seen.items()} == {k: "WORKER_CONTEXT_RESTRICTED" for k in seen} and len(seen) == 3
    assert result["status"]["status"] == "SUCCEEDED" and len(service.engine().list_runs()) == 1
    assert service.engine().load(run_id)[0]["nodes"]["a"]["outputs"] == {"result": "out-a"}  # not the forged one


def test_the_same_run_cannot_be_dispatched_twice_concurrently(tmp_path):
    seen = {}
    service = make_service(tmp_path, None)
    key = str(tmp_path.resolve())

    def respond(order, context):
        rival = AutonomousDispatcher(service.engine(), FakeHost(), state_key=key)
        thread = threading.Thread(target=lambda: seen.update(rival.drive(context.run_id)))  # fresh context: no guard
        thread.start()
        thread.join()
        return respond_ok(order, context)

    service.host_executor = FakeHost(respond)
    run_id = create(service, node("a"))
    tool_for(service)(action="run", run_id=run_id)
    assert seen["stopped"] == "dispatch_in_progress" and seen["error"] == "DISPATCH_IN_PROGRESS"
    assert status_of(service, run_id) == "SUCCEEDED" and len(service.host_executor.calls) == 1


def test_protection_holds_on_a_host_that_does_not_propagate_thread_context(tmp_path):
    """Older hosts start workers in threads without the caller's context: the context marker is then absent."""
    seen = {}
    service = make_service(tmp_path, None)
    call = tool_for(service)
    handlers = {name: handler for name, handler, _description, _hint in CommandSet(service).handlers()}
    other = create(service, node("other"))

    def respond(order, context):
        def in_plain_thread():  # no copy_context(): current_worker() is None here
            assert current_worker() is None
            for action in ("run", "resume", "verify"):
                seen[action] = call(action=action, run_id=context.run_id).get("error")
            seen["submit"] = call(action="submit", run_id=context.run_id, node=context.node_id,
                                  outputs={"result": "forged"}).get("error")
            seen["other"] = call(action="run", run_id=other)  # another approved run: progresses, is not dispatched
            seen["operator"] = handlers["ge-status"](context.run_id)  # operator commands stay available

        thread = threading.Thread(target=in_plain_thread)
        thread.start()
        thread.join()
        return respond_ok(order, context)

    service.host_executor = FakeHost(respond)
    run_id = create(service, node("a"))
    result = call(action="run", run_id=run_id)
    assert {seen[a] for a in ("run", "resume", "verify", "submit")} == {"DISPATCH_IN_PROGRESS"}
    assert seen["other"]["autonomous"]["stopped"] == "dispatch_in_progress"  # no nested dispatch
    assert seen["other"]["status"]["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION"
    assert seen["operator"].startswith("Run %s" % run_id)
    assert result["status"]["status"] == "SUCCEEDED"
    assert service.engine().load(run_id)[0]["nodes"]["a"]["outputs"] == {"result": "out-a"}
    assert [order["node"] for order, _ctx in service.host_executor.calls] == ["a"]


def test_a_worker_context_cannot_drive_even_directly(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"))
    service.engine().execute(run_id)
    with worker_scope(WorkerScope(run_id, "a")):
        report = AutonomousDispatcher(service.engine(), host, state_key="k").drive(run_id)
    assert report["stopped"] == "worker_context" and host.calls == []


# ------------------------------------------------------------------------------ K: recovery
def test_claim_is_durable_before_the_host_runs(tmp_path):
    observed = {}
    service = make_service(tmp_path, None)

    def respond(order, context):
        state = service.engine().load(context.run_id)[0]
        observed["node"] = (state["nodes"]["a"]["status"], state["nodes"]["a"]["wait"])  # write-ahead RUNNING
        observed["trace"] = trace_types(service, context.run_id)
        return respond_ok(order, context)

    service.host_executor = FakeHost(respond)
    run_id = create(service, node("a"))
    tool_for(service)(action="run", run_id=run_id)
    assert observed["node"] == ("RUNNING", "submission") and observed["trace"][-1] == "node.dispatched"


def test_interrupted_dispatch_of_a_non_idempotent_node_is_held_not_replayed(tmp_path):
    host = FakeHost()

    def dies(order, context):
        raise KeyboardInterrupt  # not an Exception: the process is going away

    service = make_service(tmp_path, FakeHost(dies))
    run_id = create(service, node("a"))
    with pytest.raises(KeyboardInterrupt):
        tool_for(service)(action="run", run_id=run_id)
    assert "node.dispatched" in trace_types(service, run_id) and "node.submitted" not in trace_types(service, run_id)
    assert status_of(service, run_id, "a") == "RUNNING"  # persisted: recovery rules apply as before
    service.host_executor = host  # a new process, working host
    result = tool_for(service)(action="resume", run_id=run_id)
    assert host.calls == [] and result["autonomous"]["stopped"] == "interrupted_dispatch"
    assert result["autonomous"]["error"] == "DISPATCH_INTERRUPTED"
    assert result["status"]["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION"
    done = tool_for(service)(action="submit", run_id=run_id, node="a", outputs={"result": "operator decided"})
    assert done["status"]["status"] == "SUCCEEDED"  # manual submission stays first-class


def test_interrupted_dispatch_of_an_idempotent_node_is_dispatched_again(tmp_path):
    def dies(order, context):
        raise KeyboardInterrupt

    service = make_service(tmp_path, FakeHost(dies))
    run_id = create(service, node("a", idempotent=True))
    with pytest.raises(KeyboardInterrupt):
        tool_for(service)(action="run", run_id=run_id)
    host = FakeHost()
    service.host_executor = host
    tool_for(service)(action="resume", run_id=run_id)
    assert host.calls[0][1].correlation_id.endswith(":a:1:2")  # second dispatch of the same attempt
    assert status_of(service, run_id) == "SUCCEEDED"


def test_manual_intervention_during_dispatch_is_reported_not_overwritten(tmp_path):
    service = make_service(tmp_path, None)

    def respond(order, context):
        service.engine().cancel(context.run_id, decided_by="operator")  # the operator acts meanwhile
        return respond_ok(order, context)

    service.host_executor = FakeHost(respond)
    run_id = create(service, node("a"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert result["autonomous"]["stopped"] == "state_changed" and status_of(service, run_id) == "CANCELLED"


def test_dispatch_budget_uses_max_steps_and_resume_continues(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host, max_steps=2)
    run_id = create(service, node("a"), chain("b", "a"), chain("c", "b"))
    result = tool_for(service)(action="run", run_id=run_id)
    assert host.nodes == ["a", "b"] and result["autonomous"]["stopped"] == "budget_exhausted"
    tool_for(service)(action="resume", run_id=run_id)
    assert host.nodes == ["a", "b", "c"] and status_of(service, run_id) == "SUCCEEDED"


def test_call_budget_stops_between_nodes_so_the_call_returns_before_the_host_deadline(tmp_path):
    host = FakeHost()
    service = make_service(tmp_path, host)
    run_id = create(service, node("a"), chain("b", "a"), chain("c", "b"))
    engine = service.engine()
    engine.execute(run_id)
    report = AutonomousDispatcher(engine, host, state_key="k", call_budget_seconds=0).drive(run_id)
    assert host.nodes == ["a"] and report["stopped"] == "time_budget_exhausted" and "error" not in report
    assert status_of(service, run_id, "b") == "RUNNING"  # waiting, not failed: the next call continues
    AutonomousDispatcher(engine, host, state_key="k").drive(run_id)
    assert host.nodes == ["a", "b", "c"] and status_of(service, run_id) == "SUCCEEDED"


# ------------------------------------------------------------------------------ L: profile isolation
def test_profiles_do_not_share_leases_workers_or_runs(tmp_path):
    with exclusive_dispatch(str(tmp_path / "a"), "run-1") as first:
        with exclusive_dispatch(str(tmp_path / "b"), "run-1") as other_profile:
            with exclusive_dispatch(str(tmp_path / "a"), "run-2") as same_profile:
                assert (first, other_profile, same_profile) == (True, True, False)
    with worker_scope(WorkerScope("r", "n")):
        seen = []
        thread = threading.Thread(target=lambda: seen.append(current_worker()))
        thread.start()
        thread.join()
    assert seen == [None]  # a worker marker does not leak into unrelated threads


# ------------------------------------------------------------------------------ Hermes executor
@dataclass(frozen=True)
class FakeLaunchRequest:
    goal: str
    context: str | None = None
    role: str = "leaf"
    model: str | None = None
    allowed_toolsets: tuple | None = None
    blocked_tools: tuple = ()
    working_directory: str | None = None
    parent_session_id: str | None = None
    correlation_id: str | None = None
    metadata: dict = field(default_factory=dict)
    timeout_seconds: float | None = None


class FakeLifecycleError(ValueError):
    pass


class FakeLifecycle:
    """Behaves like the documented ``PluginContext.subagent_lifecycle`` service."""

    def __init__(self, summary=None, state="SUCCEEDED", payload=None, launch_error=None, completes=True, **result):
        self.requests, self.cancelled, self.launch_error, self.completes = [], [], launch_error, completes
        self.summary, self.state, self.payload, self.result_fields = summary, state, payload, result

    def launch(self, request):
        self.requests.append(request)
        if self.launch_error:
            raise self.launch_error
        return SimpleNamespace(subagent_id="child-1")

    def wait(self, handle, *, timeout_seconds=None):
        return SimpleNamespace(completed=self.completes)

    def cancel(self, handle, *, reason):
        self.cancelled.append(reason)

    def result(self, handle):
        fields = dict(terminal_state=self.state, ready=True, summary=self.summary, structured_payload=self.payload,
                      error_classification=None, error_message=None)
        fields.update(self.result_fields)
        return SimpleNamespace(**fields)


@pytest.fixture
def lifecycle_module(monkeypatch):
    module = types.ModuleType("agent.subagent_lifecycle")
    module.SubagentLaunchRequest, module.SubagentLifecycleError = FakeLaunchRequest, FakeLifecycleError
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", module)


class FakeContext:
    """Registration surface plus, optionally, the documented ``subagent_lifecycle`` service."""

    def __init__(self, data_dir, settings=None, lifecycle=None):
        self.state = SimpleNamespace(data_dir=data_dir)
        self.settings, self.tools = dict(settings or {}), {}
        if lifecycle is not None:
            self.subagent_lifecycle = lifecycle

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        return object()

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = handler
        return object()

    def register_skill(self, name, path, description="", frontmatter=None):
        return object()


def order_for(**over):
    order = {"node": "a", "outputs": {"result": {"type": "string", "required": True}}}
    order.update(over)
    return order


def context_for(timeout=60):
    return WorkerContext(run_id="r", graph_id="g", node_id="a", attempt=1, attempt_id="a:1", execution_id="r:a:1",
                         correlation_id="ge:r:a:1:1", goal="goal", context="ctx", timeout_seconds=timeout)


REPLY = 'All done.\n```json\n{"result": "text"}\n```\n'


def test_hermes_executor_runs_a_worker_through_the_documented_api_only(lifecycle_module):
    lifecycle = FakeLifecycle(summary=REPLY)
    executor = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=lifecycle))
    assert isinstance(executor, HostAgentExecutor) and executor.supports_agent_execution()
    result = executor.execute_agent_work(order_for(), context_for())
    assert result.outcome is ExecutorOutcome.SUCCESS and result.outputs == {"result": "text"}
    request = lifecycle.requests[0]
    assert request.role == "leaf" and request.model is None and request.allowed_toolsets is None
    assert request.blocked_tools == () and request.working_directory is None and request.timeout_seconds is None
    assert request.goal == "goal" and request.correlation_id == "ge:r:a:1:1"
    assert request.metadata == {"graph_engineering": {"run_id": "r", "node": "a", "attempt": 1}}


def test_hermes_executor_prefers_native_structured_output_and_falls_back_to_text(lifecycle_module):
    native = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=FakeLifecycle(
        summary="ignored", payload={"result": "native"})))
    assert native.execute_agent_work(order_for(), context_for()).outputs == {"result": "native"}
    text_only = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=FakeLifecycle(summary=REPLY, payload=None)))
    assert text_only.execute_agent_work(order_for(), context_for()).outputs == {"result": "text"}


@pytest.mark.parametrize("summary", [None, "", "I could not do it.", "[1, 2]", '{"unrelated": true}'])
def test_hermes_executor_reports_unusable_replies_as_invalid_results(lifecycle_module, summary):
    executor = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=FakeLifecycle(summary=summary)))
    result = executor.execute_agent_work(order_for(), context_for())
    assert result.outcome is ExecutorOutcome.RETRYABLE_FAILURE
    assert result.detail.startswith("host returned invalid result:")


def test_hermes_executor_failure_mapping(lifecycle_module):
    def run(**kwargs):
        executor = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=FakeLifecycle(**kwargs)))
        return executor.execute_agent_work(order_for(), context_for())

    failed = run(state="FAILED", summary=None)
    assert failed.outcome is ExecutorOutcome.RETRYABLE_FAILURE and "host execution failed" in failed.detail
    assert run(state="CANCELLED").outcome is ExecutorOutcome.CANCELLED
    assert run(state="INTERRUPTED").outcome is ExecutorOutcome.CANCELLED
    assert run(state="UNKNOWN").outcome is ExecutorOutcome.RETRYABLE_FAILURE
    stuck = FakeLifecycle(completes=False)
    timed_out = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=stuck)).execute_agent_work(
        order_for(), context_for(timeout=0))
    assert timed_out.outcome is ExecutorOutcome.TIMEOUT and stuck.cancelled == ["Graph Engineering node timeout"]


def test_hermes_executor_launch_failures(lifecycle_module):
    def launch(error):
        lifecycle = FakeLifecycle(launch_error=error)
        return HermesHostExecutor(SimpleNamespace(subagent_lifecycle=lifecycle)).execute_agent_work(
            order_for(), context_for())

    no_session = launch(FakeLifecycleError("No active Hermes parent session is available."))
    assert no_session.outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE and "no active agent session" in no_session.detail
    rejected = launch(FakeLifecycleError("goal must be a non-empty string of at most 16000 characters."))
    assert rejected.outcome is ExecutorOutcome.NON_RETRYABLE_FAILURE and rejected.detail.startswith("host rejected")
    broken = launch(RuntimeError("could not build worker"))
    assert broken.outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE and "RuntimeError" in broken.detail


def test_hermes_executor_feature_detection_without_the_api():
    assert HermesHostExecutor(SimpleNamespace()).supports_agent_execution() is False
    assert HermesHostExecutor(SimpleNamespace(subagent_lifecycle=SimpleNamespace(launch=1))).supports_agent_execution() is False

    class Raising:
        @property
        def subagent_lifecycle(self):
            raise ImportError("no lifecycle module")

    executor = HermesHostExecutor(Raising())
    assert executor.supports_agent_execution() is False
    assert executor.execute_agent_work(order_for(), context_for()).outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE


def test_hermes_executor_without_the_lifecycle_module_is_unavailable(monkeypatch):
    monkeypatch.delitem(sys.modules, "agent.subagent_lifecycle", raising=False)
    monkeypatch.setitem(sys.modules, "agent", None)  # makes `import agent.*` raise ImportError
    executor = HermesHostExecutor(SimpleNamespace(subagent_lifecycle=FakeLifecycle(summary=REPLY)))
    assert executor.execute_agent_work(order_for(), context_for()).outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE


# ------------------------------------------------------------------------------ registration end to end
def spec_json():
    return json.dumps(graph(node("a"), chain("b", "a")))


def test_registered_plugin_runs_agent_nodes_through_the_host_service(tmp_path, lifecycle_module):
    lifecycle = FakeLifecycle(summary=REPLY)
    ctx = FakeContext(tmp_path / "data", {"require_plan_approval": False, "autonomous_agent_execution": True},
                      lifecycle)
    ge_hermes.register(ctx)
    assert ge_hermes.LAST_REGISTRATION["autonomous_agent_execution"] == {"enabled": True, "host_supported": True}
    handler = ctx.tools["ge_graph"]
    created = json.loads(handler({"action": "create", "spec": spec_json()}))
    result = json.loads(handler({"action": "run", "run_id": created["run_id"]}))
    assert result["status"]["status"] == "SUCCEEDED" and len(lifecycle.requests) == 2
    assert [r.metadata["graph_engineering"]["node"] for r in lifecycle.requests] == ["a", "b"]
    assert json.loads(handler({"action": "verify", "run_id": created["run_id"]}))["receipt"]["verified"] is True


def test_registered_plugin_on_a_host_without_the_api_stays_usable(tmp_path):
    ctx = FakeContext(tmp_path / "data", {"require_plan_approval": False, "autonomous_agent_execution": True})
    ge_hermes.register(ctx)  # registration must not fail
    assert ge_hermes.LAST_REGISTRATION["autonomous_agent_execution"] == {"enabled": True, "host_supported": False}
    handler = ctx.tools["ge_graph"]
    created = json.loads(handler({"action": "create", "spec": spec_json()}))
    result = json.loads(handler({"action": "run", "run_id": created["run_id"]}))
    assert result["autonomous"]["error"] == "HOST_EXECUTION_UNAVAILABLE"
    assert result["status"]["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION"


def test_two_profiles_use_only_their_own_host_and_state(tmp_path, lifecycle_module):
    first, second = FakeLifecycle(summary=REPLY), FakeLifecycle(summary=REPLY)
    settings = {"require_plan_approval": False, "autonomous_agent_execution": True}
    ctx_a = FakeContext(tmp_path / "profile-a", settings, first)
    ctx_b = FakeContext(tmp_path / "profile-b", settings, second)
    ge_hermes.register(ctx_a)
    ge_hermes.register(ctx_b)
    created = json.loads(ctx_a.tools["ge_graph"]({"action": "create", "spec": spec_json()}))
    json.loads(ctx_a.tools["ge_graph"]({"action": "run", "run_id": created["run_id"]}))
    assert len(first.requests) == 2 and second.requests == []
    assert json.loads(ctx_b.tools["ge_graph"]({"action": "list"}))["runs"] == []
    assert json.loads(ctx_b.tools["ge_graph"]({"action": "status", "run_id": created["run_id"]}))["error"] == "RUN_NOT_FOUND"


# ------------------------------------------------------------------------------ extraction
@pytest.mark.parametrize("payload, expected", [
    ('{"result": "x"}', {"result": "x"}),
    ('Here you go:\n```json\n{"result": "x"}\n```\nDone.', {"result": "x"}),
    ('draft {"result": "old"} final {"result": "new"}', {"result": "new"}),
    ('```\n{"result": "x"}\n```', {"result": "x"}),
    ('{"outputs": {"result": "wrapped"}}', {"result": "wrapped"}),
    ({"result": "mapping"}, {"result": "mapping"}),
    ('noise {"other": 1} then {"result": "x"} trailing', {"result": "x"}),
])
def test_extract_outputs_accepts_the_common_reply_shapes(payload, expected):
    outputs, why = extract_outputs(payload, {"result": {"type": "string"}})
    assert outputs == expected and why == ""


@pytest.mark.parametrize("payload", ["", "   ", "no json here", "[1, 2, 3]", '{"unrelated": 1}', None, 5])
def test_extract_outputs_never_invents_outputs(payload):
    outputs, why = extract_outputs(payload, {"result": {"type": "string"}})
    assert outputs is None and why


def test_extract_outputs_leaves_contract_checking_to_the_engine():
    outputs, _ = extract_outputs('{"result": 5, "extra": true}', {"result": {"type": "string"}})
    assert outputs == {"result": 5, "extra": True}  # passed on as found; GraphEngine.submit rejects it
