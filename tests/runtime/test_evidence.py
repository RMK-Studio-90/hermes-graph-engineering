"""Deterministic evidence: real processes, real files, real hashes, a real git work tree.
An agent's claim is never evidence; what the runtime observed is."""
import hashlib
import json
import subprocess
import sys

import pytest

from ge_runtime.engine import GraphEngine
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.evidence import EvidenceRunner, evidence_errors
from ge_runtime.policy import PolicyConfig, apply_policy
from ge_runtime.spec import admit
from ge_runtime.store import RunStore

PY = sys.executable


def node(evidence, outputs=None, **over):
    base = {"id": "work", "purpose": "produce the artifact", "executor": "agent",
            "inputs": {"task": {"from": "graph.inputs.task", "type": "string"}},
            "outputs": outputs or {"result": {"type": "string"}},
            "success_criteria": [], "evidence": evidence, "idempotent": True}
    base.update(over)
    return base


def graph(*nodes, **over):
    data = {"schema_version": 1, "graph_id": "evidence", "title": "evidence", "inputs": {"task": "t"},
            "nodes": list(nodes)}
    data.update(over)
    return data


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def engine(tmp_path):
    return GraphEngine(RunStore(tmp_path / "store"), require_plan_approval=False)


def start(engine, workspace, spec):
    run_id = engine.create(spec, created_by="t", workspace=str(workspace))["run_id"]
    engine.execute(run_id)
    return run_id


def test_admission_rejects_malformed_or_escaping_evidence():
    assert evidence_errors([{"type": "file_exists", "path": "../etc/passwd"}], "e")
    assert evidence_errors([{"type": "file_exists", "path": "/etc/passwd"}], "e")
    assert evidence_errors([{"type": "command", "argv": "pytest"}], "e")
    assert evidence_errors([{"type": "file_sha256", "path": "a", "sha256": "xyz"}], "e")
    assert evidence_errors([{"type": "nope"}], "e")
    assert not evidence_errors([{"type": "command", "argv": ["pytest", "-q"], "timeout_seconds": 60}], "e")
    with pytest.raises(GraphEngineeringError) as exc:
        admit(graph(node([{"type": "file_contains", "path": "a.txt"}])))
    assert exc.value.code is ErrorCode.GRAPH_INVALID


def test_a_claimed_success_without_the_artifact_fails_the_attempt(engine, workspace):
    run_id = start(engine, workspace, graph(node([{"type": "file_exists", "path": "report.md", "non_empty": True}])))
    state = engine.submit(run_id, "work", outputs={"result": "I wrote report.md"})  # the agent's claim
    ns = state["nodes"]["work"]
    assert state["status"] == "FAILED" and ns["last_error"]["code"] == "EVIDENCE_FAILED"
    assert ns["evidence"][0]["passed"] is False and "does not exist" in ns["evidence"][0]["detail"]


def test_command_evidence_records_the_real_exit_code_and_output(engine, workspace):
    (workspace / "check.py").write_text("import sys\nprint('3 passed')\nsys.exit(0)\n", encoding="utf-8")
    run_id = start(engine, workspace, graph(node([
        {"type": "command", "argv": [PY, "check.py"], "expect_exit": 0, "stdout_contains": "passed"}])))
    state = engine.submit(run_id, "work", outputs={"result": "done"})
    assert state["status"] == "SUCCEEDED"
    entry = state["nodes"]["work"]["evidence"][0]
    artifact = engine.store.read_artifact(run_id, entry["artifact"])
    result = artifact["results"][0]
    assert result["exit_code"] == 0 and "3 passed" in result["stdout_tail"]
    assert result["stdout_sha256"] == hashlib.sha256(b"3 passed\n").hexdigest()
    receipt = engine.store.read_receipt(run_id)
    assert receipt["verified"] and receipt["history"]["nodes"]["work"]["evidence"][0]["digest"] == entry["digest"]


def test_a_failing_command_fails_with_its_exit_code(engine, workspace):
    run_id = start(engine, workspace, graph(node([
        {"type": "command", "argv": [PY, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]}])))
    state = engine.submit(run_id, "work", outputs={"result": "all tests pass"})
    assert state["status"] == "FAILED"
    assert "exit code 3, expected 0" in state["nodes"]["work"]["last_error"]["message"]
    assert "boom" in state["nodes"]["work"]["last_error"]["message"]


def test_hash_evidence_checks_the_agents_claimed_hash(engine, workspace):
    (workspace / "out.bin").write_bytes(b"payload")
    real = hashlib.sha256(b"payload").hexdigest()
    spec = graph(node([{"type": "file_sha256", "path": "out.bin", "from_output": "digest"}],
                      outputs={"digest": {"type": "string"}}))
    run_id = start(engine, workspace, spec)
    assert engine.submit(run_id, "work", outputs={"digest": "0" * 64})["status"] == "FAILED"
    run_id = start(engine, workspace, spec)
    assert engine.submit(run_id, "work", outputs={"digest": real})["status"] == "SUCCEEDED"


