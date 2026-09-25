"""Autopilot: one entry point that drives a task to a verified terminal result.

::

    ANALYZE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> DIAGNOSE -> REPAIR | REPLAN -> RETEST -> ... -> TERMINAL

``Autopilot.start`` turns a task (or a spec) into a run: deterministic analysis and planning
(``ge_runtime.planner``), policy classification and a digest-bound policy decision on the plan
(``ge_runtime.policy``). ``Autopilot.drive`` then advances the run as far as it can within one
call: builtin nodes execute, agent nodes are dispatched to fresh worker contexts through the
durable dispatcher (``ge_runtime.dispatch``), deterministic evidence is checked by the runtime
(``ge_runtime.evidence``), failed attempts are diagnosed and repaired with the diagnosis as an
explicit artifact, structural failures revise the subgraph and are re-tested, and a terminal
run is finalized with a receipt and a final report.

Everything the autopilot knows lives in the run (``state["autopilot"]`` and the journal), so
any later call, in any process, continues where the previous one stopped: across agent turns,
call-time budgets and crashes. No operator action is needed unless the policy requires one
(an approval gate for risky work, a denied plan, an interrupted non-idempotent dispatch); the
report then names exactly that action.

Hard budgets (persisted, enforced across calls): total node attempts, repairs per node and per
run, replans, wall-clock runtime since start, host-reported cost and the number of drive calls.
An exhausted budget ends the run with the terminal outcome ``BUDGET_EXHAUSTED``.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from .dispatch import AutonomousDispatcher, AgentWorker, clip
from .engine import GraphEngine, pending_work
from .errors import ErrorCode, GraphEngineeringError
from .planner import plan_task, replan
from .policy import PolicyConfig, apply_policy
from .states import RUN_TERMINAL, HoldState, NodeStatus, RunStatus, TerminalOutcome
from .store import utc_now

AUTOPILOT_ACTOR = "autopilot"
MODE = "autopilot/v1"

# outcomes of one drive call
CONTINUE = "CONTINUE"            # progress possible; call drive again (automatically, by the host)
NEEDS_OPERATOR = "NEEDS_OPERATOR"  # a human decision is required (named in next_action)
NEEDS_AGENT_TURN = "NEEDS_AGENT_TURN"  # agent work waits for a host agent turn (no worker here)
TERMINAL = "TERMINAL"
# node history entries that record something other than a finished attempt
_NOT_ATTEMPTS = frozenset({"REVISED", "INTERRUPTED", "DISPATCH_INTERRUPTED", "STALE_DISPATCH_RECOVERED"})


@dataclass(frozen=True)
class AutopilotBudget:
    max_attempts_total: int = 30
    max_repairs_per_node: int = 2
    max_repairs: int = 6
    max_replans: int = 2
    max_runtime_seconds: int = 4 * 3600
    max_cost: float | None = None
    max_drive_calls: int = 200
    backoff_seconds: tuple[float, ...] = (0.0, 2.0, 10.0)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["backoff_seconds"] = list(self.backoff_seconds)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AutopilotBudget":
        if not data:
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        if "backoff_seconds" in known:
            known["backoff_seconds"] = tuple(known["backoff_seconds"])
        return cls(**known)


def classify_failure(node_state: Mapping[str, Any], history_codes: list[str]) -> str:
    """TRANSIENT | CONTRACT | EVIDENCE | STRUCTURAL | POLICY for the latest failed attempt."""
    error = node_state.get("last_error") or {}
    code, message = error.get("code") or "", str(error.get("message") or "")
    outcome = error.get("outcome") or ""
    if code in (ErrorCode.POLICY_VIOLATION.value, ErrorCode.POLICY_DENIED.value):
        return "POLICY"
    if "input contract violated" in message or code == ErrorCode.EXECUTOR_UNAVAILABLE.value:
        return "STRUCTURAL"
    signature = [h for h in history_codes if h == message[:200]]
    if len(signature) >= 2:
        return "STRUCTURAL"  # the same failure twice: repeating the attempt will not help
    if code == ErrorCode.EVIDENCE_FAILED.value:
        return "EVIDENCE"
    if outcome == "TIMEOUT" or "host execution failed" in message or "timed out" in message:
        return "TRANSIENT"
    return "CONTRACT"


class Autopilot:
    def __init__(self, engine: GraphEngine, worker: AgentWorker | None = None, *,
                 policy: PolicyConfig | None = None, budget: AutopilotBudget | None = None,
                 state_key: str = "autopilot", node_timeout_seconds: int = 3600,
                 toolsets_for: Callable[[str | None], Any] | None = None, lease_ttl_seconds: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.engine = engine
        self.worker = worker
        self.policy = policy or PolicyConfig()
        self.budget = budget or AutopilotBudget()
        self.state_key = state_key
        self.node_timeout_seconds = node_timeout_seconds
        self.toolsets_for = toolsets_for
        self.lease_ttl_seconds = lease_ttl_seconds
        self.sleep = sleep
        self.clock = clock

    # ------------------------------------------------------------------ ANALYZE / PLAN / POLICY
    def start(self, *, task: str | None = None, spec: Any = None, created_by: str = AUTOPILOT_ACTOR,
              workspace: str | None = None, origin: Mapping[str, Any] | None = None) -> str:
        if (task is None) == (spec is None):
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "autopilot needs exactly one of task or spec")
        analysis = None
        if task is not None:
            planned = plan_task(task)
            spec, analysis = planned["spec"], planned["analysis"]
        autopilot = {
            "mode": MODE, "repair": True, "phase": "PLAN", "started_at": self.clock(), "started": utc_now(),
            "budget": self.budget.to_dict(), "policy": self.policy.to_dict(),
            "usage": {"drive_calls": 0, "repairs": 0, "replans": 0, "cost": 0.0},
            "history": [], "origin": dict(origin or {}), "task": (task or "")[:2000],
        }
        state = self.engine.create(spec, created_by=created_by, autopilot=autopilot, workspace=workspace)
        run_id = state["run_id"]
        self._phase(run_id, "ANALYZE", analysis=analysis and {"steps": len(analysis["steps"]),
                                                               "evidence": len(analysis["evidence"])})
        self._phase(run_id, "POLICY")
        apply_policy(self.engine, run_id, self.policy)
        return run_id

    def adopt(self, run_id: str) -> None:
        """Put an existing run under autopilot control (budgets, repair, policy)."""
        state, _spec = self.engine.load(run_id)
        if isinstance(state.get("autopilot"), dict) and state["autopilot"].get("mode"):
            return
        self.engine.update_autopilot(run_id, {
            "mode": MODE, "repair": True, "phase": "POLICY", "started_at": self.clock(), "started": utc_now(),
            "budget": self.budget.to_dict(), "policy": self.policy.to_dict(),
            "usage": {"drive_calls": 0, "repairs": 0, "replans": 0, "cost": 0.0}, "history": [], "origin": {}},
            event="autopilot.adopted")

    # ------------------------------------------------------------------ drive
    def drive(self, run_id: str, *, call_budget_seconds: float | None = 300.0) -> dict[str, Any]:
        """Advance the run as far as possible in this call; returns the step report."""
        deadline = time.monotonic() + call_budget_seconds if call_budget_seconds is not None else None
        report: dict[str, Any] = {"run_id": run_id, "steps": [], "dispatched": []}
        state, _spec = self.engine.load(run_id)
        if RunStatus(state["status"]) not in RUN_TERMINAL:
            usage = dict((state.get("autopilot") or {}).get("usage") or {})
            usage["drive_calls"] = int(usage.get("drive_calls") or 0) + 1
            self.engine.update_autopilot(run_id, {"usage": usage})
        for _ in range(10_000):
            state, spec = self.engine.load(run_id)
            status = RunStatus(state["status"])
            if status in RUN_TERMINAL:
                return self._terminal(run_id, report)
            exhausted = self._budget_exhausted(state)
            if exhausted:
                self.engine.terminate(run_id, outcome=TerminalOutcome.BUDGET_EXHAUSTED,
                                      code=ErrorCode.BUDGET_EXHAUSTED, reason=exhausted, decided_by=AUTOPILOT_ACTOR)
                report["steps"].append({"phase": "TERMINAL", "detail": exhausted})
                return self._terminal(run_id, report)
            if deadline is not None and time.monotonic() >= deadline:
                return self._yield(run_id, report, CONTINUE, "call budget reached; call again to continue",
                                   {"action": "auto", "run_id": run_id})
            if status is RunStatus.DRAFT:
                decision = (state.get("policy") or {}).get("spec_digest") == state["spec_digest"]
                if not decision:
                    self._phase(run_id, "POLICY")
                    apply_policy(self.engine, run_id, self.policy)
                    report["steps"].append({"phase": "POLICY"})
                    continue
                return self._yield(run_id, report, NEEDS_OPERATOR, "the plan needs an operator approval",
                                   {"command": "/ge-approve %s plan" % run_id})
            if status is RunStatus.APPROVED:
                self._phase(run_id, "EXECUTE")
                self.engine.execute(run_id)
                report["steps"].append({"phase": "EXECUTE"})
                continue
            if status is RunStatus.RUNNING and not state["holds"]:
                self.engine.resume(run_id)
                continue
            holds = state["holds"]
            kinds = {h["kind"] for h in holds}
            if holds and all(h.get("node") is None and str(h.get("detail", "")).startswith("step budget")
                             for h in holds):
                self.engine.resume(run_id)  # the engine's per-call step bound, not a decision
                continue
            if HoldState.WAITING_FOR_REPAIR.value in kinds:
                hold = next(h for h in holds if h["kind"] == HoldState.WAITING_FOR_REPAIR.value)
                outcome = self._repair(run_id, state, spec, hold["node"], report, deadline)
                if outcome is not None:
                    return outcome
                continue
            if HoldState.WAITING_FOR_SUBMISSION.value in kinds:
                outcome = self._dispatch(run_id, report, deadline)
                if outcome is not None:
                    return outcome
                continue
            return self._operator_hold(run_id, state, report)
        raise GraphEngineeringError(ErrorCode.INVALID_TRANSITION, "autopilot made no progress on %s" % run_id)

    def run_to_completion(self, run_id: str, *, max_calls: int = 1000, call_budget_seconds: float = 300.0,
                          on_step: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Headless loop: drive until terminal or until a human or an agent turn is required."""
        report: dict[str, Any] = {}
        for _ in range(max_calls):
            report = self.drive(run_id, call_budget_seconds=call_budget_seconds)
            if on_step:
                on_step(report)
            if report["status"] != CONTINUE:
                return report
        return report

    # ------------------------------------------------------------------ phases
    def _phase(self, run_id: str, phase: str, **fields: Any) -> None:
        state, _spec = self.engine.load(run_id)
        autopilot = state.get("autopilot") or {}
        if autopilot.get("phase") == phase and not fields:
            return
        entry = {"phase": phase, "at": utc_now()}
        entry.update({k: v for k, v in fields.items() if v is not None})
        history = (list(autopilot.get("history") or []) + [entry])[-200:]
        self.engine.update_autopilot(run_id, {"phase": phase, "history": history}, event="autopilot.phase",
                                     phase=phase, **{k: v for k, v in fields.items() if v is not None})

    def _budget_exhausted(self, state: Mapping[str, Any]) -> str | None:
        autopilot = state.get("autopilot") or {}
        budget = AutopilotBudget.from_dict(autopilot.get("budget"))
        usage = autopilot.get("usage") or {}
        attempts = sum(len([h for h in n.get("history") or [] if h.get("outcome") not in _NOT_ATTEMPTS])
                       + (1 if n.get("status") == NodeStatus.RUNNING.value else 0) for n in state["nodes"].values())
        if attempts > budget.max_attempts_total:
            return "attempt budget exhausted (%d > %d)" % (attempts, budget.max_attempts_total)
        started = autopilot.get("started_at")
        if isinstance(started, (int, float)) and self.clock() - started > budget.max_runtime_seconds:
            return "runtime budget exhausted (%ds)" % budget.max_runtime_seconds
        if budget.max_cost is not None and float(usage.get("cost") or 0.0) > budget.max_cost:
            return "cost budget exhausted (%.4f > %.4f)" % (float(usage.get("cost") or 0.0), budget.max_cost)
        if int(usage.get("drive_calls") or 0) > budget.max_drive_calls:
            return "drive-call budget exhausted (%d)" % budget.max_drive_calls
        return None

    def _dispatch(self, run_id: str, report: dict[str, Any], deadline: float | None) -> dict[str, Any] | None:
        if self.worker is None or not _supported(self.worker):
            return self._yield(run_id, report, NEEDS_AGENT_TURN,
                               "agent work is waiting and no agent worker is available in this call",
                               {"action": "auto", "run_id": run_id, "where": "an agent turn (ge_graph tool)"})
        self._phase(run_id, "EXECUTE")
        dispatcher = AutonomousDispatcher(self.engine, self.worker, state_key=self.state_key,
                                          node_timeout_seconds=self.node_timeout_seconds,
                                          toolsets_for=self.toolsets_for, deadline=deadline,
                                          lease_ttl_seconds=self.lease_ttl_seconds)
        result = dispatcher.drive(run_id)
        report["dispatched"].extend(result.get("dispatched") or [])
        report["steps"].append({"phase": "EXECUTE", "dispatcher": result.get("stopped"),
                                "nodes": [d["node"] for d in result.get("dispatched") or []]})
        stopped = result.get("stopped")
        if stopped in ("run_finished", "no_pending_work", "budget_exhausted", "state_changed"):
            return None
        if stopped == "time_budget_exhausted":
            return self._yield(run_id, report, CONTINUE, "call budget reached; call again to continue",
                               {"action": "auto", "run_id": run_id})
        if stopped == "host_unavailable":
            return self._yield(run_id, report, NEEDS_AGENT_TURN, result.get("message", "host unavailable"),
                               {"action": "auto", "run_id": run_id, "where": "an agent turn (ge_graph tool)"})
        if stopped in ("dispatch_in_progress", "lease_lost"):
            return self._yield(run_id, report, CONTINUE, result.get("message", "another dispatcher is active"),
                               {"action": "auto", "run_id": run_id, "retry_after_seconds": 30})
        if stopped == "interrupted_dispatch":
            return None  # persisted as NEEDS_ATTENTION; the next iteration reports the operator hold
        return self._yield(run_id, report, NEEDS_OPERATOR, result.get("message", str(stopped)),
                           {"action": "inspect", "run_id": run_id})

    # ------------------------------------------------------------------ DIAGNOSE / REPAIR / REPLAN
    def _diagnosis(self, run_id: str, state: Mapping[str, Any], node_id: str, category: str) -> dict[str, Any]:
        ns = state["nodes"][node_id]
        error = ns.get("last_error") or {}
        failed_checks = []
        for entry in ns.get("evidence") or []:
            if entry.get("passed"):
                continue
            item = {"check": entry.get("check"), "detail": entry.get("detail")}
            try:
                payload = self.engine.store.read_artifact(run_id, entry["artifact"])
                full = next((r for r in payload.get("results", []) if r.get("check") == entry.get("check")), {})
                if isinstance(full.get("spec"), dict):
                    item["spec"] = {k: v for k, v in full["spec"].items() if k in ("type", "path", "argv", "text")}
                for key in ("exit_code", "stdout_tail", "stderr_tail", "changed", "expected", "sha256"):
                    if full.get(key) not in (None, "", []):
                        value = full[key]
                        item[key] = value[-1500:] if isinstance(value, str) else value
            except GraphEngineeringError:
                pass
            failed_checks.append(item)
        return {"category": category, "node": node_id, "attempt": ns.get("attempts"),
                "error": {"code": error.get("code"), "message": clip(error.get("message") or "", 1500)},
                "failed_checks": failed_checks[:10],
                "criteria": [c for c in ns.get("criteria") or [] if not c.get("passed")][:10],
                "previous_repairs": int((ns.get("repair") or {}).get("count") or 0)}

    def _repair(self, run_id: str, state: Mapping[str, Any], spec: Any, node_id: str, report: dict[str, Any],
                deadline: float | None) -> dict[str, Any] | None:
        ns = state["nodes"][node_id]
        autopilot = state.get("autopilot") or {}
        budget = AutopilotBudget.from_dict(autopilot.get("budget"))
        usage = dict(autopilot.get("usage") or {})
        messages = [str(h.get("detail") or "")[:200] for h in ns.get("history") or []
                    if h.get("outcome") not in ("SUCCESS", "INTERRUPTED", "REVISED")]
        category = classify_failure(ns, messages[:-1])
        diagnosis = self._diagnosis(run_id, state, node_id, category)
        self._phase(run_id, "DIAGNOSE", node=node_id, category=category)
        report["steps"].append({"phase": "DIAGNOSE", "node": node_id, "category": category})
        repairs = int(usage.get("repairs") or 0)
        node_repairs = int((ns.get("repair") or {}).get("count") or 0)
        node = spec.node(node_id)
        is_checkpoint = node["executor"] == "builtin"
        if category == "POLICY":
            self.engine.give_up_node(run_id, node_id, reason="policy failure is not repairable: %s" % (
                (ns.get("last_error") or {}).get("message")), code=ErrorCode.POLICY_VIOLATION,
                decided_by=AUTOPILOT_ACTOR)
            return None
        wants_replan = category == "STRUCTURAL" or is_checkpoint or node_repairs >= budget.max_repairs_per_node
        if wants_replan:
            replans = int(usage.get("replans") or 0)
            if replans >= budget.max_replans:
                self.engine.give_up_node(run_id, node_id, reason="replan budget exhausted (%d); last failure: %s" % (
                    budget.max_replans, diagnosis["error"]["message"]), code=ErrorCode.BUDGET_EXHAUSTED,
                    decided_by=AUTOPILOT_ACTOR)
                self._mark_budget(run_id, "replans")
                return None
            try:
                new_spec, description = replan(spec.data, node_id, diagnosis, replans + 1)
            except GraphEngineeringError as exc:
                self.engine.give_up_node(run_id, node_id, reason="cannot replan: %s" % exc.message,
                                         decided_by=AUTOPILOT_ACTOR)
                return None
            self._phase(run_id, "REPLAN", node=node_id, round=replans + 1, change=description)
            usage["replans"] = replans + 1
            self.engine.update_autopilot(run_id, {"usage": usage})
            self.engine.revise(run_id, new_spec, reason="%s: %s" % (category, description), decided_by=AUTOPILOT_ACTOR)
            apply_policy(self.engine, run_id, self.policy)  # a revised plan is never approved by an old decision
            self._phase(run_id, "RETEST", node=node_id)
            report["steps"].append({"phase": "REPLAN", "node": node_id, "change": description})
            return None
        if repairs >= budget.max_repairs:
            self.engine.give_up_node(run_id, node_id, reason="repair budget exhausted (%d); last failure: %s" % (
                budget.max_repairs, diagnosis["error"]["message"]), code=ErrorCode.BUDGET_EXHAUSTED,
                decided_by=AUTOPILOT_ACTOR)
            return None
        backoff = budget.backoff_seconds[min(node_repairs, len(budget.backoff_seconds) - 1)] \
            if budget.backoff_seconds else 0.0
        if category == "TRANSIENT" and backoff > 0:
            if deadline is not None and time.monotonic() + backoff > deadline:
                return self._yield(run_id, report, CONTINUE, "backing off %.0fs before retrying %s" % (backoff, node_id),
                                   {"action": "auto", "run_id": run_id, "retry_after_seconds": backoff})
            self.sleep(backoff)
        usage["repairs"] = repairs + 1
        self.engine.update_autopilot(run_id, {"usage": usage})
        self._phase(run_id, "REPAIR", node=node_id, category=category, round=node_repairs + 1)
        self.engine.repair_node(run_id, node_id, diagnosis=diagnosis, decided_by=AUTOPILOT_ACTOR)
        self._phase(run_id, "RETEST", node=node_id)
        report["steps"].append({"phase": "REPAIR", "node": node_id, "category": category, "backoff": backoff})
        return None

    def _mark_budget(self, run_id: str, which: str) -> None:
        self.engine.update_autopilot(run_id, {"exhausted": which})

    # ------------------------------------------------------------------ results
    def _operator_hold(self, run_id: str, state: Mapping[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        actions = []
        for hold in state["holds"]:
            kind, node, gate = hold["kind"], hold.get("node"), hold.get("gate")
            if kind in (HoldState.WAITING_FOR_APPROVAL.value, HoldState.WAITING_FOR_REVIEW.value) and node:
                actions.append({"hold": kind, "node": node, "command": "/ge-approve %s %s:%s" % (run_id, node, gate),
                                "deny": "/ge-approve %s %s:%s --deny" % (run_id, node, gate)})
            elif kind == HoldState.NEEDS_ATTENTION.value and node:
                actions.append({"hold": kind, "node": node, "code": hold.get("code"),
                                "command": "/ge-retry %s %s" % (run_id, node),
                                "alternatives": ["/ge-submit %s %s <json>" % (run_id, node),
                                                 "/ge-cancel %s" % run_id]})
            else:
                actions.append({"hold": kind, "detail": hold.get("detail"), "command": "/ge-status %s" % run_id})
        return self._yield(run_id, report, NEEDS_OPERATOR, "operator decision required", {"operator": actions})

    def _yield(self, run_id: str, report: dict[str, Any], status: str, message: str,
               next_action: Mapping[str, Any]) -> dict[str, Any]:
        state, _spec = self.engine.load(run_id)
        report.update(status=status, message=message, next=dict(next_action), run_status=state["status"],
                      holds=state["holds"], usage=(state.get("autopilot") or {}).get("usage"))
        self.engine.update_autopilot(run_id, {"last": {"status": status, "message": message, "at": utc_now(),
                                                       "next": dict(next_action)}})
        return report

    def _terminal(self, run_id: str, report: dict[str, Any]) -> dict[str, Any]:
        receipt = self.engine.ensure_finalized(run_id)
        state, spec = self.engine.load(run_id)
        autopilot = state.get("autopilot") or {}
        final = autopilot.get("final_report")
        if not final or final.get("receipt_checksum") != (receipt or {}).get("checksum"):
            final = build_final_report(state, spec, receipt or {})
            ref = self.engine.store.write_artifact(run_id, "final-report", final)
            final = dict(final, artifact=ref)
            history = (list(autopilot.get("history") or []) + [{"phase": "TERMINAL", "at": utc_now(),
                                                                 "outcome": final["outcome"]}])[-200:]
            final["phases"] = [h.get("phase") for h in history]
            self.engine.update_autopilot(run_id, {"phase": "TERMINAL", "final_report": final, "history": history,
                                                  "outcome": final["outcome"],
                                                  "last": {"status": TERMINAL, "at": utc_now(), "next": None,
                                                           "message": "run finished: %s" % final["outcome"]}},
                                         event="autopilot.finished", outcome=final["outcome"],
                                         verified=final["verified"], report_digest=ref["digest"])
        report.update(status=TERMINAL, message="run finished: %s" % final["outcome"], run_status=state["status"],
                      final_report=final, next=None, holds=[])
        return report


def _supported(worker: Any) -> bool:
    try:
        return bool(worker.supports_agent_execution())
    except Exception:
        return False


def build_final_report(state: Mapping[str, Any], spec: Any, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The automatic report of a finished run: outcome, evidence, repairs, replans, budget use."""
    autopilot = state.get("autopilot") or {}
    nodes = []
    for node_id in state["order"]:
        ns = state["nodes"][node_id]
        nodes.append({
            "node": node_id, "status": ns["status"], "attempts": ns["attempts"],
            "repairs": int((ns.get("repair") or {}).get("count") or 0),
            "evidence": [{"check": e["check"], "passed": e["passed"], "detail": e["detail"]}
                         for e in ns.get("evidence") or []],
            "error": ns.get("last_error") if ns["status"] != NodeStatus.SUCCEEDED.value else None,
        })
    outcome = state.get("terminal_outcome") or state["status"]
    verified = bool(receipt.get("verified"))
    return {
        "run_id": state["run_id"], "graph_id": state["graph_id"], "title": state.get("title"),
        "run_status": state["status"], "outcome": outcome, "verified": verified,
        "verification_level": receipt.get("verification_level"),
        "failed_checks": [c["check"] for c in receipt.get("checks") or [] if not c["passed"]],
        "receipt_checksum": receipt.get("checksum"), "failure": state.get("failure"),
        "nodes": nodes,
        "replans": [{k: r.get(k) for k in ("revision", "reason", "kept", "reset", "dropped")}
                    for r in state.get("spec_revisions") or []],
        "phases": [h.get("phase") for h in autopilot.get("history") or []],
        "usage": autopilot.get("usage"), "budget": autopilot.get("budget"),
        "policy": {k: (state.get("policy") or {}).get(k) for k in ("plan_decision", "max_risk", "digest")},
        "recovery": [r.get("kind") for r in state.get("recovery") or []],
        "finished_at": state.get("finished_at"),
    }


def render_final_report(final: Mapping[str, Any]) -> str:
    lines = ["Graph Engineering result: %s - %s (%s)" % (
        final["outcome"], "VERIFIED" if final["verified"] else "NOT VERIFIED", final.get("verification_level") or "-"),
        "run: %s  (%s)" % (final["run_id"], final.get("title") or final["graph_id"])]
    for node in final["nodes"]:
        evidence = node["evidence"]
        passed = sum(1 for e in evidence if e["passed"])
        lines.append("  %s %s attempts=%d repairs=%d evidence=%d/%d%s" % (
            node["node"], node["status"], node["attempts"], node["repairs"], passed, len(evidence),
            (" - %s" % clip((node["error"] or {}).get("message") or "", 160)) if node.get("error") else ""))
    if final.get("replans"):
        lines.append("replans: %s" % "; ".join(str(r.get("reason")) for r in final["replans"]))
    if final.get("failed_checks"):
        lines.append("failed checks: %s" % ", ".join(final["failed_checks"]))
    if final.get("failure") and not final["verified"]:
        lines.append("failure: %s %s" % (final["failure"].get("code"), clip(final["failure"].get("message") or "", 300)))
    usage = final.get("usage") or {}
    lines.append("usage: %s drive call(s), %s repair(s), %s replan(s)" % (
        usage.get("drive_calls", 0), usage.get("repairs", 0), usage.get("replans", 0)))
    lines.append("receipt: %s" % final.get("receipt_checksum"))
    return "\n".join(lines)


def render_step(report: Mapping[str, Any]) -> str:
    if report.get("status") == TERMINAL:
        return render_final_report(report["final_report"])
    lines = ["Graph Engineering autopilot: %s - %s" % (report.get("status"), report.get("message"))]
    lines.append("run: %s (%s)" % (report["run_id"], report.get("run_status")))
    for step in report.get("steps") or []:
        lines.append("  %s" % ", ".join("%s=%s" % (k, v) for k, v in step.items()))
    nxt = report.get("next") or {}
    for action in nxt.get("operator") or []:
        lines.append("  needs: %s (%s)" % (action.get("command"), action.get("hold")))
    if nxt.get("action") == "auto":
        lines.append("  next: ge_graph action=auto run_id=%s" % report["run_id"])
    return "\n".join(lines)
