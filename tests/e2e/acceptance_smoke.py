"""GE2 acceptance smoke against a real Hermes installation.

    GE_HERMES_PYTHON=<hermes-python> python tests/e2e/acceptance_smoke.py [--out report.json]

Real: Hermes CLI (pseudo terminal), plugin loader, /ge dispatch, inject_message, agent loop,
tool-search bridge, subagent lifecycle, file tools, hooks, subprocess evidence, files, journal.
Scripted: only the language model (``scripted_model``), because no model credentials exist here.
Prints a JSON report: autonomous end-to-end trace, evidence of a verified run, receipt and
recovery, policy matrix, crash recovery of the headless runner.
"""
import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "scripts"), str(ROOT / "src")]

import pexpect  # noqa: E402

import install_plugin  # noqa: E402
import scripted_model  # noqa: E402
from ge_runtime.policy import PolicyConfig, evaluate_plan  # noqa: E402
from ge_runtime.spec import admit  # noqa: E402

HERMES_PYTHON = os.environ["GE_HERMES_PYTHON"]
HERMES = str(Path(HERMES_PYTHON).parent / "hermes")
CHECK = "import pathlib,sys; sys.exit(0 if pathlib.Path('hello.txt').read_text().strip() == 'HELLO GE2' else 1)"
TASK = ('1. Create `hello.txt` containing "HELLO GE2" 2. Verify it with `python3 -c "%s"`' % CHECK)
MATRIX = ["Summarize the findings", "Implement the parser in src/parser.py", "Run the unit tests with pytest",
          "Download https://example.org/data.csv", "Deploy the service to production", "Send an email to the team",
          "Purchase a license", "Delete all files with rm -rf build"]


def env_for(home):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "HERMES_HOME")}
    env.update(HERMES_HOME=str(home), TERM="xterm-256color")
    return env


def state_of(run_dir):
    return json.loads(Path(run_dir, "state.json").read_text(encoding="utf-8"))["state"]


