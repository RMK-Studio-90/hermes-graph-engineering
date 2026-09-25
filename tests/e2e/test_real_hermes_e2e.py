"""REAL HERMES END-TO-END: ``/ge <task>`` typed into the unmodified interactive Hermes CLI.

Everything is real Hermes (CLI, plugin loader, slash-command dispatch, ``inject_message``, agent
loop, tool-search bridge, tool dispatch, subagent lifecycle and child agents, file tools, hooks)
except the language model, which is ``scripted_model``: a deterministic OpenAI-compatible
server (no model credentials exist in this environment). Requires ``GE_HERMES_PYTHON`` and
``pexpect``; skipped otherwise (for example in CI without Hermes).
"""
import glob
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import install_plugin

HERMES_PYTHON = os.environ.get("GE_HERMES_PYTHON")
pexpect = pytest.importorskip("pexpect") if HERMES_PYTHON else None
pytestmark = [pytest.mark.skipif(not HERMES_PYTHON, reason="GE_HERMES_PYTHON not set (no Hermes interpreter)"),
              pytest.mark.skipif(os.name == "nt", reason="drives a pseudo terminal")]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scripted_model  # noqa: E402

SAFE_TASK = '1. Create `hello.txt` containing "HELLO REAL HERMES" 2. Summarize what was created'


@pytest.fixture
def model(tmp_path):
    log = tmp_path / "model.jsonl"
    server = scripted_model.serve(0, str(log))
    scripted_model._SLOWED.clear()
    yield server.server_address[1], log
    server.shutdown()


@pytest.fixture
def home(tmp_path, model):
    port, _log = model
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  base_url: http://127.0.0.1:%d/v1\n  default: scripted-model\n"
        "plugins:\n  enabled:\n  - hermes-graph-engineering\n" % port, encoding="utf-8")
    assert install_plugin.install(install_plugin.Layout(home), HERMES_PYTHON)["ok"]
    return home


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def hermes_bin():
    return str(Path(HERMES_PYTHON).parent / "hermes")


def env_for(home):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "HERMES_HOME")}
    env.update(HERMES_HOME=str(home), TERM="xterm-256color", COLUMNS="200", LINES="50")
    return env


