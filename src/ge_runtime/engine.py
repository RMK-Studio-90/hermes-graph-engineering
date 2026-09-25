"""Graph execution engine: run lifecycle, gates, execution, recovery, finalization.

Invariants
----------
* A run executes only after its plan is approved (by an operator, or by a
  digest-bound policy decision; see ``ge_runtime.policy``), unless the host
  disables plan approval explicitly.
* Before a node runs: dependencies SUCCEEDED, approval gates (declared and
  policy gates) approved for the exact binding, executor available, input
  contract satisfied, policy does not deny it.
* A node is RUNNING on disk *before* its executor is called (write-ahead), so
  an interruption is always visible after a restart.
* After execution the output contract, every success criterion and every
  declared evidence check are evaluated; an executor (or agent) reporting
  success is never enough on its own.
* Recovery never replays a non-idempotent node automatically; it holds the node
  in NEEDS_ATTENTION until an operator decides.
* Every status change goes through the transition tables in ``states``.
* Every mutation reconciles the state with its journal first (torn tails are
  repaired, damaged journals quarantined, a state older than its journal is
  refused) and a run that becomes terminal is finalized with a checksummed
  receipt; an interrupted finalization is completed on the next access.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from .adapters.types import ApprovalBinding, ExecutionRequest, ExecutorOutcome, ExecutorResult
from .contracts import evaluate_criteria, is_type, output_contract_errors
from .errors import ErrorCode, GraphEngineeringError
from .executors import ExecutorCatalog
from .spec import GraphSpec, admit, digest_of
from .states import (
    NODE_TERMINAL,
    RUN_TERMINAL,
    HoldState,
    NodeStatus,
    RunStatus,
    TerminalOutcome,
    check_node_transition,
    check_run_transition,
)
from .store import RunStore, StateStore, parse_ts, utc_now
from .version import RECEIPT_SCHEMA_VERSION, RUN_STATE_SCHEMA_VERSION, __version__

MAX_SUBMISSION_BYTES = 1_000_000
MAX_DIAGNOSIS_CHARS = 6_000
PLAN_TARGET = "plan"
POLICY_GATE = "policy"
_RETRYABLE = {ExecutorOutcome.RETRYABLE_FAILURE, ExecutorOutcome.TIMEOUT}
# journal events that change durable state; seeing them after the state's journal head means a
# crash hit between the event and the state save
_STATE_CHANGING_EVENTS = frozenset({
    "node.submitted", "node.succeeded", "node.failed", "node.attempt_failed", "approval.decided",
    "run.finished", "run.cancelled", "run.revised", "node.repair_authorized",
})


def _err(code: ErrorCode, message: str, **details: Any) -> GraphEngineeringError:
    return GraphEngineeringError(code, message, details or None)


def new_node_state() -> dict[str, Any]:
    return {
        "status": NodeStatus.PENDING.value,
        "attempts": 0,
        "wait": None,
        "inputs": None,
        "outputs": None,
        "criteria": [],
        "evidence": [],
        "last_error": None,
        "started_at": None,
        "finished_at": None,
        "history": [],
        "repair": None,
    }


class GraphEngine:
    def __init__(
        self,
        store: StateStore,
        catalog: ExecutorCatalog | None = None,
        *,
        require_plan_approval: bool = True,
        max_steps: int = 500,
        evidence: Any = "default",
        policy: Any = "default",
    ) -> None:
        self.store = store
        self.catalog = catalog or ExecutorCatalog()
        self.require_plan_approval = require_plan_approval
        self.max_steps = max_steps
        # collaborators (see ge_runtime.evidence / ge_runtime.policy); None disables them
        if evidence == "default":
            from .evidence import EvidenceRunner

            evidence = EvidenceRunner()
        if policy == "default":
            from .policy import PolicyVerifier

            policy = PolicyVerifier()
        self.evidence = evidence
        if evidence is not None and getattr(evidence, "store", None) is None:
            evidence.store = store
        self.policy = policy

    # ------------------------------------------------------------------ admission
    def validate(self, raw_spec: Any) -> GraphSpec:
        return admit(raw_spec, self.catalog)

    def create(self, raw_spec: Any, *, created_by: str = "unknown", autopilot: Mapping[str, Any] | None = None,
               workspace: str | None = None) -> dict[str, Any]:
        spec = self.validate(raw_spec)
        run_id = self.store.new_run_id(spec.graph_id)
        now = utc_now()
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
            "terminal_outcome": None,
            "approvals": approvals,
            "nodes": {node["id"]: new_node_state() for node in spec.nodes},
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "verification": None,
            "revision": 0,
            "journal": None,
            "recovery": [],
            "finalization": None,
            "policy": None,
            "autopilot": dict(autopilot) if autopilot else None,
            "workspace": workspace,
            "spec_revisions": [],
            "runtime_version": __version__,
        }
        with self.store.lock(run_id):
            self.store.append_trace(run_id, "run.created", spec_digest=spec.digest, created_by=created_by)
            self._save(state)
        return state

    # ------------------------------------------------------------------ loading
    def load(self, run_id: str) -> tuple[dict[str, Any], GraphSpec]:
        """Read-only load: checksum, schema, spec digest and node table are verified; nothing is repaired."""
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

    @contextmanager
    def mutate(self, run_id: str) -> Iterator[tuple[dict[str, Any], GraphSpec]]:
        """Lock the run, load it, reconcile it with its journal, yield it; callers save explicitly."""
        with self.store.lock(run_id):
            state, spec = self.load(run_id)
            self._reconcile(state, spec)
            yield state, spec

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

    # ------------------------------------------------------------------ journal integrity
    def inspect_integrity(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only comparison of a state with its journal."""
        scan = self.store.scan_trace(state["run_id"])
        violations = [r for r in state.get("recovery") or [] if r.get("severity") == "violation"]
        result: dict[str, Any] = {"journal": scan.status, "detail": scan.detail, "head": scan.head,
                                  "state_journal": state.get("journal"), "violations": len(violations),
                                  "consistent": scan.status in ("ok", "missing") and not violations}
        journal = state.get("journal")
        if result["consistent"] and journal and journal.get("seq"):
            events = scan.events
            if journal["seq"] > len(events) or events[journal["seq"] - 1].get("hash") != journal.get("hash"):
                result.update(consistent=False, detail="journal does not contain the state's head")
            elif any(e.get("type") == "state.saved" and (e.get("revision") or 0) > (state.get("revision") or 0)
                     for e in events[journal["seq"]:]):
                result.update(consistent=False, detail="state is older than its journal")
        return result

    def _record_recovery(self, state: dict[str, Any], kind: str, severity: str, **detail: Any) -> None:
        entry = {"kind": kind, "severity": severity, "at": utc_now()}
        entry.update(detail)
        state.setdefault("recovery", []).append(entry)

    def _reconcile(self, state: dict[str, Any], spec: GraphSpec) -> None:
        """Bring the state and its journal back into a provable relationship (called under the run lock)."""
        run_id = state["run_id"]
        store = self.store
        changed = False
        scan = store.scan_trace(run_id)
        if scan.status == "torn_tail":
            repaired = store.repair_torn_tail(run_id)
            store.append_trace(run_id, "trace.repaired", **repaired)
            self._record_recovery(state, "trace_torn_tail_repaired", "repaired", **{
                k: v for k, v in repaired.items() if k != "kind"})
            changed = True
        elif scan.status == "corrupt":
            quarantined = store.quarantine_trace(run_id, scan.detail)
            store.append_trace(run_id, "trace.quarantined", previous_state_journal=state.get("journal"),
                               valid_events=len(scan.events), **{k: v for k, v in quarantined.items() if k != "kind"})
            self._record_recovery(state, "trace_quarantined", "violation", reason=scan.detail,
                                  quarantine=quarantined["quarantine"], valid_events=len(scan.events))
            state["journal"] = None  # the provable history ends here; verification reports it
            changed = True
        journal = state.get("journal")
        if journal and journal.get("seq"):
            events = store.read_trace(run_id)
            if journal["seq"] > len(events):
                store.append_trace(run_id, "trace.gap_detected", expected_seq=journal["seq"], found_seq=len(events))
                self._record_recovery(state, "trace_truncated", "violation", expected_seq=journal["seq"],
                                      found_seq=len(events))
                changed = True
            elif events[journal["seq"] - 1].get("hash") != journal.get("hash"):
                store.append_trace(run_id, "trace.diverged", at_seq=journal["seq"])
                self._record_recovery(state, "trace_diverged", "violation", at_seq=journal["seq"])
                changed = True
            else:
                ahead = events[journal["seq"]:]
                newer = [e for e in ahead if e.get("type") == "state.saved"
                         and (e.get("revision") or 0) > (state.get("revision") or 0)]
                if newer:
                    raise _err(ErrorCode.STATE_DIVERGED,
                               "run %s state (revision %s) is older than its journal (revision %s was saved); "
                               "refusing to continue from a rolled-back state" % (
                                   run_id, state.get("revision"), newer[-1].get("revision")),
                               run_id=run_id, state_revision=state.get("revision"),
                               journal_revision=newer[-1].get("revision"))
                lost = [e for e in ahead if e.get("type") in _STATE_CHANGING_EVENTS]
                if lost:
                    store.append_trace(run_id, "state.reconciled", events=[
                        {"seq": e["seq"], "type": e["type"], "node": e.get("node")} for e in lost])
                    self._record_recovery(state, "state_behind_journal", "repaired", events=[
                        {"seq": e["seq"], "type": e["type"], "node": e.get("node")} for e in lost],
                        detail="updates after the last state save were lost in a crash; the saved state "
                               "is authoritative and interrupted work is recovered conservatively")
                    changed = True
        if state.pop("_migrated_from", None) == 1:
            store.append_trace(run_id, "state.migrated", from_schema=1, to_schema=RUN_STATE_SCHEMA_VERSION)
            self._record_recovery(state, "schema_migrated", "repaired", from_schema=1,
                                  to_schema=RUN_STATE_SCHEMA_VERSION)
            if RunStatus(state["status"]) in RUN_TERMINAL and not state.get("finalization"):
                state["finalization"] = {"status": "PENDING"}
            changed = True
        if changed:
            self._save(state)

    # ------------------------------------------------------------------ transitions
    @staticmethod
    def _run_to(state: dict[str, Any], status: RunStatus) -> None:
        state["status"] = check_run_transition(state["status"], status).value

    @staticmethod
    def _node_to(node_state: dict[str, Any], status: NodeStatus, *, revision: bool = False) -> None:
        node_state["status"] = check_node_transition(node_state["status"], status, revision=revision).value

    # ------------------------------------------------------------------ approvals
    def node_gates(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str) -> list[dict[str, Any]]:
        """Declared gates plus the policy gate a policy decision may require."""
        gates = list(spec.node(node_id)["gates"])
        decision = ((state.get("policy") or {}).get("decisions") or {}).get(node_id) or {}
        if decision.get("decision") == "REQUIRE_APPROVAL" and not decision.get("gated_by"):
            gates.insert(0, {"id": POLICY_GATE, "type": "approval", "owner": "operator",
                             "description": "policy: %s requires operator approval (%s)" % (
                                 decision.get("risk"), "; ".join(decision.get("reasons") or []))})
        return gates

    def _gate(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str, gate_id: str) -> dict[str, Any] | None:
        return next((g for g in self.node_gates(state, spec, node_id) if g["id"] == gate_id), None)

    def plan_binding(self, state: Mapping[str, Any], spec: GraphSpec) -> ApprovalBinding:
        return self._binding(state, spec, PLAN_TARGET)

    def _binding(self, state: Mapping[str, Any], spec: GraphSpec, target: str) -> ApprovalBinding:
        if target == PLAN_TARGET:
            action: dict[str, Any] = {"order": state["order"], "spec_digest": spec.digest}
        else:
            node_id, gate_id = target.split(":", 1)
            node = spec.node(node_id)
            gate = self._gate(state, spec, node_id, gate_id)
            if gate is None:
                raise _err(ErrorCode.INVALID_ARGUMENT, "node %s has no gate %r" % (node_id, gate_id))
            action = {"node": node, "gate": gate}
            if gate_id == POLICY_GATE:
                action["policy"] = ((state.get("policy") or {}).get("decisions") or {}).get(node_id)
            if gate["type"] == "review":
                action["outputs"] = state["nodes"][node_id].get("outputs")
        return ApprovalBinding(
            graph_id=spec.graph_id,
            spec_version=spec.digest,
            run_id=state["run_id"],
            node_or_transition=target,
            action_digest=digest_of(action),
        )

    def decide(self, run_id: str, target: str, *, approve: bool, decided_by: str,
               basis: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Record a decision for the plan or a ``<node>:<gate>`` gate.

        Operators decide through commands; the policy engine decides with ``decided_by`` starting
        with ``policy:`` and a ``basis`` that names the policy digest it applied.
        """
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise _err(ErrorCode.INVALID_ARGUMENT, "decided_by is required")
        with self.mutate(run_id) as (state, spec):
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
                gate = self._gate(state, spec, node_id, gate_id)
                if gate is None:
                    raise _err(ErrorCode.INVALID_ARGUMENT, "node %s has no gate %r" % (node_id, gate_id))
                node_state = state["nodes"][node_id]
                expected_wait = "approval" if gate["type"] == "approval" else "review"
                if node_state["wait"] != expected_wait or self._pending_gate(state, spec, node_id, gate["type"]) != gate_id:
                    raise _err(ErrorCode.INVALID_TRANSITION, "gate %s is not currently awaiting a decision" % target)
            binding = self._binding(state, spec, target)
            now = utc_now()
            record = {
                "status": "APPROVED" if approve else "DENIED",
                "binding": binding.to_dict(),
                "binding_digest": binding.digest(),
                "decided_by": decided_by,
                "decided_at": now,
            }
            if basis:
                record["basis"] = dict(basis)
            state["approvals"][target] = record
            self.store.append_trace(run_id, "approval.decided", target=target, approved=approve, decided_by=decided_by,
                                    binding_digest=binding.digest(), basis=dict(basis) if basis else None)
            if target == PLAN_TARGET:
                if approve:
                    self._run_to(state, RunStatus.APPROVED)
                else:
                    self._run_to(state, RunStatus.CANCELLED)
                    state["failure"] = {"code": ErrorCode.GATE_FAILED.value, "node": None, "message": "plan denied"}
                    if decided_by.startswith("policy:"):
                        state["failure"] = {"code": ErrorCode.POLICY_DENIED.value, "node": None,
                                            "message": "plan denied by policy: %s" % (basis or {}).get("reason", "")}
                        state["terminal_outcome"] = TerminalOutcome.POLICY_DENIED.value
                    self._skip_open_nodes(state, "plan denied")
                    state["finished_at"] = now
                    state["holds"] = []
                self._commit(state, spec)
                return state
            node_id, _, gate_id = target.partition(":")
            node_state = state["nodes"][node_id]
            if not approve:
                self._node_to(node_state, NodeStatus.FAILED)
                node_state["wait"] = None
                node_state["finished_at"] = now
                self._record_failure(state, spec, node_id, ErrorCode.GATE_FAILED, "gate %s denied by %s" % (gate_id, decided_by))
            elif node_state["wait"] == "approval":
                if self._pending_gate(state, spec, node_id, "approval") is None:
                    node_state["wait"] = None
            elif node_state["wait"] == "review" and self._pending_gate(state, spec, node_id, "review") is None:
                node_state["wait"] = None
                self._node_to(node_state, NodeStatus.SUCCEEDED)
                node_state["finished_at"] = now
                self.store.append_trace(run_id, "node.succeeded", node=node_id, attempt=node_state["attempts"])
            if RunStatus(state["status"]) in (RunStatus.RUNNING, RunStatus.WAITING):
                self._run_to(state, RunStatus.RUNNING)
                self._advance(state, spec)
            self._settle(state, spec)
            self._commit(state, spec)
            return state

    def _pending_gate(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str, gate_type: str) -> str | None:
        for gate in self.node_gates(state, spec, node_id):
            if gate["type"] != gate_type:
                continue
            record = state["approvals"].get("%s:%s" % (node_id, gate["id"]))
            if not record or record.get("status") != "APPROVED":
                return gate["id"]
            if record.get("binding_digest") != self._binding(state, spec, "%s:%s" % (node_id, gate["id"])).digest():
                return gate["id"]
        return None

    def _denied_gate(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str) -> str | None:
        for gate in self.node_gates(state, spec, node_id):
            record = state["approvals"].get("%s:%s" % (node_id, gate["id"]))
            if record and record.get("status") == "DENIED":
                return gate["id"]
        return None

    # ------------------------------------------------------------------ execution
    def execute(self, run_id: str, *, max_steps: int | None = None) -> dict[str, Any]:
        with self.mutate(run_id) as (state, spec):
            status = RunStatus(state["status"])
            if status is RunStatus.DRAFT:
                raise _err(ErrorCode.GATE_FAILED, "plan of run %s is not approved; approve target 'plan' first" % run_id,
                           run_id=run_id, gate="plan")
            if status in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, status.value))
            self._preflight_executors(state, spec)
            if status is RunStatus.APPROVED:
                self.store.append_trace(run_id, "run.started")
            self._run_to(state, RunStatus.RUNNING)
            self._advance(state, spec, max_steps)
            self._settle(state, spec)
            self._commit(state, spec)
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
        operator: bool = False,
        usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deliver the result of a deferred (agent) node.

        ``operator=True`` (operator commands only) may also settle a node held after an
        interrupted dispatch (``DISPATCH_INTERRUPTED``), which the operator has inspected.
        """
        if not failed:
            try:
                size = len(json.dumps(outputs, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise _err(ErrorCode.INVALID_ARGUMENT, "outputs must be JSON-serializable") from exc
            if size > MAX_SUBMISSION_BYTES:
                raise _err(ErrorCode.INVALID_ARGUMENT, "outputs exceed %d bytes" % MAX_SUBMISSION_BYTES)
        with self.mutate(run_id) as (state, spec):
            if node_id not in state["nodes"]:
                raise _err(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)
            node_state = state["nodes"][node_id]
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (run_id, state["status"]))
            interrupted = (operator and node_state["wait"] == "attention"
                           and (node_state.get("last_error") or {}).get("code") == ErrorCode.DISPATCH_INTERRUPTED.value)
            if node_state["status"] != NodeStatus.RUNNING.value or (node_state["wait"] != "submission" and not interrupted):
                raise _err(ErrorCode.INVALID_TRANSITION, "node %s is not awaiting a submission (status %s)" % (
                    node_id, node_state["status"]))
            self.store.append_trace(run_id, "node.submitted", node=node_id, attempt=node_state["attempts"],
                                    failed=bool(failed), submitted_by=submitted_by)
            node_state["wait"] = None
            if usage:
                self._account_usage(state, usage)
            if failed:
                result = ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail=detail or "reported failed")
            elif not isinstance(outputs, Mapping):
                result = ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail="outputs must be an object")
            else:
                result = ExecutorResult(ExecutorOutcome.SUCCESS, outputs=outputs)
            if RunStatus(state["status"]) is not RunStatus.RUNNING:
                self._run_to(state, RunStatus.RUNNING)
            self._complete_attempt(state, spec, node_id, result)
            self._advance(state, spec)
            self._settle(state, spec)
            self._commit(state, spec)
            return state

    @staticmethod
    def _account_usage(state: dict[str, Any], usage: Mapping[str, Any]) -> None:
        autopilot = state.get("autopilot")
        if not isinstance(autopilot, dict):
            return
        spent = autopilot.setdefault("usage", {})
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            spent["cost"] = round(float(spent.get("cost") or 0.0) + float(cost), 6)

    def resume(self, run_id: str) -> dict[str, Any]:
        """Recover an interrupted run conservatively, then continue if its plan is approved."""
        with self.mutate(run_id) as (state, spec):
            status = RunStatus(state["status"])
            if status in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, status.value))
            recovered = []
            for node in spec.nodes:
                node_state = state["nodes"][node["id"]]
                if node_state["status"] != NodeStatus.RUNNING.value or node_state["wait"] is not None:
                    continue
                if node["idempotent"] and not node["side_effects"]:
                    self._node_to(node_state, NodeStatus.READY)
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
                self._run_to(state, RunStatus.RUNNING)
                self._advance(state, spec)
                self._settle(state, spec)
            self._commit(state, spec)
            state["recovered"] = recovered
            return state

    def retry_node(self, run_id: str, node_id: str, *, decided_by: str) -> dict[str, Any]:
        """Operator decision: re-run a node held in NEEDS_ATTENTION after an interruption."""
        with self.mutate(run_id) as (state, spec):
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (run_id, state["status"]))
            node_state = state["nodes"].get(node_id)
            if node_state is None:
                raise _err(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)
            if node_state["wait"] != "attention":
                raise _err(ErrorCode.INVALID_TRANSITION, "node %s is not held for operator attention" % node_id)
            node_state["wait"] = None
            self._node_to(node_state, NodeStatus.READY)
            self.store.append_trace(run_id, "node.retry_authorized", node=node_id, decided_by=decided_by)
            self._run_to(state, RunStatus.RUNNING)
            self._preflight_executors(state, spec)
            self._advance(state, spec)
            self._settle(state, spec)
            self._commit(state, spec)
            return state

    def reclaim_stale_dispatch(
        self, run_id: str, node_id: str, *,
        decided_by: str, force: bool = False, stale_after_seconds: float = 3600.0,
    ) -> dict[str, Any]:
        """Operator decision: release an abandoned host dispatch for a deferred node.

        * it NEVER marks the node successful and NEVER fabricates outputs;
        * it appends the same ``node.dispatch_released`` event the dispatcher emits, so
          claim/release accounting stays consistent;
        * it moves the node to ``NEEDS_ATTENTION`` so ``retry_node`` re-runs the same node.

        Fails closed unless every guard is satisfied; a dispatch whose lease is still live
        (heartbeat within its ttl) is never reclaimed, not even with ``force``.
        """
        from .dispatch import dispatch_claims, lease_is_live

        with self.mutate(run_id) as (state, spec):
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
            if any(e.get("type") in ("node.succeeded", "node.failed") for e in events):
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s has a terminal attempt event; refusing to reclaim" % node_id)
            claims, released = dispatch_claims(events)
            if not claims:
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s has no outstanding dispatch claim; nothing to reclaim" % node_id)
            if len(released) >= len(claims):
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s dispatch is already released; nothing to reclaim (duplicate)" % node_id)
            lease = self.store.read_lease(run_id)
            if lease_is_live(lease):
                raise _err(ErrorCode.DISPATCH_IN_PROGRESS,
                           "node %s is being executed by a live dispatcher (%s); refusing to reclaim" % (
                               node_id, (lease or {}).get("owner")))
            stale = force
            if not stale:
                latest = max((parse_ts(e.get("ts")) or 0.0 for e in claims), default=0.0)
                import time as _time
                stale = bool(latest) and _time.time() - latest >= stale_after_seconds
            if not stale:
                raise _err(ErrorCode.INVALID_TRANSITION,
                           "node %s dispatch is not stale yet (or --force needed)" % node_id)
            dispatched = claims[-1].get("dispatch", len(claims))
            self.store.append_trace(
                run_id, "node.dispatch_released", node=node_id, attempt=attempt,
                dispatch=dispatched, reason="stale_dispatch_recovery", released_by=decided_by,
                previous_wait="submission", evidence="no_host_result",
            )
            node_state["wait"] = "attention"
            node_state["last_error"] = {"code": ErrorCode.DISPATCH_INTERRUPTED.value,
                                        "message": "STALE_DISPATCH_RECOVERED"}
            node_state["history"].append({
                "attempt": attempt, "outcome": "STALE_DISPATCH_RECOVERED",
                "detail": "stale host dispatch released by %s" % decided_by, "at": utc_now(),
            })
            self._settle(state, spec)
            self._commit(state, spec)
            return state

    def terminate(self, run_id: str, *, outcome: TerminalOutcome, code: ErrorCode, reason: str,
                  decided_by: str) -> dict[str, Any]:
        """End a run that must not continue (for example an exhausted autopilot budget).

        Open nodes are skipped; the run ends FAILED (CANCELLED if its plan was never approved) with
        ``terminal_outcome`` set, and is finalized with a receipt like any other terminal run.
        """
        with self.mutate(run_id) as (state, spec):
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, state["status"]))
            self._skip_open_nodes(state, reason)
            if RunStatus(state["status"]) is RunStatus.DRAFT:
                self._run_to(state, RunStatus.CANCELLED)
            else:
                if RunStatus(state["status"]) is RunStatus.APPROVED:
                    self._run_to(state, RunStatus.RUNNING)
                self._run_to(state, RunStatus.FAILED)
            state["terminal_outcome"] = outcome.value
            state["failure"] = {"code": code.value, "node": None, "message": reason}
            state["holds"] = []
            state["finished_at"] = utc_now()
            self.store.append_trace(run_id, "run.terminated", outcome=outcome.value, code=code.value, reason=reason,
                                    decided_by=decided_by)
            self._commit(state, spec)
            return state

    def update_autopilot(self, run_id: str, changes: Mapping[str, Any], *, event: str | None = None,
                         **fields: Any) -> dict[str, Any]:
        """Merge autopilot bookkeeping into the run (and journal it) under the run lock."""
        with self.mutate(run_id) as (state, spec):
            autopilot = dict(state.get("autopilot") or {})
            for key, value in changes.items():
                if isinstance(value, Mapping) and isinstance(autopilot.get(key), Mapping):
                    merged = dict(autopilot[key])
                    merged.update(value)
                    autopilot[key] = merged
                else:
                    autopilot[key] = value
            state["autopilot"] = autopilot
            if event:
                self.store.append_trace(run_id, event, **fields)
            self._save(state)
            return state

    def cancel(self, run_id: str, *, decided_by: str) -> dict[str, Any]:
        with self.mutate(run_id) as (state, spec):
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is already %s" % (run_id, state["status"]))
            self._skip_open_nodes(state, "run cancelled")
            self._run_to(state, RunStatus.CANCELLED)
            state["holds"] = []
            state["finished_at"] = utc_now()
            self.store.append_trace(run_id, "run.cancelled", decided_by=decided_by)
            self._commit(state, spec)
            return state

    # ------------------------------------------------------------------ repair and revision
    def repair_node(self, run_id: str, node_id: str, *, diagnosis: Mapping[str, Any], decided_by: str) -> dict[str, Any]:
        """Autopilot decision: start a new attempt of a node waiting for repair, with its diagnosis."""
        with self.mutate(run_id) as (state, spec):
            node_state = self._repairing(state, node_id)
            text = json.dumps(dict(diagnosis), sort_keys=True, ensure_ascii=False, default=str)
            if len(text) > MAX_DIAGNOSIS_CHARS:
                diagnosis = {"summary": text[:MAX_DIAGNOSIS_CHARS - 20] + "...", "truncated": True}
            repair = dict(node_state.get("repair") or {})
            repair.update(count=int(repair.get("count") or 0) + 1, diagnosis=dict(diagnosis), at=utc_now(),
                          decided_by=decided_by)
            node_state["repair"] = repair
            node_state["wait"] = None
            self._node_to(node_state, NodeStatus.READY)
            self.store.append_trace(run_id, "node.repair_authorized", node=node_id, repair=repair["count"],
                                    decided_by=decided_by, diagnosis_digest=digest_of(repair["diagnosis"]))
            self._run_to(state, RunStatus.RUNNING)
            self._advance(state, spec)
            self._settle(state, spec)
            self._commit(state, spec)
            return state

    def give_up_node(self, run_id: str, node_id: str, *, reason: str, code: ErrorCode = ErrorCode.NODE_FAILED,
                     decided_by: str = "autopilot") -> dict[str, Any]:
        """Autopilot decision: a node waiting for repair cannot be repaired; apply its failure policy."""
        with self.mutate(run_id) as (state, spec):
            node_state = self._repairing(state, node_id)
            node_state["wait"] = None
            self._node_to(node_state, NodeStatus.FAILED)
            node_state["finished_at"] = utc_now()
            self.store.append_trace(run_id, "node.repair_abandoned", node=node_id, reason=reason, decided_by=decided_by)
            self._record_failure(state, spec, node_id, code, reason)
            if code is ErrorCode.BUDGET_EXHAUSTED:
                state["terminal_outcome"] = TerminalOutcome.BUDGET_EXHAUSTED.value
            if RunStatus(state["status"]) is RunStatus.WAITING:
                self._run_to(state, RunStatus.RUNNING)
            self._advance(state, spec)
            self._settle(state, spec)
            self._commit(state, spec)
            return state

    @staticmethod
    def _repairing(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        if RunStatus(state["status"]) in RUN_TERMINAL:
            raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (state["run_id"], state["status"]))
        node_state = state["nodes"].get(node_id)
        if node_state is None:
            raise _err(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)
        if node_state["status"] != NodeStatus.REPAIRING.value:
            raise _err(ErrorCode.INVALID_TRANSITION, "node %s is not waiting for repair" % node_id)
        return node_state

    def revise(self, run_id: str, raw_spec: Any, *, reason: str, decided_by: str,
               approval: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Replace the plan with a revised spec (replanning), keeping proven work.

        A node keeps its state only if its definition is unchanged, it SUCCEEDED and every
        upstream node is kept too; all other nodes start fresh (PENDING). The previous plan
        approval is bound to the previous digest and therefore no longer applies: the run
        returns to DRAFT unless ``approval`` carries a policy decision for the new plan
        (``{"decided_by": "policy:...", "basis": {...}}``), which is recorded digest-bound.
        """
        new_spec = self.validate(raw_spec)
        with self.mutate(run_id) as (state, spec):
            if RunStatus(state["status"]) in RUN_TERMINAL:
                raise _err(ErrorCode.INVALID_TRANSITION, "run %s is %s" % (run_id, state["status"]))
            if new_spec.graph_id != spec.graph_id:
                raise _err(ErrorCode.INVALID_ARGUMENT, "a revision must keep graph_id %s" % spec.graph_id)
            old_nodes = {n["id"]: n for n in spec.nodes}
            kept: list[str] = []
            reset: list[str] = []
            nodes: dict[str, Any] = {}
            for node_id in new_spec.order:
                node = new_spec.node(node_id)
                old = old_nodes.get(node_id)
                old_state = state["nodes"].get(node_id)
                unchanged = old is not None and digest_of(old) == digest_of(node)
                if (unchanged and old_state["status"] in (NodeStatus.SUCCEEDED.value, NodeStatus.RUNNING.value)
                        and all(dep in kept for dep in node["depends_on"])):
                    nodes[node_id] = old_state  # proven or in-flight work of an unchanged node is kept
                    kept.append(node_id)
                    continue
                if old_state is not None and old_state["status"] == NodeStatus.RUNNING.value:
                    raise _err(ErrorCode.INVALID_TRANSITION, "node %s is in flight; revise after it settles" % node_id)
                fresh = new_node_state()
                if old_state is not None:
                    fresh["history"] = list(old_state.get("history") or [])
                    fresh["history"].append({"attempt": old_state.get("attempts", 0), "outcome": "REVISED",
                                             "detail": reason[:300], "at": utc_now()})
                    fresh["repair"] = old_state.get("repair")
                nodes[node_id] = fresh
                reset.append(node_id)
            dropped = sorted(set(old_nodes) - set(new_spec.order))
            revision = {"revision": len(state.get("spec_revisions") or []) + 1, "from_digest": spec.digest,
                        "to_digest": new_spec.digest, "reason": reason, "decided_by": decided_by, "at": utc_now(),
                        "kept": kept, "reset": reset, "dropped": dropped}
            state.setdefault("spec_revisions", []).append(revision)
            state["spec"] = new_spec.data
            state["spec_digest"] = new_spec.digest
            state["order"] = list(new_spec.order)
            state["title"] = new_spec.data["title"]
            state["nodes"] = nodes
            state["stopped"] = False
            state["failure"] = None
            state["holds"] = []
            for target in [t for t in state["approvals"] if t != PLAN_TARGET and t.split(":", 1)[0] not in kept]:
                del state["approvals"][target]
            self.store.append_trace(run_id, "run.revised", **{k: v for k, v in revision.items() if k != "at"})
            plan = state["approvals"].get(PLAN_TARGET)
            if plan and plan.get("status") == "NOT_REQUIRED":
                pass
            elif approval:
                binding = self._binding(state, new_spec, PLAN_TARGET)
                state["approvals"][PLAN_TARGET] = {
                    "status": "APPROVED", "binding": binding.to_dict(), "binding_digest": binding.digest(),
                    "decided_by": approval["decided_by"], "decided_at": utc_now(),
                    "basis": dict(approval.get("basis") or {})}
                self.store.append_trace(run_id, "approval.decided", target=PLAN_TARGET, approved=True,
                                        decided_by=approval["decided_by"], binding_digest=binding.digest(),
                                        basis=dict(approval.get("basis") or {}))
            else:
                state["approvals"].pop(PLAN_TARGET, None)
                self._run_to(state, RunStatus.DRAFT)
            self._settle(state, new_spec)
            self._commit(state, new_spec)
            return state

    # ------------------------------------------------------------------ internals
    def _save(self, state: dict[str, Any]) -> None:
        state.pop("recovered", None)
        state["updated_at"] = utc_now()
        self.store.save(state["run_id"], state)

    def _commit(self, state: dict[str, Any], spec: GraphSpec) -> None:
        """Save; a run that just became terminal is finalized with a receipt (crash-safe, resumable)."""
        terminal = RunStatus(state["status"]) in RUN_TERMINAL
        finalization = state.get("finalization") or {}
        if terminal and finalization.get("status") != "COMPLETE":
            if state.get("terminal_outcome") is None:
                state["terminal_outcome"] = {
                    RunStatus.SUCCEEDED.value: TerminalOutcome.SUCCEEDED.value,
                    RunStatus.CANCELLED.value: TerminalOutcome.CANCELLED.value,
                }.get(state["status"], TerminalOutcome.FAILED.value)
            state["finalization"] = {"status": "PENDING", "since": utc_now()}
            self._save(state)  # durable intent before the receipt exists
            self._finalize(state, spec, generated_by="finalize")
            return
        self._save(state)

    def _skip_open_nodes(self, state: dict[str, Any], reason: str) -> None:
        for node_state in state["nodes"].values():
            if NodeStatus(node_state["status"]) not in NODE_TERMINAL:
                self._node_to(node_state, NodeStatus.SKIPPED)
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
                self._node_to(node_state, NodeStatus.SKIPPED)
                node_state["wait"] = None
                node_state["last_error"] = {"code": None, "message": "run stopped after a failure"}
                continue
            deps = [NodeStatus(nodes[dep]["status"]) for dep in spec.node(node_id)["depends_on"]]
            bad = [dep for dep in spec.node(node_id)["depends_on"]
                   if NodeStatus(nodes[dep]["status"]) in (NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.SKIPPED)]
            if bad:
                self._node_to(node_state, NodeStatus.BLOCKED)
                node_state["wait"] = None
                node_state["last_error"] = {"code": None, "message": "blocked by upstream %s" % ", ".join(bad)}
            elif status is NodeStatus.PENDING and all(dep is NodeStatus.SUCCEEDED for dep in deps):
                self._node_to(node_state, NodeStatus.READY)

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
            if input_spec.get("view"):
                try:
                    from .views import apply_view

                    value = apply_view(value, input_spec["view"], workspace=state.get("workspace"))
                except GraphEngineeringError as exc:
                    errors.append("input %r view failed: %s" % (name, exc.message))
                    continue
            values[name] = value
        return values, errors

    def _policy_block(self, state: dict[str, Any], node_id: str) -> str | None:
        decision = ((state.get("policy") or {}).get("decisions") or {}).get(node_id) or {}
        if decision.get("decision") == "DENY":
            return "policy denies %s (%s): %s" % (node_id, decision.get("risk"), "; ".join(decision.get("reasons") or []))
        return None

    def _advance(self, state: dict[str, Any], spec: GraphSpec, max_steps: int | None = None) -> None:
        budget = self.max_steps if max_steps is None else max_steps
        steps = 0
        state.pop("_budget_exhausted", None)
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
                    state["_budget_exhausted"] = True
                    return
                node = spec.node(node_id)
                blocked = self._policy_block(state, node_id)
                if blocked:
                    self._node_to(node_state, NodeStatus.FAILED)
                    node_state["finished_at"] = utc_now()
                    self._record_failure(state, spec, node_id, ErrorCode.POLICY_DENIED, blocked)
                    state["terminal_outcome"] = TerminalOutcome.POLICY_DENIED.value
                    progress = True
                    continue
                denied = self._denied_gate(state, spec, node_id)
                if denied:
                    self._node_to(node_state, NodeStatus.FAILED)
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
                    self._node_to(node_state, NodeStatus.FAILED)
                    node_state["criteria"] = []
                    self._record_failure(state, spec, node_id, ErrorCode.NODE_FAILED,
                                         "input contract violated: " + "; ".join(input_errors))
                    progress = True
                    continue
                executor = self.catalog.get(node["executor"])
                steps += 1
                self._node_to(node_state, NodeStatus.RUNNING)
                node_state["attempts"] += 1
                node_state["inputs"] = inputs
                node_state["started_at"] = utc_now()
                node_state["evidence"] = []
                if self.evidence is not None:
                    self.evidence.before_attempt(state, spec, node_id)
                # write-ahead: the attempt is durable before any executor runs
                self.store.append_trace(run_id, "node.started", node=node_id, attempt=node_state["attempts"],
                                        executor=node["executor"])
                if getattr(executor, "deferred", False):
                    node_state["wait"] = "submission"
                    self._save(state)
                    self.store.append_trace(run_id, "node.awaiting_submission", node=node_id,
                                            attempt=node_state["attempts"])
                    progress = True
                    continue
                self._save(state)
                progress = True
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

    def _repair_allowed(self, state: Mapping[str, Any], node: Mapping[str, Any], node_state: Mapping[str, Any],
                        outcome: ExecutorOutcome) -> bool:
        autopilot = state.get("autopilot")
        if not isinstance(autopilot, dict) or not autopilot.get("repair"):
            return False
        if outcome in (ExecutorOutcome.GOVERNANCE_BLOCKED, ExecutorOutcome.EXECUTOR_UNAVAILABLE,
                       ExecutorOutcome.CANCELLED):
            return False
        if not node["idempotent"] or node["side_effects"]:
            return False  # never repair work that may already have changed the outside world
        decision = ((state.get("policy") or {}).get("decisions") or {}).get(node["id"])
        if decision is not None and decision.get("decision") != "AUTO_APPROVE":
            return False  # repeating operator-approved work needs the operator
        return True  # the autopilot decides between repair, replan and giving up, within its budgets

    def _complete_attempt(self, state: dict[str, Any], spec: GraphSpec, node_id: str, result: ExecutorResult) -> None:
        node = spec.node(node_id)
        node_state = state["nodes"][node_id]
        run_id = state["run_id"]
        attempt = node_state["attempts"]
        now = utc_now()
        outcome = result.outcome
        detail = result.detail
        criteria: list[dict[str, Any]] = []
        failure_code = ErrorCode.NODE_FAILED
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
                evidence_problem = None
                if self.evidence is not None:
                    evidence_problem = self.evidence.after_attempt(state, spec, node_id, outputs)
                if evidence_problem:
                    code, message = evidence_problem
                    outcome, detail, failure_code = ExecutorOutcome.RETRYABLE_FAILURE, message, code
                else:
                    node_state["outputs"] = outputs
                    node_state["last_error"] = None
                    node_state["history"].append({"attempt": attempt, "outcome": "SUCCESS", "detail": "", "at": now})
                    if self._pending_gate(state, spec, node_id, "review"):
                        node_state["wait"] = "review"
                        self.store.append_trace(run_id, "gate.requested", node=node_id,
                                                gate=self._pending_gate(state, spec, node_id, "review"))
                        return
                    self._node_to(node_state, NodeStatus.SUCCEEDED)
                    node_state["finished_at"] = now
                    self.store.append_trace(run_id, "node.succeeded", node=node_id, attempt=attempt,
                                            outputs_digest=digest_of(outputs),
                                            evidence=[e.get("digest") for e in node_state.get("evidence") or []])
                    return
        if failure_code is ErrorCode.POLICY_VIOLATION:
            outcome = ExecutorOutcome.NON_RETRYABLE_FAILURE
        node_state["history"].append({"attempt": attempt, "outcome": outcome.value, "detail": detail, "at": now,
                                      "code": failure_code.value})
        retry_allowed = (
            outcome in _RETRYABLE
            and attempt < node["retry"]["max_attempts"]
            and node["idempotent"]
            and not node["side_effects"]
        )
        repair_allowed = (not retry_allowed and failure_code is not ErrorCode.POLICY_VIOLATION
                          and self._repair_allowed(state, node, node_state, outcome))
        self.store.append_trace(run_id, "node.attempt_failed", node=node_id, attempt=attempt, outcome=outcome.value,
                                detail=detail, code=failure_code.value, will_retry=retry_allowed,
                                will_repair=repair_allowed)
        if retry_allowed:
            self._node_to(node_state, NodeStatus.READY)
            node_state["last_error"] = {"code": failure_code.value, "message": detail}
            return
        if repair_allowed:
            self._node_to(node_state, NodeStatus.REPAIRING)
            node_state["wait"] = "repair"
            node_state["last_error"] = {"code": failure_code.value, "message": detail, "outcome": outcome.value}
            self.store.append_trace(run_id, "node.repair_requested", node=node_id, attempt=attempt,
                                    code=failure_code.value)
            return
        self._node_to(node_state, NodeStatus.FAILED)
        node_state["finished_at"] = now
        if outcome is ExecutorOutcome.EXECUTOR_UNAVAILABLE:
            failure_code = ErrorCode.EXECUTOR_UNAVAILABLE
        self._record_failure(state, spec, node_id, failure_code, detail or outcome.value)

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
                waiting = other["status"] == NodeStatus.RUNNING.value and other["wait"] in (
                    "submission", "attention", "review")
                if waiting or other["status"] == NodeStatus.REPAIRING.value:
                    self._node_to(other, NodeStatus.SKIPPED)
                    other["wait"] = None
                    other["last_error"] = {"code": None, "message": "run stopped after %s failed" % node_id}

    def _settle(self, state: dict[str, Any], spec: GraphSpec) -> None:
        self._refresh_readiness(state, spec)
        if RunStatus(state["status"]) in RUN_TERMINAL:
            state["holds"] = []
            return
        nodes = state["nodes"]
        if RunStatus(state["status"]) is RunStatus.DRAFT:
            state["holds"] = [{"kind": HoldState.WAITING_FOR_APPROVAL.value, "node": None, "gate": PLAN_TARGET}]
            return
        if all(NodeStatus(ns["status"]) in NODE_TERMINAL for ns in nodes.values()):
            succeeded = all(ns["status"] == NodeStatus.SUCCEEDED.value for ns in nodes.values())
            if succeeded and self.evidence is not None:
                problem = self.evidence.acceptance(state, spec)
                if problem:
                    code, message = problem
                    if state["failure"] is None:
                        state["failure"] = {"code": code.value, "node": None, "message": message}
                    succeeded = False
            if RunStatus(state["status"]) is RunStatus.APPROVED:
                self._run_to(state, RunStatus.RUNNING)
            self._run_to(state, RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED)
            state["holds"] = []
            state["finished_at"] = utc_now()
            self.store.append_trace(state["run_id"], "run.finished", status=state["status"])
            return
        holds = []
        wait_kind = {
            "approval": HoldState.WAITING_FOR_APPROVAL,
            "submission": HoldState.WAITING_FOR_SUBMISSION,
            "review": HoldState.WAITING_FOR_REVIEW,
            "repair": HoldState.WAITING_FOR_REPAIR,
            "attention": HoldState.NEEDS_ATTENTION,
        }
        for node_id in state["order"]:
            ns = nodes[node_id]
            if ns["wait"] in wait_kind:
                gate = None
                if ns["wait"] in ("approval", "review"):
                    gate = self._pending_gate(state, spec, node_id, ns["wait"])
                hold = {"kind": wait_kind[ns["wait"]].value, "node": node_id, "gate": gate}
                if ns["wait"] == "attention" and ns.get("last_error"):
                    hold["code"] = ns["last_error"].get("code")
                holds.append(hold)
        if state.pop("_budget_exhausted", False):
            holds.append({"kind": HoldState.NEEDS_ATTENTION.value, "node": None, "gate": None,
                          "detail": "step budget exhausted; resume to continue"})
        if not holds:
            holds.append({"kind": HoldState.NEEDS_ATTENTION.value, "node": None, "gate": None,
                          "detail": TerminalOutcome.DEADLOCKED.value})
        state["holds"] = holds
        if RunStatus(state["status"]) is not RunStatus.APPROVED:
            self._run_to(state, RunStatus.WAITING)

    # ------------------------------------------------------------------ verification and receipts
    def status(self, run_id: str) -> dict[str, Any]:
        state, spec = self.load(run_id)
        summary = status_summary(state, spec)
        summary["integrity"] = self.inspect_integrity(state)
        return summary

    def evaluate(self, state: Mapping[str, Any], spec: GraphSpec) -> list[dict[str, Any]]:
        """Deterministic verification checks over the persisted run (used by receipts and ``verify``)."""
        run_id = state["run_id"]
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        check("state_integrity", True, "checksum and schema verified")
        check("spec_digest", True, spec.digest)
        ok, detail = self.store.verify_trace(run_id)
        violations = [r for r in state.get("recovery") or [] if r.get("severity") == "violation"]
        broken = [v for v in violations if v.get("kind") in ("trace_quarantined", "trace_truncated", "trace_diverged")]
        if ok and broken:
            ok, detail = False, "journal history is not provable: %s (%s)" % (
                ", ".join(v["kind"] for v in broken), "; ".join(str(v.get("reason") or v.get("quarantine") or "")
                                                            for v in broken))
        check("trace_chain", ok, detail)
        integrity = self.inspect_integrity(state)
        check("journal_binding", integrity["consistent"],
              "state bound to journal head %s" % (integrity["head"],) if integrity["consistent"] else
              "%s; %d integrity violation(s): %s" % (integrity["detail"], len(violations),
                                                     ", ".join(v["kind"] for v in violations) or "-"))
        check("run_succeeded", state["status"] == RunStatus.SUCCEEDED.value, "run status %s" % state["status"])
        plan = state["approvals"].get(PLAN_TARGET)
        if plan and plan.get("status") == "NOT_REQUIRED":
            check("plan_approval", True, "not required by configuration")
        else:
            valid = bool(plan) and plan.get("status") == "APPROVED" and \
                plan.get("binding_digest") == self._binding(state, spec, PLAN_TARGET).digest()
            by = (plan or {}).get("decided_by", "-")
            check("plan_approval", valid, ("approved for this exact plan by %s" % by) if valid
                  else "missing or not bound to this plan")
        if self.policy is not None and state.get("policy"):
            ok, detail = self.policy.verify_record(state, spec, self)
            check("policy_decisions", ok, detail)
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
                if self.evidence is not None:
                    for name, passed, detail in self.evidence.verify_node(state, spec, node_id):
                        check("node:%s:%s" % (node_id, name), passed, detail)
            for gate in self.node_gates(state, spec, node_id):
                target = "%s:%s" % (node_id, gate["id"])
                if ns["status"] != NodeStatus.SUCCEEDED.value and target not in state["approvals"]:
                    check("gate:%s" % target, False, "not reached")
                    continue
                record = state["approvals"].get(target)
                valid = bool(record) and record.get("status") == "APPROVED" and \
                    record.get("binding_digest") == self._binding(state, spec, target).digest()
                check("gate:%s" % target, valid, "approved" if valid else "not approved for this binding")
        if self.evidence is not None and state["status"] == RunStatus.SUCCEEDED.value:
            for name, passed, detail in self.evidence.verify_acceptance(state, spec):
                check(name, passed, detail)
        return checks

    def _build_receipt(self, state: Mapping[str, Any], spec: GraphSpec, checks: list[dict[str, Any]],
                       generated_by: str) -> dict[str, Any]:
        verified = all(c["passed"] for c in checks)
        level = None
        if self.evidence is not None:
            level = self.evidence.verification_level(state, spec)
        nodes = {}
        for node_id in state["order"]:
            ns = state["nodes"][node_id]
            nodes[node_id] = {
                "status": ns["status"], "attempts": ns["attempts"], "history": ns.get("history") or [],
                "outputs_digest": digest_of(ns["outputs"]) if ns.get("outputs") is not None else None,
                "criteria": ns.get("criteria") or [], "evidence": ns.get("evidence") or [],
                "last_error": ns.get("last_error"), "repair": ns.get("repair"),
            }
        return {
            "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": "ge-run-receipt",
            "run_id": state["run_id"],
            "graph_id": spec.graph_id,
            "title": state.get("title"),
            "spec_digest": spec.digest,
            "run_status": state["status"],
            "terminal_outcome": state.get("terminal_outcome") if RunStatus(state["status"]) in RUN_TERMINAL else None,
            "failure": state.get("failure"),
            "verified": verified,
            "verification_level": level,
            "checks": checks,
            "state_revision": state.get("revision"),
            "state_checksum": state.get("_checksum"),
            "journal_head": self.store.scan_trace(state["run_id"]).head,
            "history": {
                "nodes": nodes,
                "approvals": state.get("approvals"),
                "spec_revisions": state.get("spec_revisions") or [],
                "policy": state.get("policy"),
                "autopilot": _autopilot_summary(state.get("autopilot")),
            },
            "recovery": state.get("recovery") or [],
            "generated_by": generated_by,
            "runtime_version": __version__,
            "created_at": utc_now(),
        }

    def _finalize(self, state: dict[str, Any], spec: GraphSpec, *, generated_by: str) -> dict[str, Any]:
        run_id = state["run_id"]
        checks = self.evaluate(state, spec)
        receipt = self.store.write_receipt(run_id, self._build_receipt(state, spec, checks, generated_by))
        failed = [c["check"] for c in checks if not c["passed"]]
        self.store.append_trace(run_id, "run.receipt_written", receipt_checksum=receipt["checksum"],
                                verified=receipt["verified"], failed_checks=failed, generated_by=generated_by)
        state["verification"] = {"verified": receipt["verified"], "verified_at": receipt["created_at"],
                                 "failed_checks": failed, "level": receipt.get("verification_level")}
        if RunStatus(state["status"]) in RUN_TERMINAL:
            state["finalization"] = {"status": "COMPLETE", "receipt_checksum": receipt["checksum"],
                                     "at": receipt["created_at"], "generated_by": generated_by}
        self._save(state)
        return receipt

    def ensure_finalized(self, run_id: str) -> dict[str, Any] | None:
        """Return the terminal receipt, regenerating it if finalization was interrupted or it is damaged.

        Returns None for runs that are not terminal yet.
        """
        with self.mutate(run_id) as (state, spec):
            if RunStatus(state["status"]) not in RUN_TERMINAL:
                return None
            finalization = state.get("finalization") or {}
            problem = None
            try:
                receipt = self.store.read_receipt(run_id)
            except GraphEngineeringError as exc:
                receipt, problem = None, exc.message
            if receipt is not None and finalization.get("status") == "COMPLETE" \
                    and receipt.get("checksum") == finalization.get("receipt_checksum"):
                return receipt
            if problem is None:
                problem = ("finalization interrupted" if finalization.get("status") != "COMPLETE"
                           else "receipt missing" if receipt is None else "receipt does not match the finalization")
            self.store.append_trace(run_id, "run.receipt_recovery", reason=problem)
            self._record_recovery(state, "receipt_recovered", "repaired", reason=problem)
            state["finalization"] = {"status": "PENDING", "since": utc_now()}
            return self._finalize(state, spec, generated_by="recovery")

    def verify(self, run_id: str) -> dict[str, Any]:
        with self.mutate(run_id) as (state, spec):
            receipt = self._finalize(state, spec, generated_by="verify")
            self.store.append_trace(run_id, "run.verified", verified=receipt["verified"],
                                    failed_checks=[c["check"] for c in receipt["checks"] if not c["passed"]])
            return receipt


def _autopilot_summary(autopilot: Any) -> Any:
    if not isinstance(autopilot, dict):
        return None
    return {k: autopilot.get(k) for k in ("mode", "phase", "budget", "usage", "started_at", "continuations",
                                         "outcome", "history") if k in autopilot}


# ---------------------------------------------------------------------- views
def plan_view(spec: GraphSpec, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Execution plan: order, parallel layers, gates, ownership, rollback boundaries, policy."""
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
    decisions = ((state or {}).get("policy") or {}).get("decisions") or {}
    view = {
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
                "evidence": node.get("evidence") or [],
                "risk": node.get("risk"),
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
        "acceptance": spec.data.get("acceptance") or [],
    }
    if decisions:
        view["policy"] = {node_id: {k: d.get(k) for k in ("risk", "decision", "reasons")}
                          for node_id, d in decisions.items()}
    return view


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
            "evidence": node.get("evidence") or [],
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
    decisions = ((state.get("policy") or {}).get("decisions") or {})
    for node_id in state["order"]:
        node_gates = list(spec.node(node_id)["gates"])
        if (decisions.get(node_id) or {}).get("decision") == "REQUIRE_APPROVAL" \
                and not (decisions.get(node_id) or {}).get("gated_by"):
            node_gates.insert(0, {"id": POLICY_GATE, "type": "approval"})
        for gate in node_gates:
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
        "terminal_outcome": state.get("terminal_outcome"),
        "current_nodes": current,
        "completed_nodes": by_status[NodeStatus.SUCCEEDED.value],
        "repairing_nodes": by_status[NodeStatus.REPAIRING.value],
        "blocked_nodes": blocked,
        "failed_nodes": failed,
        "skipped_nodes": by_status[NodeStatus.SKIPPED.value],
        "nodes": {nid: {"status": nodes[nid]["status"], "attempts": nodes[nid]["attempts"],
                        "wait": nodes[nid]["wait"]} for nid in state["order"]},
        "holds": state["holds"],
        "gates": gates,
        "failure": state["failure"],
        "verification": state.get("verification"),
        "finalization": (state.get("finalization") or {}).get("status"),
        "revision": state.get("revision"),
        "spec_revisions": len(state.get("spec_revisions") or []),
        "updated_at": state["updated_at"],
    }


