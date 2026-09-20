"""Autonomous dispatch: let the running host execute waiting ``agent`` nodes.

Graph Engineering defines *what* work a node needs (purpose, instructions,
inputs, capabilities, output contract, success criteria). The host decides *how*
it is done: model, provider, routing and tool policy are the host's business and
never appear here.

Lifecycle of one dispatched node::

    engine: node RUNNING + wait=submission (persisted before anything else)
      -> claim   trace ``node.dispatched`` (write-ahead, durable)
      -> host    HostAgentExecutor.execute_agent_work(work_order, context)
      -> submit  GraphEngine.submit(...)   (contract + success criteria enforced there)

The dispatcher is a worker, never an operator: it only reads runs and calls
``GraphEngine.submit``. It cannot approve plans or gates, authorize retries or
cancel runs, and it stops at every approval, review and attention hold.

Recovery needs no dispatcher state beyond the run trace: an attempt that has a
claim without a release was (possibly) started by a process that died. Nodes that
are idempotent and side-effect free are dispatched again; any other node is held
for the operator instead of being replayed.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.engine import GraphEngine, pending_work
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.states import RUN_TERMINAL, RunStatus

from .guard import WorkerScope, current_worker, exclusive_dispatch, worker_scope

SUBMITTER = "agent:host-autonomous"
DISPATCHER = "host-agent"
MAX_GOAL_CHARS = 15_000
MAX_CONTEXT_CHARS = 30_000
MAX_DETAIL_CHARS = 500
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.S)

# Stable, provider-neutral stop reasons reported by ``AutonomousDispatcher.drive``.
STOP_RUN_FINISHED = "run_finished"
STOP_NO_PENDING_WORK = "no_pending_work"
STOP_PLAN_NOT_APPROVED = "plan_not_approved"
STOP_HOST_UNAVAILABLE = "host_unavailable"
STOP_HOST_DECLINED = "host_declined"
STOP_INTERRUPTED = "interrupted_dispatch"
STOP_BUDGET = "budget_exhausted"
STOP_TIME_BUDGET = "time_budget_exhausted"
STOP_IN_PROGRESS = "dispatch_in_progress"
STOP_STATE_CHANGED = "state_changed"
STOP_WORKER_CONTEXT = "worker_context"


@dataclass(frozen=True)
class WorkerContext:
    """Bounded, host-neutral description of one node attempt handed to the host."""

    run_id: str
    graph_id: str
    node_id: str
    attempt: int
    attempt_id: str
    execution_id: str
    correlation_id: str
    goal: str
    context: str
    timeout_seconds: int


@runtime_checkable
class HostAgentExecutor(Protocol):
    """What a host must offer to run one agent node. Capabilities, not providers."""

    def supports_agent_execution(self) -> bool:
        """True if this host can execute agent work at all (static capability)."""

    def execute_agent_work(self, work_order: Mapping[str, Any], context: WorkerContext) -> ExecutorResult:
        """Perform the node's work and return a normalized result.

        ``EXECUTOR_UNAVAILABLE`` means nothing was started; ``CANCELLED`` and
        ``GOVERNANCE_BLOCKED`` mean the host declined or stopped; the failure
        outcomes mean the work was attempted and failed.
        """


# ---------------------------------------------------------------------- task text
def clip(text: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_outputs(outputs: Mapping[str, Mapping[str, Any]]) -> str:
    if not outputs:
        return "- (no outputs declared; return an empty JSON object {})"
    return "\n".join("- %s: %s%s" % (name, spec.get("type", "any"), "" if spec.get("required", True) else " (optional)")
                     for name, spec in outputs.items())


def build_worker_context(state: Mapping[str, Any], order: Mapping[str, Any], *, dispatch: int,
                         timeout_seconds: int) -> WorkerContext:
    """Render the work order into a bounded task. The text is guidance; the contracts enforce."""
    run_id, node_id, attempt = order["run_id"], order["node"], order["attempt"]
    goal = "\n".join([
        "You are executing one node from an approved Graph Engineering run.",
        "Execute only this node. Do not modify the graph, approve gates, or start, run, resume or submit any "
        "graph (do not call the ge_graph tool to change anything), and do not perform unrelated work.",
        "",
        "Node: %s (%s) - attempt %s of at most %s" % (node_id, order["name"], attempt, order["max_attempts"]),
        "",
        "Purpose:",
        str(order["purpose"]),
        "",
        "Instructions:",
        str(order["instructions"] or "(none beyond the purpose)"),
        "",
        "Required capabilities: %s" % (", ".join(order["requires"]) or "(none declared)"),
        "",
        "Required output contract - a JSON object with exactly these keys:",
        _describe_outputs(order["outputs"]),
        "",
        "Success criteria (checked automatically after you answer; a failed check fails the attempt):",
        json.dumps(order["success_criteria"], sort_keys=True) if order["success_criteria"] else "(none declared)",
        "",
        "Reply format: end your final message with ONE JSON object holding only the declared outputs, in a "
        "```json fenced block. If you cannot complete the node, say so plainly instead of inventing outputs.",
    ])
    inputs = order["inputs"] or {}
    context = ("Inputs for this node (JSON):\n" + json.dumps(inputs, sort_keys=True, ensure_ascii=False)) if inputs \
        else "This node has no inputs."
    if len(goal) > MAX_GOAL_CHARS or len(context) > MAX_CONTEXT_CHARS:
        raise GraphEngineeringError(ErrorCode.HOST_EXECUTION_FAILED,
                                    "node %s work order is too large to hand to the host" % node_id)
    execution_id = "%s:%s:%d" % (run_id, node_id, attempt)
    return WorkerContext(
        run_id=run_id, graph_id=str(state["graph_id"]), node_id=node_id, attempt=attempt,
        attempt_id="%s:%d" % (node_id, attempt), execution_id=execution_id,
        correlation_id="ge:%s:%d" % (execution_id, dispatch), goal=goal, context=context,
        timeout_seconds=timeout_seconds)


# ---------------------------------------------------------------------- outputs
def _json_candidates(text: str) -> list[Any]:
    """Whole text, fenced blocks (last first), then JSON objects embedded in prose (last first)."""
    found: list[Any] = []

    def add(chunk: str) -> None:
        try:
            found.append(json.loads(chunk))
        except ValueError:
            pass

    add(text.strip())
    for block in reversed(_FENCE.findall(text)):
        add(block.strip())
    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", text)][-40:]
    for start in reversed(starts):
        try:
            value, _end = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        found.append(value)
    return found


def extract_outputs(payload: Any, declared: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Best-effort conservative extraction of the node outputs from a host reply.

    Returns ``(outputs, "")`` or ``(None, reason)``. Nothing is repaired or guessed: the
    object is passed on as found and ``GraphEngine.submit`` decides whether it satisfies
    the contract.
    """
    structured = isinstance(payload, Mapping)
    if structured:
        candidates: list[Any] = [payload]
    elif isinstance(payload, str) and payload.strip():
        candidates = _json_candidates(payload)
    else:
        return None, "empty reply"
    for value in candidates:
        if not isinstance(value, Mapping):
            continue
        if "outputs" not in declared and isinstance(value.get("outputs"), Mapping):
            value = value["outputs"]
        if not structured and declared and not set(value) & set(declared):
            continue  # prose can embed unrelated JSON; only accept an object that names a declared output
        return dict(value), ""
    return None, "no JSON object with the declared outputs found in the reply"