class Session:
    """One interactive ``hermes --cli`` session in a pseudo terminal."""

    def __init__(self, home, workspace, transcript):
        self.child = pexpect.spawn(hermes_bin(), ["--cli"], cwd=str(workspace), env=env_for(home),
                                   encoding="utf-8", timeout=120, dimensions=(50, 200))
        self.child.logfile_read = open(transcript, "a", encoding="utf-8")
        self.child.expect("❯", timeout=90)
        time.sleep(1.0)

    def type(self, line):
        self.child.send(line)
        time.sleep(0.3)
        self.child.send("\r")

    def pump(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.child.read_nonblocking(100000, timeout=0.5)
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass

    def close(self):
        try:
            self.type("/exit")
            self.pump(2)
        finally:
            self.child.close(force=True)


def runs(home):
    return sorted(glob.glob(str(home / "plugin-data" / "*" / "runs" / "*")))


def state_of(run_dir):
    return json.loads(Path(run_dir, "state.json").read_text(encoding="utf-8"))["state"]


def wait_for(session, predicate, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        session.pump(1)
        value = predicate()
        if value:
            return value
    return None


def terminal_run(home):
    for run_dir in runs(home):
        state = state_of(run_dir)
        if state["status"] in ("SUCCEEDED", "FAILED", "CANCELLED") and \
                (state.get("finalization") or {}).get("status") == "COMPLETE":
            return run_dir
    return None


def trace(run_dir):
    return [json.loads(line) for line in Path(run_dir, "trace.jsonl").read_text(encoding="utf-8").splitlines()]


def model_rows(log):
    return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines()] if Path(log).exists() \
        else []


def test_ge_task_runs_autonomously_to_a_verified_receipt_in_real_hermes(home, workspace, model, tmp_path):
    _port, log = model
    session = Session(home, workspace, tmp_path / "tty.log")
    try:
        session.type("/ge " + SAFE_TASK)
        run_dir = wait_for(session, lambda: terminal_run(home))
    finally:
        session.close()
    assert run_dir, (tmp_path / "tty.log").read_text(encoding="utf-8")[-3000:]
    state = state_of(run_dir)
    receipt = json.loads(Path(run_dir, "receipt.json").read_text(encoding="utf-8"))
    assert state["status"] == "SUCCEEDED" and receipt["verified"] is True
    assert receipt["verification_level"] == "EVIDENCE" and receipt["terminal_outcome"] == "SUCCEEDED"
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "HELLO REAL HERMES\n"  # real write_file tool
    assert state["approvals"]["plan"]["decided_by"] == "policy:auto-approval"  # no operator step
    assert state["nodes"]["acceptance"]["evidence"][0]["passed"] is True
    workers = [e["worker"] for e in trace(run_dir) if e["type"] == "node.worker_finished" and (e.get("worker") or {})
               .get("child_session_id")]
    assert len(workers) == 2
    assert len({w["child_session_id"] for w in workers}) == 2  # a fresh Hermes session per node
    assert len({w["parent_session_id"] for w in workers}) == 1  # under the user's session
    assert all(w["toolsets"] == ["file"] and w["blocked_calls"] == [] for w in workers)
    rows = [r for r in model_rows(log) if "You are executing one node" in json.dumps(r["request"]["messages"])]
    first_turns = [r for r in rows if len(r["request"]["messages"]) == 2]
    assert len(first_turns) == 2  # each worker started with only [system, user]: no inherited conversation
    for row in first_turns:
        tools = {t["function"]["name"] for t in row["request"].get("tools") or []}
        assert tools <= {"read_file", "write_file", "patch", "search_files"}, tools  # narrowed to the risk class
        assert "/ge " not in json.dumps(row["request"]["messages"])


def test_risky_work_stops_for_the_operator_and_continues_after_approval_in_real_hermes(home, workspace, model,
                                                                                       tmp_path):
    session = Session(home, workspace, tmp_path / "tty.log")
    try:
        session.type("/ge 1. Summarize the release notes 2. Deploy the service to production")

        def held():
            for run_dir in runs(home):
                holds = state_of(run_dir)["holds"]
                if holds and holds[0]["kind"] == "WAITING_FOR_APPROVAL":
                    return run_dir
            return None

        run_dir = wait_for(session, held)
        assert run_dir, "no approval hold"
        state = state_of(run_dir)
        assert state["nodes"]["summarize_the_release_notes"]["status"] == "SUCCEEDED"
        assert state["nodes"]["deploy_the_service_to_production"]["attempts"] == 0  # stopped before it ran
        session.type("/ge-approve %s deploy_the_service_to_production:approval" % Path(run_dir).name)
        finished = wait_for(session, lambda: terminal_run(home))
    finally:
        session.close()
    assert finished and state_of(finished)["status"] == "SUCCEEDED"
    approval = state_of(finished)["approvals"]["deploy_the_service_to_production:approval"]
    assert approval["decided_by"] == "operator:slash-command"


def test_a_destructive_task_is_denied_before_anything_runs_in_real_hermes(home, workspace, model, tmp_path):
    _port, log = model
    session = Session(home, workspace, tmp_path / "tty.log")
    try:
        session.type("/ge Delete all files in the repository with rm -rf .")
        run_dir = wait_for(session, lambda: terminal_run(home), timeout=60)
    finally:
        session.close()
    state = state_of(run_dir)
    assert state["terminal_outcome"] == "POLICY_DENIED" and state["status"] == "CANCELLED"
    assert not [r for r in model_rows(log) if "You are executing one node" in json.dumps(r["request"]["messages"])]


def test_kill_9_of_hermes_mid_worker_then_restart_completes_the_run(home, workspace, model, tmp_path):
    session = Session(home, workspace, tmp_path / "tty.log")
    session.type('/ge 1. Create `slow.txt` containing "SURVIVED" (slow) 2. Summarize what was created')
    started = wait_for(session, lambda: [r for r in runs(home) if any(
        e["type"] == "node.dispatched" and (e.get("owner") or "") for e in trace(r) if e.get("attempt") == 1
    ) and any(e["type"] == "lease.acquired" for e in trace(r)) and state_of(r)["autopilot"]["usage"]["drive_calls"]
        >= 2], timeout=90)
    assert started, "the worker never started"
    run_dir = started[0]
    time.sleep(2)
    os.kill(session.child.pid, signal.SIGKILL)  # the whole Hermes process dies while its worker waits
    session.child.close(force=True)
    assert Path(run_dir, "lease.json").exists()  # the dead owner could not release it
    restarted = Session(home, workspace, tmp_path / "tty2.log")
    try:
        restarted.type("/ge-auto %s" % Path(run_dir).name)
        finished = wait_for(restarted, lambda: terminal_run(home))
    finally:
        restarted.close()
    assert finished == run_dir
    state = state_of(run_dir)
    assert state["status"] == "SUCCEEDED" and (workspace / "slow.txt").read_text(encoding="utf-8") == "SURVIVED\n"
    events = trace(run_dir)
    takeovers = [e["took_over"] for e in events if e["type"] == "lease.acquired" and e.get("took_over")]
    assert takeovers and takeovers[0]["reason"] == "expired"
    assert "abandoned_claim_reclaimed" in [e.get("reason") for e in events if e["type"] == "node.dispatch_released"]
    receipt = json.loads(Path(run_dir, "receipt.json").read_text(encoding="utf-8"))
    assert receipt["verified"] is True


def test_headless_hermes_ge_run_needs_no_session(home, workspace, model):
    env = env_for(home)
    proc = subprocess.run([hermes_bin(), "ge", "run", SAFE_TASK, "--workspace", str(workspace)], env=env,
                          capture_output=True, text=True, timeout=300, cwd=str(workspace))
    report = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert report["status"] == "TERMINAL" and report["final_report"]["verified"] is True
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "HELLO REAL HERMES\n"
