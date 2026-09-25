"""Canonical run outcomes, hold states and the explicit state machines.

Terminal outcomes end a run and are written to the completion receipt. Hold
states pause a run without ending it. Only ``TerminalOutcome.SUCCEEDED``
counts as successful completion.

Every status change of a run or a node goes through :func:`check_run_transition`
or :func:`check_node_transition`. A transition that is not in the tables below
is a runtime bug and fails closed with ``INVALID_TRANSITION`` instead of
silently corrupting a run.
"""
from __future__ import annotations

from enum import Enum

from .errors import ErrorCode, GraphEngineeringError


class TerminalOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    HARD_STOPPED = "HARD_STOPPED"
    DEADLOCKED = "DEADLOCKED"
    POLICY_DENIED = "POLICY_DENIED"


class HoldState(str, Enum):
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    # a deferred (agent) node is waiting for its outputs to be submitted
    WAITING_FOR_SUBMISSION = "WAITING_FOR_SUBMISSION"
    # produced outputs are waiting for a human review gate
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    # a failed attempt waits for the autopilot to diagnose, repair or replan it
    WAITING_FOR_REPAIR = "WAITING_FOR_REPAIR"
    # automatic continuation or replay would be unsafe or ambiguous
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    # the last attempt failed; the autopilot owns the next step (repair, replan or give up)
    REPAIRING = "REPAIRING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class RunStatus(str, Enum):
    # created, plan not yet approved (also: a revised plan awaiting approval)
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    # paused on one or more hold states
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_OUTCOMES = frozenset(TerminalOutcome)
HOLD_STATES = frozenset(HoldState)
NODE_TERMINAL = frozenset({NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.SKIPPED})
RUN_TERMINAL = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})

_R = RunStatus
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    _R.DRAFT: frozenset({_R.APPROVED, _R.CANCELLED}),
    _R.APPROVED: frozenset({_R.RUNNING, _R.CANCELLED, _R.DRAFT}),
    _R.RUNNING: frozenset({_R.RUNNING, _R.WAITING, _R.SUCCEEDED, _R.FAILED, _R.CANCELLED, _R.DRAFT}),
    _R.WAITING: frozenset({_R.RUNNING, _R.WAITING, _R.SUCCEEDED, _R.FAILED, _R.CANCELLED, _R.DRAFT}),
    _R.SUCCEEDED: frozenset(),
    _R.FAILED: frozenset(),
    _R.CANCELLED: frozenset(),
}

_N = NodeStatus
NODE_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    _N.PENDING: frozenset({_N.READY, _N.BLOCKED, _N.SKIPPED}),
    _N.READY: frozenset({_N.RUNNING, _N.FAILED, _N.BLOCKED, _N.SKIPPED}),
    _N.RUNNING: frozenset({_N.SUCCEEDED, _N.FAILED, _N.READY, _N.SKIPPED, _N.REPAIRING}),
    _N.REPAIRING: frozenset({_N.READY, _N.FAILED, _N.SKIPPED}),
    _N.SUCCEEDED: frozenset(),
    _N.FAILED: frozenset(),
    _N.BLOCKED: frozenset(),
    _N.SKIPPED: frozenset(),
}
# A plan revision (replan) may reset any node that is not SUCCEEDED-and-unchanged back to PENDING.
REVISION_RESETTABLE = frozenset(NodeStatus) - {NodeStatus.RUNNING}


def check_run_transition(current: RunStatus | str, new: RunStatus | str) -> RunStatus:
    cur, nxt = RunStatus(current), RunStatus(new)
    if nxt not in RUN_TRANSITIONS[cur]:
        raise GraphEngineeringError(ErrorCode.INVALID_TRANSITION, "run transition %s -> %s is not allowed" % (
            cur.value, nxt.value), {"from": cur.value, "to": nxt.value})
    return nxt


def check_node_transition(current: NodeStatus | str, new: NodeStatus | str, *, revision: bool = False) -> NodeStatus:
    cur, nxt = NodeStatus(current), NodeStatus(new)
    if cur is nxt:
        return nxt
    if revision and nxt is NodeStatus.PENDING and cur in REVISION_RESETTABLE:
        return nxt
    if nxt not in NODE_TRANSITIONS[cur]:
        raise GraphEngineeringError(ErrorCode.INVALID_TRANSITION, "node transition %s -> %s is not allowed" % (
            cur.value, nxt.value), {"from": cur.value, "to": nxt.value})
    return nxt


def is_success(state: TerminalOutcome | HoldState) -> bool:
    return state is TerminalOutcome.SUCCEEDED