def pending_work(state: Mapping[str, Any], spec: GraphSpec) -> list[dict[str, Any]]:
    """Agent-facing work orders for nodes awaiting submission."""
    orders = []
    decisions = ((state.get("policy") or {}).get("decisions") or {})
    for node_id in state["order"]:
        ns = state["nodes"][node_id]
        if ns["status"] == NodeStatus.RUNNING.value and ns["wait"] == "submission":
            node = spec.node(node_id)
            order = {
                "run_id": state["run_id"],
                "node": node_id,
                "name": node["name"],
                "purpose": node["purpose"],
                "instructions": node["instructions"],
                "requires": node["requires"],
                "inputs": ns["inputs"],
                "outputs": node["outputs"],
                "success_criteria": node["success_criteria"],
                "evidence": node.get("evidence") or [],
                "attempt": ns["attempts"],
                "max_attempts": node["retry"]["max_attempts"],
                "idempotent": node["idempotent"],
                "side_effects": node["side_effects"],
            }
            decision = decisions.get(node_id)
            if decision:
                order["risk"] = decision.get("risk")
            if ns.get("repair") and ns["repair"].get("diagnosis"):
                order["diagnosis"] = ns["repair"]["diagnosis"]
            orders.append(order)
    return orders


def attempt_started_at(node_state: Mapping[str, Any]) -> float | None:
    return parse_ts(node_state.get("started_at"))
