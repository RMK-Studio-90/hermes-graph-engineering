"""Durable dispatch: persistent leases (owner, heartbeat, expiry), release in ``finally``, automatic
reclaim of abandoned claims, persisted DISPATCH_INTERRUPTED and protection against duplicate
execution across real processes, including ``kill -9`` in the middle of a dispatch.

Regression tests for the audit findings: the dispatch lease existed only inside one process, so a
second process could execute the same node again; an interrupted non-idempotent dispatch was only
reported and never persisted; a stale claim could be reclaimed while its worker was still alive.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.dispatch import AutonomousDispatcher, DispatchLease, lease_is_live
from ge_runtime.engine import GraphEngine
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.store import RunStore

SUPPORT = Path(__file__).resolve().parents[1] / "support" / "dispatch_proc.py"
SRC = Path(__file__).resolve().parents[2] / "src"
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals")


def spec(idempotent=False):
    return {"schema_version": 1, "graph_id": "lease", "title": "lease", "inputs": {"task": "go"}, "nodes": [{
        "id": "a", "purpose": "do a", "executor": "agent", "idempotent": idempotent,
        "inputs": {"task": {"from": "graph.inputs.task", "type": "string"}},
        "outputs": {"result": {"type": "string"}}, "success_criteria": [{"output": "result", "check": "non_empty"}]}]}


def waiting(tmp_path, idempotent=False):
    engine = GraphEngine(RunStore(tmp_path), require_plan_approval=False)
    run_id = engine.create(spec(idempotent), created_by="t")["run_id"]
    engine.execute(run_id)
    return engine, run_id


class Worker:
    def __init__(self, respond=None):
        self.calls = []
        self.respond = respond or (lambda order, ctx: ExecutorResult(ExecutorOutcome.SUCCESS,
                                                                     outputs={"result": "ok"}))

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        self.calls.append(order["node"])
        return self.respond(order, context)


def types(engine, run_id):
    return [e["type"] for e in engine.store.read_trace(run_id)]


def spawn(root, run_id, log, sleep, ttl=60.0):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), env_path()]))
    return subprocess.Popen([sys.executable, str(SUPPORT), str(root), run_id, str(log), str(sleep), str(ttl)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def env_path():
    return os.environ.get("PYTHONPATH", "")


def executions(log):
    return [line.split() for line in Path(log).read_text(encoding="utf-8").splitlines()] if Path(log).exists() else []


def wait_for(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ lease semantics
def test_lease_is_persisted_exclusive_and_released(tmp_path):
    engine, run_id = waiting(tmp_path)
    first, second = DispatchLease(engine, run_id), DispatchLease(engine, run_id)
    assert first.acquire() is True
    stored = engine.store.read_lease(run_id)
    assert stored["owner"] == first.owner and stored["pid"] == os.getpid() and stored["epoch"] == 1
    assert lease_is_live(stored)
    assert second.acquire() is False and second.holder["owner"] == first.owner
    first.release()
    assert engine.store.read_lease(run_id) is None
    assert second.acquire() is True and second.epoch == 1
    assert types(engine, run_id).count("lease.acquired") == 2 and "lease.released" in types(engine, run_id)


def test_an_expired_lease_is_taken_over_and_recorded(tmp_path):
    engine, run_id = waiting(tmp_path)
    stale = DispatchLease(engine, run_id, ttl_seconds=0.2)
    assert stale.acquire()
    time.sleep(0.3)  # no heartbeat: expired
    rival = DispatchLease(engine, run_id)
    assert rival.acquire() is True and rival.epoch == 2
    taken = [e for e in engine.store.read_trace(run_id) if e["type"] == "lease.acquired"][-1]
    assert taken["took_over"] == {"owner": stale.owner, "epoch": 1, "reason": "expired"}
    assert stale.verify() is False and stale.heartbeat() is False and stale.lost is True


def test_a_lease_of_a_dead_process_on_this_host_expires_immediately(tmp_path):
    engine, run_id = waiting(tmp_path)
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    lease = DispatchLease(engine, run_id)
    assert lease.acquire()
    stored = dict(engine.store.read_lease(run_id), pid=int(dead.stdout))
    engine.store.write_lease(run_id, stored)
    assert lease_is_live(stored) is False  # heartbeat is fresh, but its owner is gone


def test_heartbeat_keeps_a_long_dispatch_alive_beyond_its_ttl(tmp_path):
    engine, run_id = waiting(tmp_path)
    rival_result = {}

    def slow(order, context):
        time.sleep(0.5)  # longer than the ttl below

        def rival():  # a plain thread: no worker context, like another process
            rival_result["report"] = AutonomousDispatcher(GraphEngine(RunStore(tmp_path)), Worker(),
                                                          state_key="other-process").drive(run_id)

        thread = threading.Thread(target=rival)
        thread.start()
        thread.join()
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"result": "slow"})

    worker = Worker(slow)
    report = AutonomousDispatcher(engine, worker, state_key="k", lease_ttl_seconds=0.2).drive(run_id)
    assert report["stopped"] == "run_finished" and worker.calls == ["a"]
    assert rival_result["report"]["stopped"] == "dispatch_in_progress"
    assert rival_result["report"]["error"] == "DISPATCH_IN_PROGRESS"


def test_the_lease_is_released_in_finally_even_when_the_process_is_interrupted(tmp_path):
    engine, run_id = waiting(tmp_path)

    def dies(order, context):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        AutonomousDispatcher(engine, Worker(dies), state_key="k").drive(run_id)
    assert engine.store.read_lease(run_id) is None
    assert "node.dispatched" in types(engine, run_id) and types(engine, run_id)[-1] == "lease.released"


def test_a_worker_result_is_not_submitted_after_the_lease_was_taken_over(tmp_path):
    engine, run_id = waiting(tmp_path, idempotent=True)

    def usurped(order, context):
        lease = engine.store.read_lease(run_id)
        engine.store.write_lease(run_id, dict(lease, lease_id="someone-else", owner="other:1:x"))
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"result": "late"})

    report = AutonomousDispatcher(engine, Worker(usurped), state_key="k").drive(run_id)
    assert report["stopped"] == "lease_lost"
    state = engine.load(run_id)[0]
    assert state["nodes"]["a"]["outputs"] is None and "node.submitted" not in types(engine, run_id)


def test_operator_reclaim_refuses_a_dispatch_with_a_live_lease(tmp_path):
    engine, run_id = waiting(tmp_path)
    lease = DispatchLease(engine, run_id)
    assert lease.acquire()
    from ge_runtime.dispatch import claim
    assert claim(engine, run_id, "a", 1, lease) == 1
    with pytest.raises(GraphEngineeringError) as exc:
        engine.reclaim_stale_dispatch(run_id, "a", decided_by="op", force=True)
    assert exc.value.code is ErrorCode.DISPATCH_IN_PROGRESS
    lease.release()
    state = engine.reclaim_stale_dispatch(run_id, "a", decided_by="op", force=True)
    assert state["nodes"]["a"]["wait"] == "attention"


# ------------------------------------------------------------------ abandoned claims
def abandon_claim(engine, run_id):
    """A dispatcher that claimed the node and died without a trace of its end (lease expired)."""
    from ge_runtime.dispatch import claim

    gone = DispatchLease(engine, run_id, ttl_seconds=0.05)
    assert gone.acquire()
    assert claim(engine, run_id, "a", 1, gone) == 1
    time.sleep(0.1)


def test_an_abandoned_claim_of_an_idempotent_node_is_reclaimed_and_dispatched_again(tmp_path):
    engine, run_id = waiting(tmp_path, idempotent=True)
    abandon_claim(engine, run_id)
    worker = Worker()
    report = AutonomousDispatcher(engine, worker, state_key="k").drive(run_id)
    assert report["stopped"] == "run_finished" and worker.calls == ["a"]
    released = [e for e in engine.store.read_trace(run_id) if e["type"] == "node.dispatch_released"]
    assert released[0]["reason"] == "abandoned_claim_reclaimed"
    assert report["dispatched"][0]["dispatch"] == 2


def test_an_abandoned_claim_of_unsafe_work_is_persisted_as_dispatch_interrupted(tmp_path):
    engine, run_id = waiting(tmp_path)
    abandon_claim(engine, run_id)
    worker = Worker()
    report = AutonomousDispatcher(engine, worker, state_key="k").drive(run_id)
    assert worker.calls == [] and report["error"] == "DISPATCH_INTERRUPTED"
    state = engine.load(run_id)[0]
    assert state["nodes"]["a"]["wait"] == "attention"
    assert state["nodes"]["a"]["last_error"]["code"] == "DISPATCH_INTERRUPTED"
    assert state["holds"] == [{"kind": "NEEDS_ATTENTION", "node": "a", "gate": None, "code": "DISPATCH_INTERRUPTED"}]
    # stable across further drives: nothing is dispatched until the operator decides
    assert AutonomousDispatcher(engine, worker, state_key="k").drive(run_id)["stopped"] == "no_pending_work"
    engine.retry_node(run_id, "a", decided_by="operator")
    AutonomousDispatcher(engine, worker, state_key="k").drive(run_id)
    assert worker.calls == ["a"] and engine.load(run_id)[0]["status"] == "SUCCEEDED"


# ------------------------------------------------------------------ real processes
def test_two_processes_never_execute_the_same_node_twice(tmp_path):
    engine, run_id = waiting(tmp_path, idempotent=True)  # replay-safe: the most exposed case
    log = tmp_path / "executions.log"
    procs = [spawn(tmp_path, run_id, log, 1.0) for _ in range(3)]
    reports = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert len(executions(log)) == 1, executions(log)
    stops = sorted(r["stopped"] for r in reports)
    assert stops.count("run_finished") + stops.count("no_pending_work") >= 1
    assert stops.count("dispatch_in_progress") >= 1
    assert engine.load(run_id)[0]["status"] == "SUCCEEDED"


@posix_only
def test_kill_9_mid_dispatch_then_restart_redispatches_replay_safe_work(tmp_path):
    engine, run_id = waiting(tmp_path, idempotent=True)
    log = tmp_path / "executions.log"
    victim = spawn(tmp_path, run_id, log, 30.0)
    assert wait_for(lambda: len(executions(log)) == 1)
    os.kill(victim.pid, signal.SIGKILL)
    victim.wait(timeout=10)
    assert engine.store.read_lease(run_id) is not None  # the dead owner could not clean up
    survivor = spawn(tmp_path, run_id, log, 0.0)
    report = json.loads(survivor.communicate(timeout=60)[0])
    assert report["stopped"] == "run_finished", report
    lines = executions(log)
    assert len(lines) == 2 and lines[0][0] != lines[1][0]  # executed again, by the new process
    trace = engine.store.read_trace(run_id)
    takeover = [e for e in trace if e["type"] == "lease.acquired"][-1]["took_over"]
    assert takeover["reason"] == "expired" and takeover["owner"].split(":")[1] == str(victim.pid)
    assert [e["reason"] for e in trace if e["type"] == "node.dispatch_released"] == ["abandoned_claim_reclaimed"]
    state = engine.load(run_id)[0]
    assert state["status"] == "SUCCEEDED" and engine.store.read_receipt(run_id)["verified"] is True


@posix_only
def test_kill_9_mid_dispatch_of_unsafe_work_is_held_not_replayed(tmp_path):
    engine, run_id = waiting(tmp_path)
    log = tmp_path / "executions.log"
    victim = spawn(tmp_path, run_id, log, 30.0)
    assert wait_for(lambda: len(executions(log)) == 1)
    os.kill(victim.pid, signal.SIGKILL)
    victim.wait(timeout=10)
    report = json.loads(spawn(tmp_path, run_id, log, 0.0).communicate(timeout=60)[0])
    assert report["error"] == "DISPATCH_INTERRUPTED" and len(executions(log)) == 1
    state = engine.load(run_id)[0]
    assert state["nodes"]["a"]["last_error"]["code"] == "DISPATCH_INTERRUPTED"
    assert state["holds"][0]["code"] == "DISPATCH_INTERRUPTED"
