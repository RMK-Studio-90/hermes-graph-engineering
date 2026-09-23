"""The /ge-reclaim operator command releases a stale dispatch and holds the node."""
import json

import pytest

from ge_hermes.commands import CommandSet
from ge_hermes.service import GraphService
from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.errors import ErrorCode, GraphEngineeringError


class Host:
    def __init__(self):
        self.calls = []

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        self.calls.append(order["node"])
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"result": "actual test output"})


@pytest.fixture
def service(tmp_path):
    settings = {"autonomous_agent_execution": True}
    return GraphService(tmp_path, lambda key, default: settings.get(key, default), Host())


def handler(service, name, *, desktop=True):
    fn = next(fn for key, fn, _, _ in CommandSet(service).handlers() if key == name)
    # The desktop agent_turn_handler attribute only exists when the host wires
    # agent turn handoff (independent WIP). The reclaim command must be testable
    # on its own, so fall back to the synchronous handler when not wired.
    if desktop and hasattr(fn, "agent_turn_handler"):
        return fn.agent_turn_handler
    return fn


def _stale_run(service):
    raw = {"schema_version": 1, "graph_id": "agentic",
           "nodes": [{"id": "a", "purpose": "do a", "executor": "agent",
                      "outputs": {"result": "string"},
                      "success_criteria": [{"output": "result", "check": "non_empty"}]}]}
    engine = service.engine()
    run_id = engine.create(raw, created_by="test")["run_id"]
    engine.decide(run_id, "plan", approve=True, decided_by="tester")
    engine.execute(run_id)
    engine.store.append_trace(run_id, "node.dispatched", node="a", attempt=1,
                              dispatch=1, dispatcher="host-agent")
    return run_id, "a"


def test_ge_reclaim_releases_stale_dispatch_and_holds_for_retry(service):
    run_id, node = _stale_run(service)
    result = json.loads(handler(service, "ge-reclaim")(run_id + " " + node + " --force --json"))
    assert result["ok"] is True
    assert result["status"]["holds"][0]["kind"] == "NEEDS_ATTENTION"
    assert result["status"]["nodes"]["a"]["wait"] == "attention"
    state = service.engine().load(run_id)[0]
    assert state["nodes"]["a"]["history"][-1]["outcome"] == "STALE_DISPATCH_RECOVERED"
    events = [e for e in service.engine().store.read_trace(run_id)
              if e.get("type") == "node.dispatch_released"]
    assert len(events) == 1 and events[0]["reason"] == "stale_dispatch_recovery"


def test_ge_reclaim_requires_node_argument(service):
    run_id, _node = _stale_run(service)
    out = handler(service, "ge-reclaim")(run_id + " --json")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == ErrorCode.INVALID_ARGUMENT.value
