"""Durable dispatch: let a host agent execute waiting ``agent`` nodes, exactly once per claim.

Graph Engineering defines *what* work a node needs (purpose, instructions, inputs,
capabilities, output contract, success criteria, evidence). The host decides *how*
it is done: model, provider, routing and tool policy never appear here.

Lifecycle of one dispatched node::

    engine: node RUNNING + wait=submission (persisted before anything else)
      -> lease   DispatchLease.acquire()  (runs/<id>/lease.json: owner, heartbeat, expiry)
      -> claim   trace ``node.dispatched`` with owner + lease epoch (write-ahead, durable)
      -> host    AgentWorker.execute_agent_work(work_order, context)   (fresh worker context)
      -> submit  GraphEngine.submit(...)  only while the lease is still ours
      -> release lease released in ``finally`` (heartbeat thread stopped)

Exactly one live lease exists per run. A second dispatcher in another thread,
process or host that shares the state directory sees the live lease and stops with
``DISPATCH_IN_PROGRESS``. A lease whose heartbeat is older than its ttl, or whose
owner process is gone (same host), is expired and may be taken over.

An attempt with a claim but no release was started by a dispatcher that is gone.
When the next lease holder claims that attempt again:

* idempotent, side-effect-free nodes: the abandoned claim is released
  (``abandoned_claim_reclaimed``) and the node is dispatched again;
* every other node: ``DISPATCH_INTERRUPTED`` is persisted on the node (NEEDS_ATTENTION
  hold with that code); only an operator may retry, submit or cancel.

The dispatcher is a worker, never an operator: it reads runs, claims, submits and
releases. It cannot approve plans or gates, authorize retries or cancel runs.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from .adapters.types import ExecutorOutcome, ExecutorResult
from .engine import GraphEngine, pending_work
from .errors import ErrorCode, GraphEngineeringError
from .guard import WorkerScope, current_worker, exclusive_dispatch, worker_scope
from .spec import digest_of
from .states import RUN_TERMINAL, NodeStatus, RunStatus
from .store import utc_now

SUBMITTER = "agent:host-autonomous"
DISPATCHER = "host-agent"
MAX_GOAL_CHARS = 15_000
MAX_CONTEXT_CHARS = 30_000
MAX_DETAIL_CHARS = 500
LEASE_TTL_SECONDS = 60.0
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
STOP_LEASE_LOST = "lease_lost"


@dataclass(frozen=True)
class WorkerContext:
    """Bounded, host-neutral description of one node attempt handed to the host.

    Everything a worker sees is in ``goal`` and ``context``: explicit artifacts of this
    node (instructions, resolved inputs, contract, diagnosis). No conversation history
    of the caller or of other nodes is ever part of it (see ``context_digest``).
    """

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
    risk: str | None = None
    allowed_toolsets: tuple[str, ...] | None = None
    context_digest: str = ""
    context_sources: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class AgentWorker(Protocol):
    """What a host must offer to run one agent node. Capabilities, not providers."""

    def supports_agent_execution(self) -> bool:
        """True if this host can execute agent work at all (static capability)."""

    def execute_agent_work(self, work_order: Mapping[str, Any], context: WorkerContext) -> ExecutorResult:
        """Perform the node's work in a fresh worker context and return a normalized result.

        ``EXECUTOR_UNAVAILABLE`` means nothing was started; ``CANCELLED`` and
        ``GOVERNANCE_BLOCKED`` mean the host declined or stopped; the failure
        outcomes mean the work was attempted and failed.
        """


HostAgentExecutor = AgentWorker  # 1.x name


# ---------------------------------------------------------------------- leases
def _pid_alive(pid: Any) -> bool | None:
    """True/False on POSIX; None when it cannot be determined."""
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def lease_is_live(lease: Mapping[str, Any] | None, now: float | None = None) -> bool:
    """A lease is live while its heartbeat is within its ttl and (on this host) its owner process exists."""
    if not lease or lease.get("corrupt") or lease.get("released"):
        return False
    now = time.time() if now is None else now
    try:
        expires = float(lease["heartbeat_at"]) + float(lease["ttl_seconds"])
    except (KeyError, TypeError, ValueError):
        return False
    if expires <= now:
        return False
    if lease.get("host") == socket.gethostname():
        alive = _pid_alive(lease.get("pid"))
        if alive is False:
            return False
    return True


class DispatchLease:
    """Durable, heartbeated ownership of dispatching one run."""

    def __init__(self, engine: GraphEngine, run_id: str, *, ttl_seconds: float = LEASE_TTL_SECONDS,
                 purpose: str = "dispatch") -> None:
        self.engine = engine
        self.store = engine.store
        self.run_id = run_id
        self.ttl_seconds = float(ttl_seconds)
        self.purpose = purpose
        self.lease_id = secrets.token_hex(12)
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.owner = "%s:%d:%s" % (self.host, self.pid, self.lease_id[:8])
        self.epoch = 0
        self.lost = False
        self.held = False
        self.holder: Mapping[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        with self.store.lock(self.run_id):
            current = self.store.read_lease(self.run_id)
            if lease_is_live(current) and current.get("lease_id") != self.lease_id:
                self.holder = current
                return False
            previous_epoch = int((current or {}).get("epoch") or 0) if not (current or {}).get("corrupt") else 0
            self.epoch = previous_epoch + 1
            now = time.time()
            self.store.write_lease(self.run_id, {
                "run_id": self.run_id, "owner": self.owner, "lease_id": self.lease_id, "host": self.host,
                "pid": self.pid, "purpose": self.purpose, "epoch": self.epoch, "acquired_at": now,
                "heartbeat_at": now, "ttl_seconds": self.ttl_seconds, "node": None})
            fields: dict[str, Any] = {"owner": self.owner, "epoch": self.epoch, "ttl_seconds": self.ttl_seconds,
                                      "purpose": self.purpose}
            if current and not current.get("released"):
                fields["took_over"] = {"owner": current.get("owner"), "epoch": current.get("epoch"),
                                       "reason": "corrupt" if current.get("corrupt") else "expired"}
            self.store.append_trace(self.run_id, "lease.acquired", **fields)
            self.held = True
            return True

    def verify(self) -> bool:
        """True while this lease is still the stored one (callers hold the run lock)."""
        current = self.store.read_lease(self.run_id)
        return bool(current) and current.get("lease_id") == self.lease_id and not current.get("released")

    def heartbeat(self, node: str | None = None) -> bool:
        try:
            with self.store.lock(self.run_id):
                current = self.store.read_lease(self.run_id)
                if not current or current.get("lease_id") != self.lease_id:
                    self.lost = True
                    return False
                current = dict(current)
                current["heartbeat_at"] = time.time()
                if node is not None:
                    current["node"] = node
                self.store.write_lease(self.run_id, current)
                return True
        except GraphEngineeringError:
            return True  # the lock was busy; the next beat retries well before the ttl ends

    def release(self) -> None:
        if not self.held:
            return
        self.stop_heartbeat()
        try:
            with self.store.lock(self.run_id):
                current = self.store.read_lease(self.run_id)
                if current and current.get("lease_id") == self.lease_id:
                    self.store.delete_lease(self.run_id)
                    self.store.append_trace(self.run_id, "lease.released", owner=self.owner, epoch=self.epoch)
        finally:
            self.held = False

    def start_heartbeat(self) -> None:
        interval = max(0.05, self.ttl_seconds / 3.0)

        def beat() -> None:
            while not self._stop.wait(interval):
                if not self.heartbeat():
                    return

        self._stop.clear()
        self._thread = threading.Thread(target=beat, name="ge-lease-heartbeat", daemon=True)
        self._thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    @contextmanager
    def held_for(self) -> Iterator[bool]:
        """Acquire (yield False if another owner is live), heartbeat while held, release in ``finally``."""
        if not self.acquire():
            yield False
            return
        self.start_heartbeat()
        try:
            yield True
        finally:
            self.release()


def dispatch_claims(events: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    claims = [e for e in events if e.get("type") == "node.dispatched"]
    released = [e for e in events if e.get("type") == "node.dispatch_released"]
    return claims, released


def claim(engine: GraphEngine, run_id: str, node_id: str, attempt: int, lease: DispatchLease,
          context_digest: str = "") -> int | None:
    """Write-ahead claim of one attempt. Returns the dispatch number (>= 1), 0 if the node was held
    as ``DISPATCH_INTERRUPTED`` (an abandoned claim of unsafe work), or None if the state moved on."""
    with engine.mutate(run_id) as (state, spec):
        if not lease.verify():
            lease.lost = True
            return None
        ns = state["nodes"].get(node_id)
        if ns is None or ns["status"] != NodeStatus.RUNNING.value or ns["wait"] != "submission" \
                or ns["attempts"] != attempt:
            return None
        node = spec.node(node_id)
        events = [e for e in engine.store.read_trace(run_id) if e.get("node") == node_id and e.get("attempt") == attempt]
        claims, released = dispatch_claims(events)
        replay_safe = bool(node["idempotent"]) and not node["side_effects"]
        if len(claims) > len(released):
            abandoned = claims[-1]
            if not replay_safe:
                engine.store.append_trace(run_id, "node.dispatch_interrupted", node=node_id, attempt=attempt,
                                          dispatch=abandoned.get("dispatch"), previous_owner=abandoned.get("owner"),
                                          detected_by=lease.owner)
                ns["wait"] = "attention"
                ns["last_error"] = {
                    "code": ErrorCode.DISPATCH_INTERRUPTED.value,
                    "message": "dispatch %s of attempt %s by %s ended without a result; the node may have partly "
                               "run and is not idempotent, so it is not replayed. An operator must retry, submit "
                               "or cancel." % (abandoned.get("dispatch"), attempt, abandoned.get("owner") or "?")}
                ns["history"].append({"attempt": attempt, "outcome": "DISPATCH_INTERRUPTED",
                                      "detail": "abandoned claim held for the operator", "at": utc_now()})
                engine._settle(state, spec)
                engine._commit(state, spec)
                return 0
            engine.store.append_trace(run_id, "node.dispatch_released", node=node_id, attempt=attempt,
                                      dispatch=abandoned.get("dispatch", len(claims)),
                                      reason="abandoned_claim_reclaimed", previous_owner=abandoned.get("owner"),
                                      released_by=lease.owner)
        engine.store.append_trace(run_id, "node.dispatched", node=node_id, attempt=attempt,
                                  dispatch=len(claims) + 1, dispatcher=DISPATCHER, owner=lease.owner,
                                  lease_epoch=lease.epoch, context_digest=context_digest)
        return len(claims) + 1


def release_claim(engine: GraphEngine, run_id: str, node_id: str, attempt: int, dispatch: int, reason: str,
                  lease: DispatchLease | None = None) -> None:
    with engine.store.lock(run_id):
        engine.store.append_trace(run_id, "node.dispatch_released", node=node_id, attempt=attempt,
                                  dispatch=dispatch, reason=reason, released_by=lease.owner if lease else None)


# ---------------------------------------------------------------------- task text
def clip(text: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_outputs(outputs: Mapping[str, Mapping[str, Any]]) -> str:
    if not outputs:
        return "- (no outputs declared; return an empty JSON object {})"
    return "\n".join("- %s: %s%s" % (name, spec.get("type", "any"), "" if spec.get("required", True) else " (optional)")
                     for name, spec in outputs.items())


def _describe_evidence(evidence: list[Mapping[str, Any]]) -> str:
    if not evidence:
        return "(none declared)"
    lines = []
    for check in evidence:
        kind = check.get("type")
        if kind == "command":
            lines.append("- command %s must exit %s" % (json.dumps(check.get("argv")), check.get("expect_exit", 0)))
        elif kind in ("file_exists", "file_absent"):
            lines.append("- %s: %s" % (kind, check.get("path")))
        elif kind == "file_contains":
            lines.append("- file %s must contain %s" % (check.get("path"), json.dumps(check.get("text"))))
        elif kind == "file_sha256":
            lines.append("- file %s must have sha256 %s" % (check.get("path"), check.get("sha256")))
        elif kind == "git_diff":
            lines.append("- git diff limited to %s" % json.dumps(check.get("allowed_paths") or []))
        else:
            lines.append("- %s" % json.dumps(dict(check), sort_keys=True))
    return "\n".join(lines)


def build_worker_context(state: Mapping[str, Any], order: Mapping[str, Any], *, dispatch: int,
                         timeout_seconds: int, allowed_toolsets: tuple[str, ...] | None = None) -> WorkerContext:
    """Render the work order into a bounded task. The text is guidance; contracts and evidence enforce."""
    run_id, node_id, attempt = order["run_id"], order["node"], order["attempt"]
    risk = order.get("risk")
    lines = [
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
    ]
    if risk:
        lines += ["Permitted risk class: %s. Actions beyond it are blocked and fail this node." % risk]
    workspace = state.get("workspace")
    if workspace:
        lines += ["Workspace directory: %s (keep all file changes inside it)." % workspace]
    lines += [
        "",
        "Required output contract - a JSON object with exactly these keys:",
        _describe_outputs(order["outputs"]),
        "",
        "Success criteria (checked automatically after you answer; a failed check fails the attempt):",
        json.dumps(order["success_criteria"], sort_keys=True) if order["success_criteria"] else "(none declared)",
        "",
        "Evidence (checked deterministically by the runtime after you answer; your own claims do not count):",
        _describe_evidence(list(order.get("evidence") or [])),
        "",
        "Reply format: end your final message with ONE JSON object holding only the declared outputs, in a "
        "```json fenced block. If you cannot complete the node, say so plainly instead of inventing outputs.",
    ]
    goal = "\n".join(lines)
    inputs = order["inputs"] or {}
    parts = [("Inputs for this node (JSON):\n" + json.dumps(inputs, sort_keys=True, ensure_ascii=False)) if inputs
             else "This node has no inputs."]
    sources = ["inputs:%s" % name for name in sorted(inputs)]
    if order.get("diagnosis"):
        parts.append("Diagnosis of the previous failed attempt (fix this; do not repeat it):\n"
                     + json.dumps(order["diagnosis"], sort_keys=True, ensure_ascii=False))
        sources.append("diagnosis")
    context = "\n\n".join(parts)
    if len(goal) > MAX_GOAL_CHARS or len(context) > MAX_CONTEXT_CHARS:
        raise GraphEngineeringError(ErrorCode.HOST_EXECUTION_FAILED,
                                    "node %s work order is too large to hand to the host" % node_id)
    execution_id = "%s:%s:%d" % (run_id, node_id, attempt)
    return WorkerContext(
        run_id=run_id, graph_id=str(state["graph_id"]), node_id=node_id, attempt=attempt,
        attempt_id="%s:%d" % (node_id, attempt), execution_id=execution_id,
        correlation_id="ge:%s:%d" % (execution_id, dispatch), goal=goal, context=context,
        timeout_seconds=timeout_seconds, risk=risk, allowed_toolsets=allowed_toolsets,
        context_digest=digest_of({"goal": goal, "context": context}), context_sources=tuple(sources))


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
    """Sequentially executes waiting agent nodes of one run through an ``AgentWorker``."""

    def __init__(self, engine: GraphEngine, executor: AgentWorker, *, state_key: str,
                 max_dispatches: int | None = None, node_timeout_seconds: int = 3600,
                 call_budget_seconds: float | None = None, lease_ttl_seconds: float = LEASE_TTL_SECONDS,
                 toolsets_for: Any = None, deadline: float | None = None) -> None:
        self.engine = engine
        self.executor = executor
        self.state_key = state_key
        self.max_dispatches = engine.max_steps if max_dispatches is None else max_dispatches
        self.node_timeout_seconds = node_timeout_seconds
        self.call_budget_seconds = call_budget_seconds  # None: unbounded
        self.lease_ttl_seconds = lease_ttl_seconds
        self.toolsets_for = toolsets_for  # callable(risk) -> tuple[str, ...] | None
        self.deadline = deadline  # absolute time.monotonic() bound shared with a caller (autopilot)

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
            lease = DispatchLease(self.engine, run_id, ttl_seconds=self.lease_ttl_seconds)
            with lease.held_for() as held:
                if not held:
                    holder = lease.holder or {}
                    return self._stop(report, STOP_IN_PROGRESS, ErrorCode.DISPATCH_IN_PROGRESS,
                                      "run %s is being dispatched by %s (lease epoch %s)" % (
                                          run_id, holder.get("owner"), holder.get("epoch")),
                                      lease_owner=holder.get("owner"))
                report["lease"] = {"owner": lease.owner, "epoch": lease.epoch}
                return self._drive_locked(run_id, report, lease)

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

    def _out_of_time(self, started: float, dispatches: int) -> bool:
        now = time.monotonic()
        if self.deadline is not None and now >= self.deadline:
            return True
        return bool(dispatches) and self.call_budget_seconds is not None \
            and now - started >= self.call_budget_seconds

    def _drive_locked(self, run_id: str, report: dict[str, Any], lease: DispatchLease) -> dict[str, Any]:
        dispatches = 0
        started = time.monotonic()
        while True:
            if lease.lost:
                return self._stop(report, STOP_LEASE_LOST, ErrorCode.DISPATCH_IN_PROGRESS,
                                  "the dispatch lease was taken over; stopping without submitting")
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
            if self._out_of_time(started, dispatches):
                return self._stop(report, STOP_TIME_BUDGET,
                                  detail="time budget for this call reached; call again to continue")
            order = orders[0]
            toolsets = self.toolsets_for(order.get("risk")) if self.toolsets_for else None
            try:
                context = build_worker_context(state, order, dispatch=1, timeout_seconds=self.node_timeout_seconds,
                                               allowed_toolsets=toolsets)
            except GraphEngineeringError as exc:
                context, context_error = None, exc
            else:
                context_error = None
            claimed = claim(self.engine, run_id, order["node"], order["attempt"], lease,
                            context.context_digest if context else "")
            if claimed is None:
                if lease.lost:
                    continue
                return self._stop(report, STOP_STATE_CHANGED)
            if claimed == 0:
                return self._stop(
                    report, STOP_INTERRUPTED, ErrorCode.DISPATCH_INTERRUPTED,
                    "node %s was dispatched before and never finished; it may have partly run and is not "
                    "idempotent, so it is not replayed. It is held for the operator (/ge-retry, /ge-submit or "
                    "/ge-cancel)." % order["node"], node=order["node"])
            if context is not None and claimed != 1:
                context = build_worker_context(state, order, dispatch=claimed,
                                               timeout_seconds=self.node_timeout_seconds, allowed_toolsets=toolsets)
            dispatches += 1
            lease.heartbeat(order["node"])
            outcome = self._execute_and_submit(run_id, state, order, claimed, report, lease, context, context_error)
            if outcome is not None:
                return outcome

    def _execute_and_submit(self, run_id: str, state: Mapping[str, Any], order: Mapping[str, Any], dispatch: int,
                            report: dict[str, Any], lease: DispatchLease, context: WorkerContext | None,
                            context_error: GraphEngineeringError | None) -> dict[str, Any] | None:
        """Run one node on the host and submit. Returns a final report to stop, or None to continue."""
        node_id, attempt = order["node"], order["attempt"]
        entry: dict[str, Any] = {"node": node_id, "attempt": attempt, "dispatch": dispatch}
        if context is not None:
            entry["context_digest"] = context.context_digest
            entry["correlation_id"] = context.correlation_id
        report["dispatched"].append(entry)
        try:
            if context_error is not None:
                raise context_error
            assert context is not None
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
            release_claim(self.engine, run_id, node_id, attempt, dispatch, "host unavailable", lease)
            entry["submitted"] = False
            return self._stop(report, STOP_HOST_UNAVAILABLE, ErrorCode.HOST_EXECUTION_UNAVAILABLE,
                              clip(result.detail) or "host agent execution unavailable")
        if outcome in (ExecutorOutcome.CANCELLED, ExecutorOutcome.GOVERNANCE_BLOCKED):
            entry["submitted"] = False
            return self._stop(report, STOP_HOST_DECLINED, ErrorCode.HOST_EXECUTION_FAILED,
                              clip(result.detail) or "host stopped before finishing the node; node left waiting")

        try:
            with self.engine.store.lock(run_id):
                if not lease.verify():
                    lease.lost = True
                    entry["submitted"] = False
                    return self._stop(report, STOP_LEASE_LOST, ErrorCode.DISPATCH_IN_PROGRESS,
                                      "the dispatch lease was taken over while node %s ran; its result was not "
                                      "submitted" % node_id)
                self._submit(run_id, order, result, entry)
        except GraphEngineeringError as exc:
            if exc.code is ErrorCode.INVALID_TRANSITION:
                entry["submitted"] = False
                return self._stop(report, STOP_STATE_CHANGED, exc.code, exc.message)
            raise
        return None

    def _submit(self, run_id: str, order: Mapping[str, Any], result: ExecutorResult, entry: dict[str, Any]) -> None:
        node_id = order["node"]
        usage = {"cost": result.usage.cost} if result.usage.cost is not None else None
        if result.outcome is ExecutorOutcome.SUCCESS:
            outputs, why = extract_outputs(dict(result.outputs), order["outputs"])
            if outputs is not None:
                try:
                    self.engine.submit(run_id, node_id, outputs=outputs, submitted_by=SUBMITTER, usage=usage)
                    entry["submitted"] = True
                    return
                except GraphEngineeringError as exc:
                    if exc.code is not ErrorCode.INVALID_ARGUMENT:
                        raise
                    why = exc.message
            detail = "host returned invalid result: " + clip(why)
        else:
            detail = clip(result.detail) or "host execution failed"
        self.engine.submit(run_id, node_id, failed=True, detail=detail, submitted_by=SUBMITTER, usage=usage)
        entry["submitted"] = True
        entry["failed"] = detail
        entry["error"] = (ErrorCode.HOST_RESULT_INVALID if detail.startswith("host returned invalid result")
                          else ErrorCode.HOST_EXECUTION_FAILED).value
