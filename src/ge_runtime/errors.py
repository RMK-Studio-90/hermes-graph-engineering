"""Stable, machine-readable error codes.

Every failure the runtime reports to a host carries one of these codes, a
human-readable message and optional structured details. Hosts render them;
they never parse messages.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    GRAPH_INVALID = "GRAPH_INVALID"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    NODE_FAILED = "NODE_FAILED"
    GATE_FAILED = "GATE_FAILED"
    STATE_CORRUPT = "STATE_CORRUPT"
    PLUGIN_CONFIGURATION_INVALID = "PLUGIN_CONFIGURATION_INVALID"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_LOCKED = "RUN_LOCKED"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TRACE_WRITE_FAILED = "TRACE_WRITE_FAILED"
    STATE_WRITE_FAILED = "STATE_WRITE_FAILED"
    # host-side autonomous execution of agent nodes; generic, never provider specific
    HOST_EXECUTION_UNAVAILABLE = "HOST_EXECUTION_UNAVAILABLE"
    HOST_EXECUTION_FAILED = "HOST_EXECUTION_FAILED"
    HOST_RESULT_INVALID = "HOST_RESULT_INVALID"
    WORKER_CONTEXT_RESTRICTED = "WORKER_CONTEXT_RESTRICTED"
    DISPATCH_IN_PROGRESS = "DISPATCH_IN_PROGRESS"
    DISPATCH_INTERRUPTED = "DISPATCH_INTERRUPTED"
    # durable kernel: trace/state integrity and receipts
    STATE_DIVERGED = "STATE_DIVERGED"
    TRACE_CORRUPT = "TRACE_CORRUPT"
    RECEIPT_CORRUPT = "RECEIPT_CORRUPT"
    # policy, evidence and autopilot
    POLICY_DENIED = "POLICY_DENIED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    EVIDENCE_FAILED = "EVIDENCE_FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class GraphEngineeringError(Exception):
    """A fail-closed runtime error with a stable code."""

    def __init__(self, code: ErrorCode, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code.value, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return "%s: %s" % (self.code.value, self.message)
