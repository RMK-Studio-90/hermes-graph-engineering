"""Focused regression tests for the stale-dispatch recovery action.

Covers: success on a stale RUNNING+WAITING_FOR_SUBMISSION node, the fail-closed
guards (active/terminal/with-output/double-reclaim/wrong-target), trace event +
claim/release accounting, retry-eligibility, attempt increment, prior nodes
unchanged, trace-chain + state-integrity preserved, and idempotent duplicate.
"""
import pytest

from ge_runtime.engine import GraphEngine
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.store import RunStore


def agent_node(node_id, depends_on=(), **extra):
    node = {"id": node_id, "purpose": "do %s" % node_id, "executor": "agent",
            "depends_on": list(depends_on), "outputs": {"result": "string"},
            "success_criteria": [{"output": "result", "check": "non_empty"}]}
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


def stale_submission_run(engine):
    """A run whose first agent node is RUNNING + wait=submission with one
    outstanding host dispatch claim (as if a worker was claimed then died
    before submitting). Returns run_id and the stale node id."""
    run_id = approved(engine, agent_spec(agent_node("a"), agent_node("b", ["a"])))
    engine.execute(run_id)
    engine.store.append_trace(run_id, "node.dispatched", node="a", attempt=1,
                              dispatch=1, dispatcher="host-agent")
    return run_id, "a"


def test_stale_submission_node_reclaims_to_attention(engine):
    run_id, node = stale_submission_run(engine)
    state = engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    ns = state["nodes"][node]
    assert ns["wait"] == "attention"
    assert ns["status"] == "RUNNING"
    assert state["holds"][0]["kind"] == "NEEDS_ATTENTION"
    assert state["status"] == "WAITING"
    assert ns["last_error"]["message"] == "STALE_DISPATCH_RECOVERED"
    assert ns["history"][-1]["outcome"] == "STALE_DISPATCH_RECOVERED"
    assert ns["outputs"] is None


def test_reclaim_emits_dispatch_released_trace_event(engine):
    run_id, node = stale_submission_run(engine)
    engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    events = [e for e in engine.store.read_trace(run_id)
              if e.get("type") == "node.dispatch_released"]
    assert len(events) == 1
    e = events[0]
    assert e["node"] == node and e["attempt"] == 1 and e["dispatch"] == 1
    assert e["reason"] == "stale_dispatch_recovery"
    assert e["released_by"] == "op"
    assert e["previous_wait"] == "submission"
    assert e["evidence"] == "no_host_result"


def test_claim_release_accounting_is_balanced_after_reclaim(engine):
    run_id, node = stale_submission_run(engine)
    engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    events = [e for e in engine.store.read_trace(run_id)
              if e.get("node") == node and e.get("attempt") == 1]
    claims = sum(1 for e in events if e.get("type") == "node.dispatched")
    released = sum(1 for e in events if e.get("type") == "node.dispatch_released")
    assert claims == 1 and released == 1