def trace(run_dir):
    return [json.loads(line) for line in Path(run_dir, "trace.jsonl").read_text(encoding="utf-8").splitlines()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="ge2-acceptance-"))
    log = tmp / "model.jsonl"
    server = scripted_model.serve(0, str(log))
    home, ws = tmp / "home", tmp / "workspace"
    home.mkdir()
    ws.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  base_url: http://127.0.0.1:%d/v1\n  default: scripted-model\n"
        "plugins:\n  enabled:\n  - hermes-graph-engineering\n" % server.server_address[1], encoding="utf-8")
    report = {"hermes_version": subprocess.run([HERMES, "--version"], capture_output=True, text=True,
                                               env=env_for(home)).stdout.strip().splitlines()[:1],
              "install": install_plugin.install(install_plugin.Layout(home), HERMES_PYTHON)["ok"]}

    # 1. /ge <task> in the interactive CLI
    child = pexpect.spawn(HERMES, ["--cli"], cwd=str(ws), env=env_for(home), encoding="utf-8", timeout=120,
                          dimensions=(50, 200))
    child.logfile_read = open(tmp / "tty.log", "w", encoding="utf-8")
    child.expect("❯", timeout=90)
    time.sleep(1)
    started = time.time()
    child.send("/ge " + TASK)
    time.sleep(0.3)
    child.send("\r")
    run_dir = None
    while time.time() - started < 180:
        try:
            child.read_nonblocking(100000, timeout=1)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass
        found = glob.glob(str(home / "plugin-data" / "*" / "runs" / "*"))
        if found and (state_of(found[0]).get("finalization") or {}).get("status") == "COMPLETE":
            run_dir = found[0]
            break
    child.send("/exit\r")
    time.sleep(2)
    child.close(force=True)
    state = state_of(run_dir)
    receipt = json.loads(Path(run_dir, "receipt.json").read_text(encoding="utf-8"))
    events = trace(run_dir)
    command = next(e for e in state["nodes"]["acceptance"]["evidence"] if e["type"] == "command")
    artifact = json.loads(Path(run_dir, "artifacts", command["artifact"] + ".json").read_text(encoding="utf-8"))
    command_result = next(r for r in artifact["payload"]["results"] if r["type"] == "command")
    workers = [e for e in events if e["type"] == "node.worker_finished" and (e.get("worker") or {}).get(
        "child_session_id")]
    worker_rows = [r for r in (json.loads(x) for x in log.read_text(encoding="utf-8").splitlines())
                   if "You are executing one node" in json.dumps(r["request"]["messages"])]
    report["autonomous_e2e"] = {
        "task": TASK, "run_id": state["run_id"], "seconds": round(time.time() - started, 1),
        "status": state["status"], "terminal_outcome": state["terminal_outcome"],
        "plan_approved_by": state["approvals"]["plan"]["decided_by"],
        "policy": {k: state["policy"][k] for k in ("plan_decision", "max_risk")},
        "operator_actions": 0, "file": (ws / "hello.txt").read_text(encoding="utf-8"),
        "phases": [h["phase"] for h in state["autopilot"]["history"]],
        "usage": state["autopilot"]["usage"], "continued_turns": state["autopilot"].get("continued_turns"),
        "trace": [{"seq": e["seq"], "type": e["type"], **{k: e[k] for k in ("node", "attempt", "reason", "phase",
                                                                               "outcome", "verified") if k in e}}
                  for e in events if e["type"] not in ("state.saved",)],
    }
    report["fresh_worker_contexts"] = {
        "workers": [{"node": e["node"], "child_session": e["worker"]["child_session_id"],
                     "parent_session": e["worker"]["parent_session_id"], "risk": e["worker"]["risk"],
                     "toolsets": e["worker"]["toolsets"], "context_digest": e["context_digest"]} for e in workers],
        "first_request_message_roles": [[m["role"] for m in r["request"]["messages"]] for r in worker_rows
                                        if len(r["request"]["messages"]) == 2],
        "worker_tools": sorted({t["function"]["name"] for r in worker_rows for t in r["request"].get("tools") or []}),
    }
    report["evidence_verified_run"] = {
        "receipt_verified": receipt["verified"], "verification_level": receipt["verification_level"],
        "failed_checks": [c["check"] for c in receipt["checks"] if not c["passed"]],
        "checks": [c["check"] for c in receipt["checks"]],
        "acceptance_evidence": state["nodes"]["acceptance"]["evidence"],
        "command": command_result["spec"]["argv"][:2] + ["..."], "exit_code": command_result["exit_code"],
        "stdout_sha256": command_result["stdout_sha256"], "artifact_digest": command["digest"],
        "receipt_checksum": receipt["checksum"],
    }

    # 2. receipt + recovery through the real CLI command
    receipt_path = Path(run_dir, "receipt.json")
    receipt_path.unlink()
    lost = subprocess.run([HERMES, "ge", "recover", state["run_id"]], capture_output=True, text=True,
                          env=env_for(home), cwd=str(ws), timeout=120)
    recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered["verified"] = False
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    fixed = subprocess.run([HERMES, "ge", "recover", state["run_id"]], capture_output=True, text=True,
                           env=env_for(home), cwd=str(ws), timeout=120)
    after = json.loads(receipt_path.read_text(encoding="utf-8"))
    report["receipt_recovery"] = {
        "deleted_then_recover_exit": lost.returncode, "recovered_generated_by": recovered["generated_by"],
        "recovered_verified": recovered["verified"],
        "tampered_then_recover_exit": fixed.returncode, "after_tamper_verified": after["verified"],
        "recovery_log": [r["kind"] for r in state_of(run_dir)["recovery"]],
        "checks_identical": [c["check"] for c in after["checks"]] == [c["check"] for c in receipt["checks"]],
    }

    # 3. policy matrix
    matrix = []
    for purpose in MATRIX:
        spec = admit({"schema_version": 1, "graph_id": "m", "title": "m", "inputs": {}, "nodes": [
            {"id": "n", "purpose": purpose, "executor": "agent", "outputs": {"r": {"type": "string"}}}]})
        decision = evaluate_plan(spec, PolicyConfig())["decisions"]["n"]
        matrix.append({"purpose": purpose, "risk": decision["risk"], "decision": decision["decision"]})
    report["policy_matrix"] = matrix

    # 4. headless crash: kill -9 the runner mid-worker, restart, finish
    state_root = tmp / "headless"
    worker = "%s %s {prompt} --toolsets {toolsets}" % (sys.executable, ROOT / "tests" / "support" / "scripted_worker.py")
    base = [sys.executable, "-m", "ge_runtime.headless", "--root", str(state_root), "--worker-command", worker]
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    hws = tmp / "headless-ws"
    hws.mkdir()
    planned = json.loads(subprocess.run(base + ["start", "--workspace", str(hws), "--task",
                                                '1. Create `a.txt` containing "A" 2. Summarize it', "--no-drive"],
                                        capture_output=True, text=True, env=env, timeout=60).stdout)
    wlog = tmp / "workers.log"
    victim = subprocess.Popen(base + ["drive", planned["run_id"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(env, GE_TEST_WORKER_SLEEP="30", GE_TEST_WORKER_LOG=str(wlog)))
    while not (wlog.exists() and wlog.read_text().strip()):
        time.sleep(0.05)
    os.kill(victim.pid, signal.SIGKILL)
    victim.wait()
    resumed = subprocess.run(base + ["drive", planned["run_id"]], capture_output=True, text=True,
                             env=dict(env, GE_TEST_WORKER_LOG=str(wlog)), timeout=120)
    final = json.loads(resumed.stdout)
    hevents = trace(next(iter(glob.glob(str(state_root / "runs" / "*")))))
    report["headless_kill_9"] = {
        "exit_code": resumed.returncode, "verified": final["final_report"]["verified"],
        "worker_processes": len(wlog.read_text().splitlines()),
        "lease_takeover": [e.get("took_over") for e in hevents if e["type"] == "lease.acquired" and e.get("took_over")],
        "reclaimed": [e["reason"] for e in hevents if e["type"] == "node.dispatch_released"],
    }
    server.shutdown()
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