# ---------------------------------------------------------------------- dispatcher
class AutonomousDispatcher:
    """Sequentially executes waiting agent nodes of one run through a ``HostAgentExecutor``."""

    def __init__(self, engine: GraphEngine, executor: HostAgentExecutor, *, state_key: str,
                 max_dispatches: int | None = None, node_timeout_seconds: int = 3600,
                 call_budget_seconds: float | None = None) -> None:
        self.engine = engine
        self.executor = executor
        self.state_key = state_key
        self.max_dispatches = engine.max_steps if max_dispatches is None else max_dispatches
        self.node_timeout_seconds = node_timeout_seconds
        self.call_budget_seconds = call_budget_seconds  # None: unbounded

    # -- public -----------------------------------------------------------------
    def drive(self, run_id: str) -> dict[str, Any]:
        report: dict[str, Any] = {"enabled": True, "supported": True, "dispatched": []}
        if current_worker() is not None:
            return self._stop(report, STOP_WORKER_CONTEXT, ErrorCode.WORKER_CONTEXT_RESTRICTED,
                              "a node worker cannot drive runs")
        with exclusive_dispatch(self.state_key, run_id) as acquired:
            if not acquired:
                return self._stop(report, STOP_IN_PROGRESS, ErrorCode.DISPATCH_IN_PROGRESS,
                                  "the host is already executing agent nodes for this profile; try again when it "
                                  "stops, or submit manually")
            return self._drive_locked(run_id, report)

    # -- internals --------------------------------------------------------------
    @staticmethod
    def _stop(report: dict[str, Any], reason: str, code: ErrorCode | None = None, detail: str = "",
              **extra: Any) -> dict[str, Any]:
        report["stopped"] = reason
        if code is not None:
            report["error"] = code.value
        if detail:
            report["message"] = detail
        report.update(extra)
        return report

    def _drive_locked(self, run_id: str, report: dict[str, Any]) -> dict[str, Any]:
        dispatches = 0
        started = time.monotonic()
        while True:
            state, spec = self.engine.load(run_id)
            status = RunStatus(state["status"])
            if status in RUN_TERMINAL:
                return self._stop(report, STOP_RUN_FINISHED, status=status.value)
            if status is RunStatus.DRAFT:
                return self._stop(report, STOP_PLAN_NOT_APPROVED)
            orders = pending_work(state, spec)
            if not orders:
                return self._stop(report, STOP_NO_PENDING_WORK, holds=state["holds"])
            if dispatches >= self.max_dispatches:
                return self._stop(report, STOP_BUDGET, detail="step budget reached; resume to continue")
            if dispatches and self.call_budget_seconds is not None \
                    and time.monotonic() - started >= self.call_budget_seconds:
                return self._stop(report, STOP_TIME_BUDGET,
                                  detail="time budget for this call reached; call resume to continue")
            order = orders[0]
            node = spec.node(order["node"])
            claimed = self._claim(run_id, node, order)
            if claimed is None:
                return self._stop(report, STOP_STATE_CHANGED)
            if claimed == 0:
                return self._stop(
                    report, STOP_INTERRUPTED, ErrorCode.DISPATCH_INTERRUPTED,
                    "node %s was dispatched before and never finished; it may have partly run and is not "
                    "idempotent, so it is not replayed. Submit its outcome manually or cancel the run."
                    % order["node"], node=order["node"])
            dispatches += 1
            outcome = self._execute_and_submit(run_id, state, order, claimed, report)
            if outcome is not None:
                return outcome

    def _claim(self, run_id: str, node: Mapping[str, Any], order: Mapping[str, Any]) -> int | None:
        """Write-ahead claim. Returns the dispatch number (>=1), 0 if a replay is unsafe, None if state moved."""
        node_id, attempt = order["node"], order["attempt"]
        with self.engine.store.lock(run_id):
            state, _spec = self.engine.load(run_id)
            ns = state["nodes"][node_id]
            if ns["status"] != "RUNNING" or ns["wait"] != "submission" or ns["attempts"] != attempt:
                return None
            events = [e for e in self.engine.store.read_trace(run_id)
                      if e.get("node") == node_id and e.get("attempt") == attempt]
            claims = sum(1 for e in events if e.get("type") == "node.dispatched")
            released = sum(1 for e in events if e.get("type") == "node.dispatch_released")
            replay_safe = bool(node["idempotent"]) and not node["side_effects"]
            if claims > released and not replay_safe:
                return 0
            self.engine.store.append_trace(run_id, "node.dispatched", node=node_id, attempt=attempt,
                                           dispatch=claims + 1, dispatcher=DISPATCHER)
            return claims + 1

    def _release(self, run_id: str, order: Mapping[str, Any], dispatch: int, reason: str) -> None:
        with self.engine.store.lock(run_id):
            self.engine.store.append_trace(run_id, "node.dispatch_released", node=order["node"],
                                           attempt=order["attempt"], dispatch=dispatch, reason=reason)

    def _execute_and_submit(self, run_id: str, state: Mapping[str, Any], order: Mapping[str, Any], dispatch: int,
                            report: dict[str, Any]) -> dict[str, Any] | None:
        """Run one node on the host and submit. Returns a final report to stop, or None to continue."""
        node_id, attempt = order["node"], order["attempt"]
        entry: dict[str, Any] = {"node": node_id, "attempt": attempt}
        report["dispatched"].append(entry)
        try:
            context = build_worker_context(state, order, dispatch=dispatch, timeout_seconds=self.node_timeout_seconds)
            with worker_scope(WorkerScope(run_id, node_id)):
                result = self.executor.execute_agent_work(order, context)
            if not isinstance(result, ExecutorResult):
                result = ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="host returned an unrecognized result")
        except GraphEngineeringError as exc:
            result = ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail=exc.message)
        except Exception as exc:  # a host bug fails the attempt, never the process
            result = ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE,
                                    detail="host execution failed (%s)" % type(exc).__name__)
        outcome = result.outcome
        entry["outcome"] = outcome.value

        if outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE:
            self._release(run_id, order, dispatch, "host unavailable")
            entry["submitted"] = False
            return self._stop(report, STOP_HOST_UNAVAILABLE, ErrorCode.HOST_EXECUTION_UNAVAILABLE,
                              clip(result.detail) or "host agent execution unavailable")
        if outcome in (ExecutorOutcome.CANCELLED, ExecutorOutcome.GOVERNANCE_BLOCKED):
            entry["submitted"] = False
            return self._stop(report, STOP_HOST_DECLINED, ErrorCode.HOST_EXECUTION_FAILED,
                              clip(result.detail) or "host stopped before finishing the node; node left waiting")

        try:
            self._submit(run_id, order, result, entry)
        except GraphEngineeringError as exc:
            if exc.code is ErrorCode.INVALID_TRANSITION:
                entry["submitted"] = False
                return self._stop(report, STOP_STATE_CHANGED, exc.code, exc.message)
            raise
        return None

    def _submit(self, run_id: str, order: Mapping[str, Any], result: ExecutorResult, entry: dict[str, Any]) -> None:
        node_id = order["node"]
        if result.outcome is ExecutorOutcome.SUCCESS:
            outputs, why = extract_outputs(dict(result.outputs), order["outputs"])
            if outputs is not None:
                try:
                    self.engine.submit(run_id, node_id, outputs=outputs, submitted_by=SUBMITTER)
                    entry["submitted"] = True
                    return
                except GraphEngineeringError as exc:
                    if exc.code is not ErrorCode.INVALID_ARGUMENT:
                        raise
                    why = exc.message
            detail = "host returned invalid result: " + clip(why)
        else:
            detail = clip(result.detail) or "host execution failed"
        self.engine.submit(run_id, node_id, failed=True, detail=detail, submitted_by=SUBMITTER)
        entry["submitted"] = True
        entry["failed"] = detail
        entry["error"] = (ErrorCode.HOST_RESULT_INVALID if detail.startswith("host returned invalid result")
                          else ErrorCode.HOST_EXECUTION_FAILED).value
