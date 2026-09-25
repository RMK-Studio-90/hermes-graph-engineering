"""Graph execution engine: run lifecycle, gates, execution, recovery, verification.

Invariants
----------
* A run executes only after its plan is approved (unless the host disables
  plan approval explicitly).
* Before a node runs: dependencies SUCCEEDED, approval gates approved for the
  exact binding, executor available, input contract satisfied.
* A node is RUNNING on disk *before* its executor is called (write-ahead), so
  an interruption is always visible after a restart.
* After execution the output contract and every success criterion are checked;
  an executor reporting success is never enough on its own.
* Recovery never replays a non-idempotent node automatically; it holds the run
  in NEEDS_ATTENTION until an operator decides.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .adapters.types import ApprovalBinding, ExecutionRequest, ExecutorOutcome, ExecutorResult
from .contracts import evaluate_criteria, is_type, output_contract_errors
from .errors import ErrorCode, GraphEngineeringError
from .executors import ExecutorCatalog
from .spec import GraphSpec, admit, digest_of
from .states import NODE_TERMINAL, RUN_TERMINAL, HoldState, NodeStatus, RunStatus, TerminalOutcome
from .store import RunStore, utc_now
from .version import RECEIPT_SCHEMA_VERSION, RUN_STATE_SCHEMA_VERSION

MAX_SUBMISSION_BYTES = 1_000_000
PLAN_TARGET = "plan"
_RETRYABLE = {ExecutorOutcome.RETRYABLE_FAILURE, ExecutorOutcome.TIMEOUT}


def _err(code: ErrorCode, message: str, **details: Any) -> GraphEngineeringError:
    return GraphEngineeringError(code, message, details or None)


class GraphEngine:
    def __init__(
        self,
        store: RunStore,
        catalog: ExecutorCatalog | None = None,
        *,
        require_plan_approval: bool = True,
        max_steps: int = 500,
    ) -> None:
        self.store = store
        self.catalog = catalog or ExecutorCatalog()
        self.require_plan_approval = require_plan_approval
        self.max_steps = max_steps

    # ------------------------------------------------------------------ admission
    def validate(self, raw_spec: Any) -> GraphSpec:
        return admit(raw_spec, self.catalog)

    def create(self, raw_spec: Any, *, created_by: str = "unknown") -> dict[str, Any]:
        spec = self.validate(raw_spec)
        run_id = self.store.new_run_id(spec.graph_id)
        now = utc_now()
        nodes = {
            node["id"]: {
                "status": NodeStatus.PENDING.value,
                "attempts": 0,
                "wait": None,
                "inputs": None,
                "outputs": None,
                "criteria": [],
                "last_error": None,
                "started_at": None,
                "finished_at": None,
                "history": [],
            }
            for node in spec.nodes
        }
        approvals: dict[str, Any] = {}
        status = RunStatus.DRAFT
        if not self.require_plan_approval:
            approvals[PLAN_TARGET] = {"status": "NOT_REQUIRED", "decided_by": "configuration", "decided_at": now}
            status = RunStatus.APPROVED
        state = {
            "state_schema_version": RUN_STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "graph_id": spec.graph_id,
            "title": spec.data["title"],
            "spec": spec.data,
            "spec_digest": spec.digest,
            "order": list(spec.order),
            "status": status.value,
            "holds": [],
            "stopped": False,
            "failure": None,
            "approvals": approvals,
            "nodes": nodes,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "verification": None,
        }
        with self.store.lock(run_id):
            self.store.save(run_id, state)
            self.store.append_trace(run_id, "run.created", spec_digest=spec.digest, created_by=created_by)
        return state

    # ------------------------------------------------------------------ loading
    def load(self, run_id: str) -> tuple[dict[str, Any], GraphSpec]:
        state = self.store.load(run_id)
        try:
            spec = admit(state.get("spec"), ExecutorCatalog())
        except GraphEngineeringError as exc:
            raise _err(ErrorCode.STATE_CORRUPT, "run %s stored spec no longer admits: %s" % (run_id, exc)) from exc
        if spec.digest != state.get("spec_digest") or list(spec.order) != state.get("order"):
            raise _err(ErrorCode.STATE_CORRUPT, "run %s spec digest mismatch" % run_id)
        nodes = state.get("nodes")
        if not isinstance(nodes, dict) or set(nodes) != {n["id"] for n in spec.nodes}:
            raise _err(ErrorCode.STATE_CORRUPT, "run %s node table does not match its spec" % run_id)
        valid_status = {s.value for s in NodeStatus}
        for node_id, node_state in nodes.items():
            if not isinstance(node_state, dict) or node_state.get("status") not in valid_status:
                raise _err(ErrorCode.STATE_CORRUPT, "run %s node %s has an invalid status" % (run_id, node_id))
        if state.get("status") not in {s.value for s in RunStatus}:
            raise _err(ErrorCode.STATE_CORRUPT, "run %s has an invalid status" % run_id)
        return state, spec

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = []
        for run_id in self.store.list_runs()[:limit]:
            try:
                state = self.store.load(run_id)
                rows.append({"run_id": run_id, "graph_id": state.get("graph_id"), "status": state.get("status"),
                             "updated_at": state.get("updated_at")})
            except GraphEngineeringError as exc:
                rows.append({"run_id": run_id, "graph_id": None, "status": exc.code.value, "updated_at": None})
        return rows

    # ------------------------------------------------------------------ approvals
    def _binding(self, state: Mapping[str, Any], spec: GraphSpec, target: str) -> ApprovalBinding:
        if target == PLAN_TARGET:
            action = {"order": state["order"], "spec_digest": spec.digest}
        else:
            node_id, gate_id = target.split(":", 1)
            node = spec.node(node_id)
            gate = next((g for g in node["gates"] if g["id"] == gate_id), None)
            if gate is None:
                raise _err(ErrorCode.INVALID_ARGUMENT, "node %s has no gate %r" % (node_id, gate_id))
            action = {"node": node, "gate": gate}
            if gate["type"] == "review":
                action["outputs"] = state["nodes"][node_id].get("outputs")
        return ApprovalBinding(
            graph_id=spec.graph_id,
            spec_version=spec.digest,
            run_id=state["run_id"],
            node_or_transition=target,
            action_digest=digest_of(action),
        )

    def decide(self, run_id: str, target: str, *, approve: bool, decided_by: str) -> dict[str, Any]:
        """Record a human decision for the plan or for a ``<node>:<gate>`` gate."""
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise _err(ErrorCode.INVALID_ARGUMENT, "decided_by is required")
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            status = RunStatus(state["status"])
            if status in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (run_id, status.value))
            if target == PLAN_TARGET:
                if status is not RunStatus.DRAFT:
                    raise _err(ErrorCode.INVALID_TRANSITION, "plan of run %s is not awaiting approval" % run_id)
            else:
                node_id, sep, gate_id = target.partition(":")
                if not sep or node_id not in state["nodes"]:
                    raise _err(ErrorCode.INVALID_ARGUMENT, "gate target must be 'plan' or '<node>:<gate>'")
                gate = next((g for g in spec.node(node_id)["gates"] if g["id"] == gate_id), None)
                if gate is None:
                    raise _err(ErrorCode.INVALID_ARGUMENT, "node %s has no gate %r" % (node_id, gate_id))
                node_state = state["nodes"][node_id]
                expected_wait = "approval" if gate["type"] == "approval" else "review"
                if node_state["wait"] != expected_wait or self._pending_gate(state, spec, node_id, gate["type"]) != gate_id:
                    raise _err(ErrorCode.INVALID_TRANSITION, "gate %s is not currently awaiting a decision" % target)
            binding = self._binding(state, spec, target)
            now = utc_now()
            state["approvals"][target] = {
                "status": "APPROVED" if approve else "DENIED",
                "binding": binding.to_dict(),
                "binding_digest": binding.digest(),
                "decided_by": decided_by,
                "decided_at": now,
            }
            self.store.append_trace(run_id, "approval.decided", target=target, approved=approve, decided_by=decided_by,
                                    binding_digest=binding.digest())
            if target == PLAN_TARGET:
                if approve:
                    state["status"] = RunStatus.APPROVED.value
                else:
                    state["status"] = RunStatus.CANCELLED.value
                    state["failure"] = {"code": ErrorCode.GATE_FAILED.value, "node": None, "message": "plan denied"}
                    self._skip_open_nodes(state, "plan denied")
                    state["finished_at"] = now
                self._save(state)
                return state
            node_id, _, gate_id = target.partition(":")
            node_state = state["nodes"][node_id]
            if not approve:
                node_state["status"] = NodeStatus.FAILED.value
                node_state["wait"] = None
                node_state["finished_at"] = now
                self._record_failure(state, spec, node_id, ErrorCode.GATE_FAILED, "gate %s denied by %s" % (gate_id, decided_by))
            elif node_state["wait"] == "approval":
                if self._pending_gate(state, spec, node_id, "approval") is None:
                    node_state["wait"] = None
            elif node_state["wait"] == "review" and self._pending_gate(state, spec, node_id, "review") is None:
                node_state["wait"] = None
                node_state["status"] = NodeStatus.SUCCEEDED.value
                node_state["finished_at"] = now
                self.store.append_trace(run_id, "node.succeeded", node=node_id, attempt=node_state["attempts"])
            if RunStatus(state["status"]) in (RunStatus.RUNNING, RunStatus.WAITING):
                self._advance(state, spec)
            self._settle(state, spec)
            self._save(state)
            return state

    def _pending_gate(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str, gate_type: str) -> str | None:
        for gate in spec.node(node_id)["gates"]:
            if gate["type"] != gate_type:
                continue
            record = state["approvals"].get("%s:%s" % (node_id, gate["id"]))
            if not record or record.get("status") != "APPROVED":
                return gate["id"]
            if record.get("binding_digest") != self._binding(state, spec, "%s:%s" % (node_id, gate["id"])).digest():
                return gate["id"]
        return None

    def _denied_gate(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str) -> str | None:
        for gate in spec.node(node_id)["gates"]:
            record = state["approvals"].get("%s:%s" % (node_id, gate["id"]))
            if record and record.get("status") == "DENIED":
                return gate["id"]
        return None

    # ------------------------------------------------------------------ execution
    def execute(self, run_id: str, *, max_steps: int | None = None) -> dict[str, Any]:
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            status = RunStatus(state["status"])
            if status is RunStatus.DRAFT:
                raise _err(ErrorCode.GATE_FAILED, "plan of run %s is not approved; approve target 'plan' first" % run_id,
                           run_id=run_id, gate="plan")
            if status in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, status.value))
            self._preflight_executors(state, spec)
            if status is RunStatus.APPROVED:
                self.store.append_trace(run_id, "run.started")
            state["status"] = RunStatus.RUNNING.value
            self._advance(state, spec, max_steps)
            self._settle(state, spec)
            self._save(state)
            return state

    def _preflight_executors(self, state: Mapping[str, Any], spec: GraphSpec) -> None:
        problems = []
        for node in spec.nodes:
            if NodeStatus(state["nodes"][node["id"]]["status"]) in NODE_TERMINAL:
                continue
            try:
                executor = self.catalog.get(node["executor"])
            except GraphEngineeringError as exc:
                problems.append("node %s: %s" % (node["id"], exc.message))
                continue
            missing = sorted(set(node["requires"]) - set(executor.capabilities))
            if missing:
                problems.append("node %s: missing capability %s" % (node["id"], ", ".join(missing)))
        if problems:
            raise _err(ErrorCode.EXECUTOR_UNAVAILABLE, "required executors are unavailable", errors=problems)

    def submit(
        self,
        run_id: str,
        node_id: str,
        *,
        outputs: Any = None,
        failed: bool = False,
        detail: str = "",
        submitted_by: str = "agent",
    ) -> dict[str, Any]:
        """Deliver the result of a deferred (agent) node."""
        if not failed:
            try:
                size = len(json.dumps(outputs, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise _err(ErrorCode.INVALID_ARGUMENT, "outputs must be JSON-serializable") from exc
            if size > MAX_SUBMISSION_BYTES:
                raise _err(ErrorCode.INVALID_ARGUMENT, "outputs exceed %d bytes" % MAX_SUBMISSION_BYTES)
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            if node_id not in state["nodes"]:
                raise _err(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)
            node_state = state["nodes"][node_id]
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (run_id, state["status"]))
            if node_state["status"] != NodeStatus.RUNNING.value or node_state["wait"] != "submission":
                raise _err(ErrorCode.INVALID_TRANSITION, "node %s is not awaiting a submission (status %s)" % (
                    node_id, node_state["status"]))
            self.store.append_trace(run_id, "node.submitted", node=node_id, attempt=node_state["attempts"],
                                    failed=bool(failed), submitted_by=submitted_by)
            node_state["wait"] = None
            if failed:
                result = ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail=detail or "reported failed")
            else:
                result = ExecutorResult(ExecutorOutcome.SUCCESS, outputs=outputs if isinstance(outputs, Mapping) else {})
                if not isinstance(outputs, Mapping):
                    result = ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail="outputs must be an object")
            self._complete_attempt(state, spec, node_id, result)
            self._advance(state, spec)
            self._settle(state, spec)
            self._save(state)
            return state

    def resume(self, run_id: str) -> dict[str, Any]:
        """Recover an interrupted run conservatively, then continue if its plan is approved."""
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            status = RunStatus(state["status"])
            if status in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, status.value))
            recovered = []
            for node in spec.nodes:
                node_state = state["nodes"][node["id"]]
                if node_state["status"] != NodeStatus.RUNNING.value or node_state["wait"] is not None:
                    continue
                if node["idempotent"] and not node["side_effects"]:
                    node_state["status"] = NodeStatus.READY.value
                    node_state["history"].append({"attempt": node_state["attempts"], "outcome": "INTERRUPTED",
                                                  "detail": "re-queued (idempotent)", "at": utc_now()})
                    recovered.append({"node": node["id"], "action": "requeued"})
                else:
                    node_state["wait"] = "attention"
                    node_state["history"].append({"attempt": node_state["attempts"], "outcome": "INTERRUPTED",
                                                  "detail": "not replayed (non-idempotent)", "at": utc_now()})
                    recovered.append({"node": node["id"], "action": "held_for_operator"})
            self.store.append_trace(run_id, "run.resumed", recovered=recovered)
            if status is not RunStatus.DRAFT:
                self._preflight_executors(state, spec)
                state["status"] = RunStatus.RUNNING.value
                self._advance(state, spec)
                self._settle(state, spec)
            self._save(state)
            state["recovered"] = recovered
            return state

    def retry_node(self, run_id: str, node_id: str, *, decided_by: str) -> dict[str, Any]:
        """Operator decision: re-run a node held in NEEDS_ATTENTION after an interruption."""
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            node_state = state["nodes"].get(node_id)
            if node_state is None:
                raise _err(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)
            if node_state["wait"] != "attention":
                raise _err(ErrorCode.INVALID_TRANSITION, "node %s is not held for operator attention" % node_id)
            node_state["wait"] = None
            node_state["status"] = NodeStatus.READY.value
            self.store.append_trace(run_id, "node.retry_authorized", node=node_id, decided_by=decided_by)
            state["status"] = RunStatus.RUNNING.value
            self._preflight_executors(state, spec)
            self._advance(state, spec)
            self._settle(state, spec)
            self._save(state)
            return state

    def reclaim_stale_dispatch(
        self, run_id: str, node_id: str, *,
        decided_by: str, force: bool = False, stale_after_seconds: float = 3600.0,
    ) -> dict[str, Any]:
        """Operator decision: release an abandoned host dispatch for a deferred node.

        A node that is ``RUNNING`` with ``wait == "submission"`` normally stays
        waiting forever when the host worker that claimed it died before
        submitting (a synchronous call-budget boundary is the usual cause, not
        a node failure). This action is the explicit, trace-backed escape hatch:

        * it NEVER marks the node successful and NEVER fabricates outputs;
        * it appends the same ``node.dispatch_released`` event the dispatcher
          emits, so claim/release accounting stays consistent and a later
          legitimate dispatch is not blocked;
        * it moves the node to ``NEEDS_ATTENTION`` so the existing
          ``retry_node`` path re-runs the SAME node in the SAME run.

        Fails closed unless every guard below is satisfied.
        """
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (run_id, state["status"]))
            node_state = state["nodes"].get(node_id)
            if node_state is None:
                raise _err(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)
            if node_state["status"] != NodeStatus.RUNNING.value or node_state["wait"] != "submission":
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s is not RUNNING+WAITING_FOR_SUBMISSION (status %s, wait %r)" % (
                               node_id, node_state["status"], node_state["wait"]))
            if node_state.get("outputs"):
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s already has persisted outputs; refusing to reclaim" % node_id)

            attempt = node_state["attempts"]
            events = [e for e in self.store.read_trace(run_id)
                      if e.get("node") == node_id and e.get("attempt") == attempt]
            terminal = any(e.get("type") in ("node.succeeded", "node.failed") for e in events)
            if terminal:
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s has a terminal attempt event; refusing to reclaim" % node_id)
            claims = [e for e in events if e.get("type") == "node.dispatched"]
            released = [e for e in events if e.get("type") == "node.dispatch_released"]
            if not claims:
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s has no outstanding dispatch claim; nothing to reclaim" % node_id)
            if len(released) >= len(claims):
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s dispatch is already released; nothing to reclaim (duplicate)" % node_id)

            stale = False
            if force:
                stale = True
            else:
                latest = max((e.get("ts", "") for e in claims), default="")
                if latest:
                    try:
                        from datetime import datetime, timezone
                        latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - latest_dt).total_seconds()
                        stale = age >= stale_after_seconds
                    except (ValueError, TypeError):
                        stale = False
            if not stale:
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s dispatch is not stale yet (or --force needed)" % node_id)

            dispatched = claims[-1].get("dispatch", len(claims))
            now = utc_now()
            self.store.append_trace(
                run_id, "node.dispatch_released", node=node_id, attempt=attempt,
                dispatch=dispatched, reason="stale_dispatch_recovery", released_by=decided_by,
                previous_wait="submission", evidence="no_host_result",
            )
            node_state["wait"] = "attention"
            node_state["last_error"] = {
                "code": ErrorCode.DISPATCH_INTERRUPTED.value,
                "message": "STALE_DISPATCH_RECOVERED",
            }
            node_state["history"].append({
                "attempt": attempt, "outcome": "STALE_DISPATCH_RECOVERED",
                "detail": "stale host dispatch released by %s" % decided_by, "at": now,
            })
            state["status"] = RunStatus.WAITING.value
            self._settle(state, spec)
            self._save(state)
            return state

    def cancel(self, run_id: str, *, decided_by: str) -> dict[str, Any]:
        with self.store.lock(run_id):
            state, _spec = self.load(run_id)
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, state["status"]))
            self._skip_open_nodes(state, "run cancelled")
            state["status"] = RunStatus.CANCELLED.value
            state["holds"] = []
            state["finished_at"] = utc_now()
            self.store.append_trace(run_id, "run.cancelled", decided_by=decided_by)
            self._save(state)
            return state

    # ------------------------------------------------------------------ internals
    def _save(self, state: dict[str, Any]) -> None:
        state.pop("recovered", None)
        state["updated_at"] = utc_now()
        self.store.save(state["run_id"], state)

    def _skip_open_nodes(self, state: dict[str, Any], reason: str) -> None:
        for node_state in state["nodes"].values():
            if NodeStatus(node_state["status"]) not in NODE_TERMINAL:
                node_state["status"] = NodeStatus.SKIPPED.value
                node_state["wait"] = None
                node_state["last_error"] = node_state["last_error"] or {"code": None, "message": reason}

    def _refresh_readiness(self, state: dict[str, Any], spec: GraphSpec) -> None:
        nodes = state["nodes"]
        for node_id in state["order"]:
            node_state = nodes[node_id]
            status = NodeStatus(node_state["status"])
            if status not in (NodeStatus.PENDING, NodeStatus.READY):
                continue
            if state["stopped"]:
                node_state["status"] = NodeStatus.SKIPPED.value
                node_state["wait"] = None
                node_state["last_error"] = {"code": None, "message": "run stopped after a failure"}
                continue
            deps = [NodeStatus(nodes[dep]["status"]) for dep in spec.node(node_id)["depends_on"]]
            bad = [dep for dep in spec.node(node_id)["depends_on"]
                   if NodeStatus(nodes[dep]["status"]) in (NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.SKIPPED)]
            if bad:
                node_state["status"] = NodeStatus.BLOCKED.value
                node_state["wait"] = None
                node_state["last_error"] = {"code": None, "message": "blocked by upstream %s" % ", ".join(bad)}
            elif status is NodeStatus.PENDING and all(dep is NodeStatus.SUCCEEDED for dep in deps):
                node_state["status"] = NodeStatus.READY.value

    def _resolve_inputs(self, state: Mapping[str, Any], spec: GraphSpec, node: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        values: dict[str, Any] = {}
        errors = []
        graph_inputs = spec.data["inputs"]
        for name, input_spec in node["inputs"].items():
            source = input_spec["from"]
            if source.startswith("graph.inputs."):
                key = source[len("graph.inputs."):]
                present, value = key in graph_inputs, graph_inputs.get(key)
            else:
                src_node, _, src_output = source.partition(".")
                outputs = state["nodes"][src_node].get("outputs") or {}
                present, value = src_output in outputs, outputs.get(src_output)
            if not present:
                if input_spec["required"]:
                    errors.append("required input %r (%s) is missing" % (name, source))
                continue
            if not is_type(value, input_spec["type"]):
                errors.append("input %r (%s) is not of type %s" % (name, source, input_spec["type"]))
                continue
            values[name] = value
        return values, errors

    def _advance(self, state: dict[str, Any], spec: GraphSpec, max_steps: int | None = None) -> None:
        budget = self.max_steps if max_steps is None else max_steps
        steps = 0
        state.pop("budget_exhausted", None)
        run_id = state["run_id"]
        progress = True
        while progress:
            progress = False
            self._refresh_readiness(state, spec)
            for node_id in state["order"]:
                node_state = state["nodes"][node_id]
                if node_state["status"] != NodeStatus.READY.value or node_state["wait"] == "attention":
                    continue
                if steps >= budget:
                    state["budget_exhausted"] = True
                    return
                node = spec.node(node_id)
                denied = self._denied_gate(state, spec, node_id)
                if denied:
                    node_state["status"] = NodeStatus.FAILED.value
                    self._record_failure(state, spec, node_id, ErrorCode.GATE_FAILED, "gate %s denied" % denied)
                    progress = True
                    continue
                pending = self._pending_gate(state, spec, node_id, "approval")
                if pending:
                    if node_state["wait"] != "approval":
                        node_state["wait"] = "approval"
                        self.store.append_trace(run_id, "gate.requested", node=node_id, gate=pending)
                    continue
                node_state["wait"] = None
                inputs, input_errors = self._resolve_inputs(state, spec, node)
                if input_errors:
                    node_state["status"] = NodeStatus.FAILED.value
                    node_state["criteria"] = []
                    self._record_failure(state, spec, node_id, ErrorCode.NODE_FAILED,
                                         "input contract violated: " + "; ".join(input_errors))
                    progress = True
                    continue
                executor = self.catalog.get(node["executor"])
                steps += 1
                node_state["status"] = NodeStatus.RUNNING.value
                node_state["attempts"] += 1
                node_state["inputs"] = inputs
                node_state["started_at"] = utc_now()
                # write-ahead: the attempt is durable before any executor runs
                self.store.append_trace(run_id, "node.started", node=node_id, attempt=node_state["attempts"],
                                        executor=node["executor"])
                self._save(state)
                progress = True
                if getattr(executor, "deferred", False):
                    node_state["wait"] = "submission"
                    self.store.append_trace(run_id, "node.awaiting_submission", node=node_id,
                                            attempt=node_state["attempts"])
                    continue
                attempt = node_state["attempts"]
                request = ExecutionRequest(
                    run_id=run_id,
                    node_id=node_id,
                    attempt_id="%s:%d" % (node_id, attempt),
                    execution_id="%s:%s:%d" % (run_id, node_id, attempt),
                    context_id=run_id,
                    node_type=node["executor"],
                    identity=node["owner"],
                    contract={"operation": node["operation"], "outputs": node["outputs"]},
                    inputs=inputs,
                )
                try:
                    result = executor.execute(request)
                except Exception as exc:  # executor bugs fail the attempt, never the process
                    result = ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail="executor raised %s" % type(exc).__name__)
                self._complete_attempt(state, spec, node_id, result)

    def _complete_attempt(self, state: dict[str, Any], spec: GraphSpec, node_id: str, result: ExecutorResult) -> None:
        node = spec.node(node_id)
        node_state = state["nodes"][node_id]
        run_id = state["run_id"]
        attempt = node_state["attempts"]
        now = utc_now()
        outcome = result.outcome
        detail = result.detail
        criteria: list[dict[str, Any]] = []
        if outcome is ExecutorOutcome.SUCCESS:
            outputs = dict(result.outputs)
            contract_errors = output_contract_errors(node["outputs"], outputs)
            criteria = evaluate_criteria(node["success_criteria"], outputs) if not contract_errors else []
            node_state["criteria"] = criteria
            if contract_errors:
                outcome, detail = ExecutorOutcome.RETRYABLE_FAILURE, "output contract violated: " + "; ".join(contract_errors)
            elif any(not c["passed"] for c in criteria):
                failed = [c["detail"] for c in criteria if not c["passed"]]
                outcome, detail = ExecutorOutcome.RETRYABLE_FAILURE, "success criteria failed: " + "; ".join(failed)
            else:
                node_state["outputs"] = outputs
                node_state["last_error"] = None
                node_state["history"].append({"attempt": attempt, "outcome": "SUCCESS", "detail": "", "at": now})
                if self._pending_gate(state, spec, node_id, "review"):
                    node_state["wait"] = "review"
                    self.store.append_trace(run_id, "gate.requested", node=node_id,
                                            gate=self._pending_gate(state, spec, node_id, "review"))
                    return
                node_state["status"] = NodeStatus.SUCCEEDED.value
                node_state["finished_at"] = now
                self.store.append_trace(run_id, "node.succeeded", node=node_id, attempt=attempt,
                                        outputs_digest=digest_of(outputs))
                return
        node_state["history"].append({"attempt": attempt, "outcome": outcome.value, "detail": detail, "at": now})
        retry_allowed = (
            outcome in _RETRYABLE
            and attempt < node["retry"]["max_attempts"]
            and node["idempotent"]
            and not node["side_effects"]
        )
        self.store.append_trace(run_id, "node.attempt_failed", node=node_id, attempt=attempt, outcome=outcome.value,
                                detail=detail, will_retry=retry_allowed)
        if retry_allowed:
            node_state["status"] = NodeStatus.READY.value
            node_state["last_error"] = {"code": ErrorCode.NODE_FAILED.value, "message": detail}
            return
        node_state["status"] = NodeStatus.FAILED.value
        node_state["finished_at"] = now
        code = ErrorCode.EXECUTOR_UNAVAILABLE if outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE else ErrorCode.NODE_FAILED
        self._record_failure(state, spec, node_id, code, detail or outcome.value)

    def _record_failure(self, state: dict[str, Any], spec: GraphSpec, node_id: str, code: ErrorCode, message: str) -> None:
        node_state = state["nodes"][node_id]
        node_state["wait"] = None
        node_state["last_error"] = {"code": code.value, "message": message}
        node_state["finished_at"] = node_state["finished_at"] or utc_now()
        if state["failure"] is None:
            state["failure"] = {"code": code.value, "node": node_id, "message": message}
        self.store.append_trace(state["run_id"], "node.failed", node=node_id, code=code.value, message=message)
        if spec.node(node_id)["on_failure"] == "stop":
            state["stopped"] = True
            for other_id, other in state["nodes"].items():
                if other["status"] == NodeStatus.RUNNING.value and other["wait"] in ("submission", "attention", "review"):
                    other["status"] = NodeStatus.SKIPPED.value
                    other["wait"] = None
                    other["last_error"] = {"code": None, "message": "run stopped after %s failed" % node_id}

    def _settle(self, state: dict[str, Any], spec: GraphSpec) -> None:
        self._refresh_readiness(state, spec)
        if RunStatus(state["status"]) in RUN_TERMINAL:
            state["holds"] = []
            return
        nodes = state["nodes"]
        if all(NodeStatus(ns["status"]) in NODE_TERMINAL for ns in nodes.values()):
            succeeded = all(ns["status"] == NodeStatus.SUCCEEDED.value for ns in nodes.values())
            state["status"] = (RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED).value
            state["holds"] = []
            state["finished_at"] = utc_now()
            self.store.append_trace(state["run_id"], "run.finished", status=state["status"])
            return
        holds = []
        wait_kind = {
            "approval": HoldState.WAITING_FOR_APPROVAL,
            "submission": HoldState.WAITING_FOR_SUBMISSION,
            "review": HoldState.WAITING_FOR_REVIEW,
            "attention": HoldState.NEEDS_ATTENTION,
        }
        for node_id in state["order"]:
            ns = nodes[node_id]
            if ns["wait"] in wait_kind:
                gate = None
                if ns["wait"] in ("approval", "review"):
                    gate = self._pending_gate(state, spec, node_id, ns["wait"])
                holds.append({"kind": wait_kind[ns["wait"]].value, "node": node_id, "gate": gate})
        if state.pop("budget_exhausted", False):
            holds.append({"kind": HoldState.NEEDS_ATTENTION.value, "node": None, "gate": None,
                          "detail": "step budget exhausted; resume to continue"})
        if not holds:
            holds.append({"kind": HoldState.NEEDS_ATTENTION.value, "node": None, "gate": None,
                          "detail": TerminalOutcome.DEADLOCKED.value})
        state["holds"] = holds
        state["status"] = RunStatus.WAITING.value

    # ------------------------------------------------------------------ reporting
    def status(self, run_id: str) -> dict[str, Any]:
        state, spec = self.load(run_id)
        return status_summary(state, spec)

    def verify(self, run_id: str) -> dict[str, Any]:
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            checks: list[dict[str, Any]] = []

            def check(name: str, passed: bool, detail: str) -> None:
                checks.append({"check": name, "passed": bool(passed), "detail": detail})

            check("state_integrity", True, "checksum and schema verified")
            check("spec_digest", True, spec.digest)
            ok, detail = self.store.verify_trace(run_id)
            check("trace_chain", ok, detail)
            check("run_succeeded", state["status"] == RunStatus.SUCCEEDED.value, "run status %s" % state["status"])
            plan = state["approvals"].get(PLAN_TARGET)
            if plan and plan.get("status") == "NOT_REQUIRED":
                check("plan_approval", True, "not required by configuration")
            else:
                valid = bool(plan) and plan.get("status") == "APPROVED" and \
                    plan.get("binding_digest") == self._binding(state, spec, PLAN_TARGET).digest()
                check("plan_approval", valid, "approved for this exact plan" if valid else "missing or not bound to this plan")
            for node in spec.nodes:
                node_id = node["id"]
                ns = state["nodes"][node_id]
                check("node:%s:status" % node_id, ns["status"] == NodeStatus.SUCCEEDED.value, ns["status"])
                outputs = ns.get("outputs")
                if ns["status"] == NodeStatus.SUCCEEDED.value:
                    errors = output_contract_errors(node["outputs"], outputs or {})
                    check("node:%s:output_contract" % node_id, not errors, "; ".join(errors) or "outputs match contract")
                    results = evaluate_criteria(node["success_criteria"], outputs or {})
                    failed = [r["detail"] for r in results if not r["passed"]]
                    check("node:%s:success_criteria" % node_id, not failed,
                          "; ".join(failed) or "%d criteria passed" % len(results))
                for gate in node["gates"]:
                    target = "%s:%s" % (node_id, gate["id"])
                    record = state["approvals"].get(target)
                    valid = bool(record) and record.get("status") == "APPROVED" and \
                        record.get("binding_digest") == self._binding(state, spec, target).digest()
                    check("gate:%s" % target, valid, "approved" if valid else "not approved for this binding")
            verified = all(c["passed"] for c in checks)
            outcome = {
                RunStatus.SUCCEEDED.value: TerminalOutcome.SUCCEEDED.value,
                RunStatus.FAILED.value: TerminalOutcome.FAILED.value,
                RunStatus.CANCELLED.value: TerminalOutcome.CANCELLED.value,
            }.get(state["status"])
            receipt = {
                "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": run_id,
                "graph_id": spec.graph_id,
                "spec_digest": spec.digest,
                "run_status": state["status"],
                "terminal_outcome": outcome,
                "verified": verified,
                "checks": checks,
                "verified_at": utc_now(),
            }
            self.store.write_receipt(run_id, receipt)
            self.store.append_trace(run_id, "run.verified", verified=verified,
                                    failed_checks=[c["check"] for c in checks if not c["passed"]])
            state["verification"] = {"verified": verified, "verified_at": receipt["verified_at"],
                                     "failed_checks": [c["check"] for c in checks if not c["passed"]]}
            self._save(state)
            return receipt


# ---------------------------------------------------------------------- views
def plan_view(spec: GraphSpec) -> dict[str, Any]:
    """Execution plan: order, parallel layers, gates, ownership, rollback boundaries."""
    depth: dict[str, int] = {}
    for node_id in spec.order:
        deps = spec.node(node_id)["depends_on"]
        depth[node_id] = 1 + max((depth[d] for d in deps), default=-1)
    layers: dict[int, list[str]] = {}
    for node_id in spec.order:
        layers.setdefault(depth[node_id], []).append(node_id)
    boundaries: dict[str, list[str]] = {}
    for node in spec.nodes:
        if node["rollback_boundary"]:
            boundaries.setdefault(node["rollback_boundary"], []).append(node["id"])
    return {
        "graph_id": spec.graph_id,
        "title": spec.data["title"],
        "spec_digest": spec.digest,
        "execution_order": list(spec.order),
        "layers": [layers[level] for level in sorted(layers)],
        "nodes": [
            {
                "id": node["id"],
                "name": node["name"],
                "purpose": node["purpose"],
                "depends_on": node["depends_on"],
                "executor": node["executor"],
                "requires": node["requires"],
                "owner": node["owner"],
                "inputs": node["inputs"],
                "outputs": node["outputs"],
                "success_criteria": node["success_criteria"],
                "on_failure": node["on_failure"],
                "retry": node["retry"],
                "idempotent": node["idempotent"],
                "side_effects": node["side_effects"],
                "gates": node["gates"],
                "rollback_boundary": node["rollback_boundary"],
            }
            for node in (spec.node(node_id) for node_id in spec.order)
        ],
        "gates": [
            {"target": "plan", "type": "approval", "owner": "operator"}
        ] + [
            {"target": "%s:%s" % (node["id"], gate["id"]), "type": gate["type"], "owner": gate["owner"]}
            for node in (spec.node(node_id) for node_id in spec.order) for gate in node["gates"]
        ],
        "rollback_boundaries": boundaries,
    }


def explain_view(spec: GraphSpec) -> dict[str, Any]:
    """Why each node, dependency and gate exists."""
    explained = []
    for node_id in spec.order:
        node = spec.node(node_id)
        consumed: dict[str, list[str]] = {}
        for input_name, input_spec in node["inputs"].items():
            src = input_spec["from"]
            if not src.startswith("graph.inputs."):
                consumed.setdefault(src.partition(".")[0], []).append("%s <- %s" % (input_name, src))
        dependencies = []
        for dep in node["depends_on"]:
            if dep in consumed:
                reason = "consumes %s" % ", ".join(consumed[dep])
            else:
                reason = "ordering constraint: %s must succeed first" % dep
            dependencies.append({"node": dep, "reason": reason})
        gates = []
        for gate in node["gates"]:
            if gate["type"] == "approval":
                reason = ("side effects require explicit human approval before execution" if node["side_effects"]
                          else "declared approval checkpoint before execution")
            else:
                reason = "outputs must be reviewed by a human before the node counts as succeeded"
            if gate["description"]:
                reason += " (%s)" % gate["description"]
            gates.append({"gate": gate["id"], "type": gate["type"], "owner": gate["owner"], "reason": reason})
        if node["executor"] == "builtin":
            execution = "deterministic builtin operation %s" % node["operation"]["op"]
        else:
            execution = "host agent work; the host routes it to a suitable model/tooling"
        explained.append({
            "id": node_id,
            "name": node["name"],
            "why": node["purpose"],
            "execution": execution,
            "requires": node["requires"],
            "dependencies": dependencies,
            "gates": gates,
            "success_criteria": node["success_criteria"],
            "failure": "stop the run" if node["on_failure"] == "stop" else "block dependents, continue other branches",
            "retry": node["retry"]["max_attempts"],
            "rollback_boundary": node["rollback_boundary"],
        })
    return {"graph_id": spec.graph_id, "title": spec.data["title"], "nodes": explained}


def status_summary(state: Mapping[str, Any], spec: GraphSpec) -> dict[str, Any]:
    nodes = state["nodes"]
    by_status: dict[str, list[str]] = {s.value: [] for s in NodeStatus}
    for node_id in state["order"]:
        by_status[nodes[node_id]["status"]].append(node_id)
    current = [nid for nid in state["order"] if nodes[nid]["status"] == NodeStatus.RUNNING.value]
    if not current:
        current = [nid for nid in state["order"] if nodes[nid]["status"] == NodeStatus.READY.value]
    gates = [{"target": "plan", "status": (state["approvals"].get(PLAN_TARGET) or {}).get("status", "PENDING")}]
    for node_id in state["order"]:
        for gate in spec.node(node_id)["gates"]:
            target = "%s:%s" % (node_id, gate["id"])
            record = state["approvals"].get(target)
            if record:
                gate_status = record["status"]
            elif nodes[node_id]["wait"] in ("approval", "review"):
                gate_status = "PENDING"
            else:
                gate_status = "NOT_REACHED"
            gates.append({"target": target, "type": gate["type"], "status": gate_status})
    failed = [
        {"node": nid, "error": nodes[nid]["last_error"]}
        for nid in by_status[NodeStatus.FAILED.value]
    ]
    blocked = [
        {"node": nid, "reason": (nodes[nid]["last_error"] or {}).get("message")}
        for nid in by_status[NodeStatus.BLOCKED.value]
    ]
    return {
        "run_id": state["run_id"],
        "graph_id": state["graph_id"],
        "title": state["title"],
        "status": state["status"],
        "current_nodes": current,
        "completed_nodes": by_status[NodeStatus.SUCCEEDED.value],
        "blocked_nodes": blocked,
        "failed_nodes": failed,
        "skipped_nodes": by_status[NodeStatus.SKIPPED.value],
        "nodes": {nid: {"status": nodes[nid]["status"], "attempts": nodes[nid]["attempts"],
                        "wait": nodes[nid]["wait"]} for nid in state["order"]},
        "holds": state["holds"],
        "gates": gates,
        "failure": state["failure"],
        "verification": state.get("verification"),
        "updated_at": state["updated_at"],
    }


def pending_work(state: Mapping[str, Any], spec: GraphSpec) -> list[dict[str, Any]]:
    """Agent-facing work orders for nodes awaiting submission."""
    orders = []
    for node_id in state["order"]:
        ns = state["nodes"][node_id]
        if ns["status"] == NodeStatus.RUNNING.value and ns["wait"] == "submission":
            node = spec.node(node_id)
            orders.append({
                "run_id": state["run_id"],
                "node": node_id,
                "name": node["name"],
                "purpose": node["purpose"],
                "instructions": node["instructions"],
                "requires": node["requires"],
                "inputs": ns["inputs"],
                "outputs": node["outputs"],
                "success_criteria": node["success_criteria"],
                "attempt": ns["attempts"],
                "max_attempts": node["retry"]["max_attempts"],
            })
    return orders
