"""Headless runner: the autopilot without any chat host, as CI/cron/containers use it.
Every agent node runs as its own process (a fresh context); crashes of the runner are recovered."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
WORKER = Path(__file__).resolve().parents[1] / "support" / "scripted_worker.py"
TASK = '1. Create `hello.txt` containing "HELLO HEADLESS" 2. Summarize the result'
TOOLSETS = json.dumps({"READ_ONLY": ["file"], "WORKSPACE_WRITE": ["file"], "LOCAL_EXEC": ["file", "terminal"]})


def headless(root, *args, env=None, wait=True):
    environment = dict(os.environ, PYTHONPATH=str(SRC), **(env or {}))
    command = [sys.executable, "-m", "ge_runtime.headless", "--root", str(root),
               "--worker-command", "%s %s {prompt} --toolsets {toolsets}" % (sys.executable, WORKER),
               "--toolsets-by-risk", TOOLSETS, *args]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    if not wait:
        return proc
    out, err = proc.communicate(timeout=120)
    return proc.returncode, json.loads(out) if out.strip() else err


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_a_task_runs_headless_to_a_verified_result(tmp_path, workspace):
    log = tmp_path / "workers.log"
    code, report = headless(tmp_path / "state", "start", "--workspace", str(workspace), "--task", TASK,
                            env={"GE_TEST_WORKER_LOG": str(log)})
    assert code == 0, report
    assert report["status"] == "TERMINAL" and report["final_report"]["verified"] is True
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "HELLO HEADLESS\n"
    workers = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(workers) == 2 and workers[0][0] != workers[1][0]  # one fresh process per node
    assert workers[0][2] == "file" and workers[0][3] == "WORKSPACE_WRITE"  # rights follow the risk class


def test_without_a_worker_the_runner_reports_waiting_agent_work(tmp_path, workspace):
    environment = dict(os.environ, PYTHONPATH=str(SRC))
    proc = subprocess.run([sys.executable, "-m", "ge_runtime.headless", "--root", str(tmp_path / "s"), "start",
                           "--workspace", str(workspace), "--task", TASK], capture_output=True, text=True,
                          env=environment, timeout=60)
    assert proc.returncode == 3 and json.loads(proc.stdout)["status"] == "NEEDS_AGENT_TURN"


def test_an_operator_hold_is_exit_code_2(tmp_path, workspace):
    code, report = headless(tmp_path / "state", "start", "--workspace", str(workspace), "--task",
                            "1. Summarize the notes 2. Deploy the service to production")
    assert code == 2 and report["status"] == "NEEDS_OPERATOR"


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals")
def test_kill_9_of_the_runner_is_recovered_by_the_next_invocation(tmp_path, workspace):
    state = tmp_path / "state"
    log = tmp_path / "workers.log"
    code, planned = headless(state, "start", "--workspace", str(workspace), "--task", TASK, "--no-drive")
    run_id = planned["run_id"]
    victim = headless(state, "drive", run_id, env={"GE_TEST_WORKER_SLEEP": "30", "GE_TEST_WORKER_LOG": str(log)},
                      wait=False)
    deadline = time.time() + 30
    while time.time() < deadline and not (log.exists() and log.read_text(encoding="utf-8").strip()):
        time.sleep(0.05)
    os.kill(victim.pid, signal.SIGKILL)  # the runner dies mid-dispatch (its worker process is orphaned)
    victim.wait(timeout=10)
    code, report = headless(state, "drive", run_id, env={"GE_TEST_WORKER_LOG": str(log)})
    assert code == 0 and report["final_report"]["verified"] is True, report
    code, status = headless(state, "status", run_id)
    assert status["status"] == "SUCCEEDED" and status["integrity"]["consistent"] is True
    code, recovered = headless(state, "recover", run_id)
    assert code == 0 and recovered["receipt"]["verified"] is True
