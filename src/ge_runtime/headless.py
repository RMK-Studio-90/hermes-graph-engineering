"""Headless runner: drive autopilot runs without any chat host (CI, cron, containers, cloud).

::

    python -m ge_runtime.headless --root DIR start --workspace DIR --task "..." [--worker-command CMD]
    python -m ge_runtime.headless --root DIR drive RUN_ID [--worker-command CMD]
    python -m ge_runtime.headless --root DIR status RUN_ID
    python -m ge_runtime.headless --root DIR recover RUN_ID        (complete an interrupted finalization)

Agent nodes run through ``CommandWorker``: every node attempt is a *new process* (for example
``hermes -z {prompt} -t {toolsets}``), so each worker starts from a fresh context that contains
only the node's goal and explicit inputs. The worker process gets ``GE_WORKER_RUN``,
``GE_WORKER_NODE`` and ``GE_WORKER_RISK`` in its environment, which the Hermes plugin uses to
restrict it like an in-process worker.

Exit codes: 0 finished and verified, 1 finished but not verified, 2 an operator decision is
required, 3 agent work is waiting and no worker is configured, 4 error. The JSON report is
printed on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters.types import ExecutorOutcome, ExecutorResult
from .autopilot import CONTINUE, NEEDS_AGENT_TURN, NEEDS_OPERATOR, TERMINAL, Autopilot, AutopilotBudget
from .dispatch import WorkerContext, clip, extract_outputs
from .engine import GraphEngine
from .errors import GraphEngineeringError
from .evidence import EvidenceRunner
from .policy import PolicyConfig, RiskClass
from .spec import load_spec_text
from .store import RunStore

TAIL = 2000


class CommandWorker:
    """Runs each node attempt as a separate process; placeholders: ``{prompt}``, ``{toolsets}``.

    Without ``{prompt}`` in the command the prompt is written to the process's stdin.
    """

    def __init__(self, command: str | Sequence[str], *, cwd: str | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self.argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.argv:
            raise ValueError("worker command is empty")
        self.cwd = cwd
        self.env = dict(env or {})

    def supports_agent_execution(self) -> bool:
        head = self.argv[0]
        return Path(head).is_file() or shutil.which(head) is not None

    def execute_agent_work(self, work_order: Mapping[str, Any], context: WorkerContext) -> ExecutorResult:
        prompt = context.goal + "\n\n" + context.context
        toolsets = ",".join(context.allowed_toolsets or ())
        uses_prompt = any("{prompt}" in part for part in self.argv)
        argv = []
        for part in self.argv:
            if part == "{toolsets}" and not toolsets:
                if argv and argv[-1] in ("-t", "--toolsets"):
                    argv.pop()  # no restriction to pass: drop the flag instead of an empty value
                continue
            argv.append(part.replace("{prompt}", prompt).replace("{toolsets}", toolsets))
        env = dict(os.environ)
        env.update(self.env)
        env.update(GE_WORKER_RUN=context.run_id, GE_WORKER_NODE=context.node_id, GE_WORKER_ATTEMPT=str(context.attempt),
                   GE_WORKER_RISK=context.risk or "", GE_WORKER_CORRELATION=context.correlation_id)
        try:
            proc = subprocess.run(argv, input=None if uses_prompt else prompt, capture_output=True, text=True,
                                  timeout=context.timeout_seconds, cwd=self.cwd, env=env,
                                  stdin=subprocess.DEVNULL if uses_prompt else None)
        except subprocess.TimeoutExpired:
            return ExecutorResult(ExecutorOutcome.TIMEOUT, detail="worker process exceeded %ds" % context.timeout_seconds)
        except OSError as exc:
            return ExecutorResult(ExecutorOutcome.EXECUTOR_UNAVAILABLE,
                                  detail="worker process could not start (%s)" % type(exc).__name__)
        if proc.returncode != 0:
            return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="worker exited %d: %s" % (
                proc.returncode, clip(proc.stderr[-TAIL:] or proc.stdout[-TAIL:], 400)))
        outputs, why = extract_outputs(proc.stdout, work_order["outputs"])
        if outputs is None:
            return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="host returned invalid result: " + why)
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs=outputs)


def _toolsets_map(value: str | None) -> Any:
    if not value:
        return None
    mapping = json.loads(value)

    def lookup(risk: str | None) -> tuple[str, ...] | None:
        entry = mapping.get(risk or "READ_ONLY")
        return tuple(entry) if isinstance(entry, list) else None

    return lookup


def build_autopilot(root: str, *, worker_command: str | None = None, workspace: str | None = None,
                    policy: PolicyConfig | None = None, budget: AutopilotBudget | None = None,
                    toolsets_for: Any = None, node_timeout_seconds: int = 3600) -> Autopilot:
    store = RunStore(root)
    engine = GraphEngine(store, require_plan_approval=True, evidence=EvidenceRunner(store, exclude=root))
    worker = CommandWorker(worker_command, cwd=workspace) if worker_command else None
    return Autopilot(engine, worker, policy=policy, budget=budget, state_key="headless:%s" % Path(root).resolve(),
                     toolsets_for=toolsets_for, node_timeout_seconds=node_timeout_seconds)


def exit_code(report: Mapping[str, Any]) -> int:
    status = report.get("status")
    if status == TERMINAL:
        return 0 if (report.get("final_report") or {}).get("verified") else 1
    if status == NEEDS_OPERATOR:
        return 2
    if status == NEEDS_AGENT_TURN:
        return 3
    return 4


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ge-headless", description="Drive Graph Engineering runs without a chat host.")
    parser.add_argument("--root", required=True, help="state directory (runs/ lives below it)")
    parser.add_argument("--worker-command", default=os.environ.get("GE_WORKER_COMMAND"),
                        help="command per agent node attempt, e.g. 'hermes -z {prompt} -t {toolsets}'")
    parser.add_argument("--toolsets-by-risk", default=None,
                        help='JSON map risk class -> toolsets passed as {toolsets}, e.g. {"READ_ONLY": ["file"]}')
    parser.add_argument("--auto-approve-max-risk", default="LOCAL_EXEC", choices=[r.value for r in RiskClass])
    parser.add_argument("--max-calls", type=int, default=1000)
    parser.add_argument("--call-budget-seconds", type=float, default=300.0)
    parser.add_argument("--node-timeout-seconds", type=int, default=3600)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--workspace", required=True)
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("--task")
    source.add_argument("--spec", help="path to a JSON/YAML spec")
    start.add_argument("--no-drive", action="store_true", help="only plan and apply the policy")
    for name in ("drive", "status", "recover"):
        sub.add_parser(name).add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        workspace = str(Path(args.workspace).resolve()) if args.command == "start" else None
        policy = PolicyConfig(auto_approve_max=RiskClass(args.auto_approve_max_risk))
        pilot = build_autopilot(args.root, worker_command=args.worker_command, workspace=workspace, policy=policy,
                                toolsets_for=_toolsets_map(args.toolsets_by_risk),
                                node_timeout_seconds=args.node_timeout_seconds)
        if args.command == "start":
            spec = load_spec_text(Path(args.spec).read_text(encoding="utf-8")) if args.spec else None
            run_id = pilot.start(task=args.task if spec is None else None, spec=spec, workspace=workspace,
                                 created_by="headless", origin={"via": "headless"})
            if args.no_drive:
                print(json.dumps({"run_id": run_id, "status": "PLANNED"}))
                return 0
        else:
            run_id = args.run_id
        state, _spec = pilot.engine.load(run_id)
        if pilot.worker is not None and state.get("workspace"):
            pilot.worker.cwd = state["workspace"]
        if args.command == "status":
            print(json.dumps(pilot.engine.status(run_id), indent=2, sort_keys=True))
            return 0
        if args.command == "recover":
            receipt = pilot.engine.ensure_finalized(run_id)
            print(json.dumps({"run_id": run_id, "receipt": receipt}, indent=2, sort_keys=True))
            return 0 if receipt is not None else 4
        report = pilot.run_to_completion(run_id, max_calls=args.max_calls, call_budget_seconds=args.call_budget_seconds)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return exit_code(report)
    except GraphEngineeringError as exc:
        print(json.dumps(exc.to_dict()))
        return 4


if __name__ == "__main__":
    sys.exit(main())
