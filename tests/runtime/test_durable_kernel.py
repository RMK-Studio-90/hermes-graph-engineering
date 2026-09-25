"""Durable kernel: explicit transitions, state/journal binding, journal repair and quarantine,
divergence detection, automatic receipts, crash-safe finalization and schema migration.

Regression tests for the audit findings: a torn final journal line used to brick a run forever
(STATE_CORRUPT on every append), a rolled-back state.json was accepted silently, receipts existed
only after an explicit verify and carried no checksum, and replaced files were not made durable
with a directory fsync.
"""
import json
import shutil
from pathlib import Path

import pytest

import ge_runtime._fs as fs
from ge_hermes.templates import build_template
from ge_runtime.engine import GraphEngine
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.states import (NODE_TRANSITIONS, RUN_TRANSITIONS, NodeStatus, RunStatus, check_node_transition,
                               check_run_transition)
from ge_runtime.store import LocalStateStore, RunStore, StateStore, scan_trace_bytes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v1" / "run_dirs"


def agent_spec(**node_over):
    node = {"id": "a", "purpose": "do a", "executor": "agent",
            "inputs": {"task": {"from": "graph.inputs.task", "type": "string"}},
            "outputs": {"result": {"type": "string"}},
            "success_criteria": [{"output": "result", "check": "non_empty"}]}
    node.update(node_over)
    return {"schema_version": 1, "graph_id": "kernel", "title": "kernel", "inputs": {"task": "go"}, "nodes": [node]}


@pytest.fixture
def engine(tmp_path):
    return GraphEngine(RunStore(tmp_path))


def finished_run(engine):
    run_id = engine.create(build_template("text-pipeline", " durable "), created_by="t")["run_id"]
    engine.decide(run_id, "plan", approve=True, decided_by="t")
    engine.execute(run_id)
    return run_id


def waiting_run(engine):
    run_id = engine.create(agent_spec(), created_by="t")["run_id"]
    engine.decide(run_id, "plan", approve=True, decided_by="t")
    engine.execute(run_id)
    return run_id


def trace_path(engine, run_id):
    return engine.store.run_dir(run_id) / "trace.jsonl"


def types(engine, run_id):
    return [e["type"] for e in engine.store.read_trace(run_id)]


# ------------------------------------------------------------------ transitions
def test_transition_tables_are_explicit_and_terminal_states_are_final():
    assert set(RUN_TRANSITIONS) == set(RunStatus) and set(NODE_TRANSITIONS) == set(NodeStatus)
    for terminal in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        assert RUN_TRANSITIONS[terminal] == frozenset()
    with pytest.raises(GraphEngineeringError) as exc:
        check_run_transition("SUCCEEDED", "RUNNING")
    assert exc.value.code is ErrorCode.INVALID_TRANSITION
    with pytest.raises(GraphEngineeringError):
        check_node_transition("SUCCEEDED", "READY")
    with pytest.raises(GraphEngineeringError):
        check_node_transition("PENDING", "SUCCEEDED")
    assert check_node_transition("SUCCEEDED", "PENDING", revision=True) is NodeStatus.PENDING
    with pytest.raises(GraphEngineeringError):
        check_node_transition("RUNNING", "PENDING", revision=True)  # never reset work in flight


def test_the_local_store_implements_the_state_store_contract(tmp_path):
    assert isinstance(LocalStateStore(tmp_path), StateStore)


# ------------------------------------------------------------------ journal binding
def test_every_save_is_bound_to_the_journal_head_and_recorded_in_it(engine):
    run_id = finished_run(engine)
    state = engine.store.load(run_id)
    events = engine.store.read_trace(run_id)
    saved = [e for e in events if e["type"] == "state.saved"]
    assert [e["revision"] for e in saved] == list(range(1, state["revision"] + 1))
    head = state["journal"]
    assert events[head["seq"] - 1]["hash"] == head["hash"]
    assert saved[-1]["checksum"] == state["_checksum"]  # the journal records the exact state it bound
    assert engine.inspect_integrity(state)["consistent"] is True


