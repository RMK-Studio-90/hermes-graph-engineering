"""Canonical run outcomes and hold states.

Terminal outcomes end a run and are written to the completion receipt. Hold
states pause a run without ending it. Only ``TerminalOutcome.SUCCEEDED``
counts as successful completion.
"""
from __future__ import annotations

from enum import Enum


class TerminalOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    HARD_STOPPED = "HARD_STOPPED"
    DEADLOCKED = "DEADLOCKED"


class HoldState(str, Enum):
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    # a deferred (agent) node is waiting for its outputs to be submitted
    WAITING_FOR_SUBMISSION = "WAITING_FOR_SUBMISSION"
    # produced outputs are waiting for a human review gate
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    # automatic continuation or replay would be unsafe or ambiguous
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class RunStatus(str, Enum):
    # created, plan not yet approved
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


def is_success(state: TerminalOutcome | HoldState) -> bool:
    return state is TerminalOutcome.SUCCEEDED