def test_reclaim_then_retry_increments_attempt_and_creates_new_dispatch(engine):
    run_id, node = stale_submission_run(engine)
    engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    state = engine.retry_node(run_id, node, decided_by="op")
    assert state["nodes"][node]["attempts"] == 2
    assert state["nodes"][node]["wait"] == "submission"
    assert state["nodes"][node]["status"] == "RUNNING"
    assert state["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION"
    assert state["nodes"]["b"]["status"] == "PENDING"


def test_prior_succeeded_nodes_unchanged_by_reclaim(engine):
    run_id2 = approved(engine, agent_spec(agent_node("a"), agent_node("b", ["a"]),
                                          agent_node("c", ["b"])))
    engine.execute(run_id2)
    engine.submit(run_id2, "a", outputs={"result": "done"})
    engine.store.append_trace(run_id2, "node.dispatched", node="b", attempt=1,
                              dispatch=1, dispatcher="host-agent")
    engine.reclaim_stale_dispatch(run_id2, "b", decided_by="op", force=True)
    state = engine.store.load(run_id2)
    assert state["nodes"]["a"]["status"] == "SUCCEEDED"
    assert state["nodes"]["a"]["outputs"] == {"result": "done"}
    assert state["nodes"]["b"]["status"] == "RUNNING"
    assert state["nodes"]["b"]["wait"] == "attention"
    ok, detail = engine.store.verify_trace(run_id2)
    assert ok, detail


def test_reclaim_rejected_when_node_has_outputs(engine):
    run_id = approved(engine, agent_spec(agent_node("a")))
    engine.execute(run_id)
    engine.submit(run_id, "a", outputs={"result": "done"})
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch(run_id, "a", decided_by="op", force=True)
    assert info.value.code is ErrorCode.INVALID_TRANSITION


def test_reclaim_rejected_when_no_outstanding_claim(engine):
    run_id = approved(engine, agent_spec(agent_node("a")))
    engine.execute(run_id)
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch(run_id, "a", decided_by="op", force=True)
    assert info.value.code is ErrorCode.INVALID_TRANSITION
    assert "no outstanding dispatch claim" in info.value.message


def test_reclaim_duplicate_after_release_is_noop_error(engine):
    run_id, node = stale_submission_run(engine)
    engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    assert info.value.code is ErrorCode.INVALID_TRANSITION
    assert "already released" in info.value.message or "not RUNNING+WAITING_FOR_SUBMISSION" in info.value.message
    events = [e for e in engine.store.read_trace(run_id)
              if e.get("type") == "node.dispatch_released"]
    assert len(events) == 1


def test_reclaim_rejected_for_terminal_node(engine):
    run_id = approved(engine, agent_spec(agent_node("a"), agent_node("b", ["a"])))
    engine.execute(run_id)
    engine.submit(run_id, "a", outputs={"result": "x"})
    engine.submit(run_id, "b", outputs={"result": "y"})
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch(run_id, "a", decided_by="op", force=True)
    assert info.value.code is ErrorCode.INVALID_TRANSITION


def test_reclaim_rejected_for_wrong_run_or_node(engine):
    run_id, node = stale_submission_run(engine)
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch(run_id, "does_not_exist", decided_by="op", force=True)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch("pipeline-20260101t000000z-abcdef", node,
                                      decided_by="op", force=True)
    assert info.value.code is ErrorCode.RUN_NOT_FOUND


def test_reclaim_requires_staleness_unless_forced(engine):
    run_id, node = stale_submission_run(engine)
    with pytest.raises(GraphEngineeringError) as info:
        engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=False,
                                      stale_after_seconds=3600.0)
    assert info.value.code is ErrorCode.INVALID_TRANSITION
    assert "not stale yet" in info.value.message


def test_reclaim_preserves_spec_digest_and_approval_binding(engine):
    run_id, node = stale_submission_run(engine)
    before = engine.store.load(run_id)
    spec_digest = before["spec_digest"]
    binding = before["approvals"]["plan"]["binding_digest"]
    engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    after = engine.store.load(run_id)
    assert after["spec_digest"] == spec_digest
    assert after["approvals"]["plan"]["binding_digest"] == binding
    ok, detail = engine.store.verify_trace(run_id)
    assert ok, detail


def test_late_host_submission_after_reclaim_is_rejected(engine):
    run_id, node = stale_submission_run(engine)
    engine.reclaim_stale_dispatch(run_id, node, decided_by="op", force=True)
    # a worker that was actually still alive and submits late must be refused:
    # the node is no longer in submission wait, so no second completion happens.
    with pytest.raises(GraphEngineeringError) as info:
        engine.submit(run_id, node, outputs={"result": "late but real"}, submitted_by="agent:host-autonomous")
    assert info.value.code is ErrorCode.INVALID_TRANSITION
    state = engine.store.load(run_id)
    assert state["nodes"][node]["outputs"] is None
    succ = [e for e in engine.store.read_trace(run_id)
            if e.get("type") == "node.succeeded" and e.get("node") == node]
    assert succ == []