def git(workspace, *args):
    subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True)


def test_git_diff_evidence_limits_what_an_attempt_may_change(engine, workspace):
    git(workspace, "init", "-q")
    git(workspace, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    (workspace / "unrelated.txt").write_text("dirty before the attempt\n", encoding="utf-8")  # pre-existing change
    spec = graph(node([{"type": "git_diff", "allowed_paths": ["src/*"], "require_changes": True}]))
    run_id = start(engine, workspace, spec)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    state = engine.submit(run_id, "work", outputs={"result": "changed src/app.py"})
    assert state["status"] == "SUCCEEDED"  # the pre-existing dirty file is not attributed to this attempt
    run_id = start(engine, workspace, spec)
    (workspace / "README.md").write_text("sneaky\n", encoding="utf-8")
    state = engine.submit(run_id, "work", outputs={"result": "changed only src"})
    assert state["status"] == "FAILED" and "README.md" in state["nodes"]["work"]["last_error"]["message"]


def test_a_read_only_node_that_changes_the_workspace_is_a_policy_violation(tmp_path, workspace):
    engine = GraphEngine(RunStore(tmp_path / "store"))
    spec = graph(node(None, purpose="Summarize the design document"))
    spec["nodes"][0].pop("evidence")
    run_id = engine.create(spec, created_by="t", workspace=str(workspace), autopilot={"repair": True})["run_id"]
    apply_policy(engine, run_id, PolicyConfig())
    engine.execute(run_id)
    (workspace / "rogue.txt").write_text("a read-only worker wrote this\n", encoding="utf-8")
    state = engine.submit(run_id, "work", outputs={"result": "summary"})
    ns = state["nodes"]["work"]
    assert state["status"] == "FAILED" and ns["last_error"]["code"] == "POLICY_VIOLATION"
    assert ns["status"] == "FAILED"  # never repaired, never retried
    assert "rogue.txt" in ns["last_error"]["message"]


def test_acceptance_is_rechecked_at_verification(engine, workspace):
    spec = graph(node([], outputs={"result": {"type": "string"}}),
                 acceptance=[{"type": "file_contains", "path": "result.txt", "text": "OK"}])
    spec["nodes"][0].pop("evidence")
    run_id = start(engine, workspace, spec)
    (workspace / "result.txt").write_text("OK\n", encoding="utf-8")
    state = engine.submit(run_id, "work", outputs={"result": "done"})
    assert state["status"] == "SUCCEEDED" and engine.store.read_receipt(run_id)["verification_level"] == "EVIDENCE"
    (workspace / "result.txt").write_text("broken later\n", encoding="utf-8")
    receipt = engine.verify(run_id)
    failed = {c["check"] for c in receipt["checks"] if not c["passed"]}
    assert failed == {"acceptance:re-checked"}


def test_tampered_evidence_artifacts_fail_verification(engine, workspace):
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    run_id = start(engine, workspace, graph(node([{"type": "file_exists", "path": "a.txt"}])))
    state = engine.submit(run_id, "work", outputs={"result": "ok"})
    path = engine.store.run_dir(run_id) / "artifacts" / (state["nodes"]["work"]["evidence"][0]["artifact"] + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["results"][0]["passed"] = False
    path.write_text(json.dumps(record), encoding="utf-8")
    receipt = engine.verify(run_id)
    check = next(c for c in receipt["checks"] if c["check"] == "node:work:evidence")
    assert check["passed"] is False and "checksum" in check["detail"]


def test_self_reported_success_is_not_verified_when_evidence_is_required(engine, workspace):
    spec = graph(node([], outputs={"result": {"type": "string"}}), require_evidence=True)
    spec["nodes"][0].pop("evidence")
    run_id = start(engine, workspace, spec)
    engine.submit(run_id, "work", outputs={"result": "trust me"})
    receipt = engine.store.read_receipt(run_id)
    assert receipt["run_status"] == "SUCCEEDED" and receipt["verified"] is False
    assert receipt["verification_level"] == "SELF_REPORTED"
    assert [c["check"] for c in receipt["checks"] if not c["passed"]] == ["evidence_coverage"]


def test_runner_without_workspace_fails_closed(tmp_path):
    runner = EvidenceRunner(RunStore(tmp_path))
    result = runner.run_check({"type": "file_exists", "path": "a"}, 0, {"run_id": "x"}, None, {}, {})
    assert result["passed"] is False and "no workspace" in result["detail"]