def test_a_torn_final_journal_line_is_repaired_and_quarantined_not_fatal(engine):
    """Regression: a crash mid-append used to make every later append fail with STATE_CORRUPT."""
    run_id = waiting_run(engine)
    with open(trace_path(engine, run_id), "ab") as handle:
        handle.write(b'{"seq": 999, "type": "node.subm')  # interrupted write: no newline
    assert engine.store.scan_trace(run_id).status == "torn_tail"
    state = engine.submit(run_id, "a", outputs={"result": "done"})
    assert state["status"] == "SUCCEEDED"
    repaired = [r for r in state["recovery"] if r["kind"] == "trace_torn_tail_repaired"]
    assert repaired and repaired[0]["severity"] == "repaired" and repaired[0]["dropped_bytes"] == 31
    quarantine = engine.store.run_dir(run_id) / repaired[0]["quarantine"]
    assert quarantine.read_bytes() == b'{"seq": 999, "type": "node.subm'
    assert "trace.repaired" in types(engine, run_id)
    assert engine.verify(run_id)["verified"] is True  # a crash artefact, no provable history was lost


def test_a_corrupted_journal_is_quarantined_and_verification_fails_for_good(engine):
    run_id = waiting_run(engine)
    path = trace_path(engine, run_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[1])
    forged["type"] = "forged"
    lines[1] = json.dumps(forged)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = engine.submit(run_id, "a", outputs={"result": "done"})  # the run stays operable
    assert state["status"] == "SUCCEEDED"
    [violation] = [r for r in state["recovery"] if r["severity"] == "violation"]
    assert violation["kind"] == "trace_quarantined"
    assert (engine.store.run_dir(run_id) / violation["quarantine"]).is_file()  # never deleted
    assert types(engine, run_id)[0] == "trace.quarantined"
    receipt = engine.verify(run_id)
    failed = {c["check"] for c in receipt["checks"] if not c["passed"]}
    assert receipt["verified"] is False and {"trace_chain", "journal_binding"} <= failed


def test_a_truncated_journal_is_detected(engine):
    run_id = waiting_run(engine)
    path = trace_path(engine, run_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")  # clean cut at a line boundary
    state = engine.submit(run_id, "a", outputs={"result": "done"})
    assert [r["kind"] for r in state["recovery"] if r["severity"] == "violation"] == ["trace_truncated"]
    assert engine.verify(run_id)["verified"] is False


def test_a_rolled_back_state_file_is_refused(engine):
    """Regression: restoring an older (validly checksummed) state.json used to be accepted silently."""
    run_id = waiting_run(engine)
    state_file = engine.store.run_dir(run_id) / "state.json"
    old = state_file.read_bytes()
    engine.submit(run_id, "a", outputs={"result": "done"})
    state_file.write_bytes(old)
    with pytest.raises(GraphEngineeringError) as exc:
        engine.verify(run_id)
    assert exc.value.code is ErrorCode.STATE_DIVERGED
    state, _spec = engine.load(run_id)
    assert engine.inspect_integrity(state)["consistent"] is False  # status reports it read-only


def test_updates_lost_between_journal_and_state_are_reconciled_conservatively(engine):
    run_id = waiting_run(engine)
    with engine.store.lock(run_id):  # a crash after the journal write, before the state save
        engine.store.append_trace(run_id, "node.submitted", node="a", attempt=1, failed=False, submitted_by="x")
    state = engine.resume(run_id)
    [entry] = [r for r in state["recovery"] if r["kind"] == "state_behind_journal"]
    assert entry["severity"] == "repaired" and entry["events"][0]["type"] == "node.submitted"
    assert state["nodes"]["a"]["wait"] == "submission"  # the saved state is authoritative
    assert "state.reconciled" in types(engine, run_id)


# ------------------------------------------------------------------ receipts and finalization
def test_a_terminal_run_gets_a_checksummed_receipt_automatically(engine):
    run_id = finished_run(engine)
    receipt = engine.store.read_receipt(run_id)  # validates schema and checksum
    state = engine.store.load(run_id)
    assert receipt["receipt_schema_version"] == 2 and receipt["generated_by"] == "finalize"
    assert receipt["verified"] is True and receipt["terminal_outcome"] == "SUCCEEDED"
    assert state["finalization"]["status"] == "COMPLETE"
    assert state["finalization"]["receipt_checksum"] == receipt["checksum"]
    assert set(receipt["history"]["nodes"]) == set(state["order"])
    assert receipt["history"]["nodes"]["transform_input"]["history"][-1]["outcome"] == "SUCCESS"
    assert receipt["journal_head"]["seq"] > 0 and receipt["recovery"] == []
    assert "run.receipt_written" in types(engine, run_id)


def test_a_damaged_or_missing_receipt_is_recovered(engine):
    run_id = finished_run(engine)
    path = engine.store.run_dir(run_id) / "receipt.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["verified"] = False
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(GraphEngineeringError) as exc:
        engine.store.read_receipt(run_id)
    assert exc.value.code is ErrorCode.RECEIPT_CORRUPT
    receipt = engine.ensure_finalized(run_id)
    assert receipt["verified"] is True and receipt["generated_by"] == "recovery"
    assert engine.store.read_receipt(run_id)["checksum"] == receipt["checksum"]
    path.unlink()
    assert engine.ensure_finalized(run_id)["generated_by"] == "recovery"
    kinds = [r["kind"] for r in engine.store.load(run_id)["recovery"]]
    assert kinds == ["receipt_recovered", "receipt_recovered"]
    assert engine.ensure_finalized(run_id)["checksum"] == engine.store.read_receipt(run_id)["checksum"]  # stable


def test_a_crash_during_finalization_is_completed_on_the_next_access(engine, monkeypatch):
    run_id = waiting_run(engine)
    real = engine.store.write_receipt

    def crash(*args, **kwargs):
        raise OSError("power lost")

    monkeypatch.setattr(engine.store, "write_receipt", crash)
    with pytest.raises(OSError):
        engine.submit(run_id, "a", outputs={"result": "done"})
    state = engine.store.load(run_id)
    assert state["status"] == "SUCCEEDED" and state["finalization"]["status"] == "PENDING"  # durable intent
    monkeypatch.setattr(engine.store, "write_receipt", real)
    receipt = engine.ensure_finalized(run_id)
    assert receipt["verified"] is True and engine.store.load(run_id)["finalization"]["status"] == "COMPLETE"


def test_replaced_files_are_made_durable_with_a_directory_fsync(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(fs, "fsync_dir", lambda directory: synced.append(Path(directory)))
    fs.atomic_write_text(tmp_path / "x.json", "{}")
    fs.append_line_durable(tmp_path / "new.jsonl", "{}")
    fs.append_line_durable(tmp_path / "new.jsonl", "{}")  # existing file: no directory change
    assert synced == [tmp_path, tmp_path]


def test_scan_distinguishes_torn_tails_from_corruption():
    good = b'{"a":1}\n'
    assert scan_trace_bytes(b"").status == "ok"
    assert scan_trace_bytes(b"garbage").status == "torn_tail"
    assert scan_trace_bytes(b"garbage\n").status == "corrupt"
    assert scan_trace_bytes(good).status == "corrupt"  # not a chained event


# ------------------------------------------------------------------ migration
def copy_fixtures(tmp_path):
    runs = tmp_path / "runs"
    shutil.copytree(FIXTURES, runs)
    return sorted(p.name for p in runs.iterdir())


def test_schema_v1_runs_are_migrated_and_stay_verifiable(tmp_path):
    finished, waiting = sorted(copy_fixtures(tmp_path), key=lambda r: not r.startswith("text-pipeline"))
    engine = GraphEngine(RunStore(tmp_path))
    raw = json.loads((tmp_path / "runs" / finished / "state.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    state, _spec = engine.load(finished)
    assert state["state_schema_version"] == 2 and state["journal"] is None
    receipt = engine.ensure_finalized(finished)  # 1.x receipts had no checksum: regenerated
    assert receipt["receipt_schema_version"] == 2 and receipt["verified"] is True
    on_disk = json.loads((tmp_path / "runs" / finished / "state.json").read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 2
    assert [r["kind"] for r in on_disk["state"]["recovery"]] == ["schema_migrated", "receipt_recovered"]
    assert "state.migrated" in types(engine, finished)
    # an unfinished 1.x run continues under the new kernel
    state = engine.submit(waiting, "a", outputs={"result": "after upgrade"})
    assert state["status"] == "SUCCEEDED" and engine.store.read_receipt(waiting)["verified"] is True


def test_unknown_schema_versions_still_fail_closed(tmp_path):
    copy_fixtures(tmp_path)
    run_id = sorted((tmp_path / "runs").iterdir())[0].name
    path = tmp_path / "runs" / run_id / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GraphEngineeringError) as exc:
        GraphEngine(RunStore(tmp_path)).load(run_id)
    assert exc.value.code is ErrorCode.STATE_CORRUPT
