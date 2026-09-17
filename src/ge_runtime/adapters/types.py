"""Value types exchanged between the runtime and adapters.

Adapters normalize host-, provider- and framework-specific behaviour into these
types; the runtime never depends on adapter-specific exceptions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class ExecutorOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    NON_RETRYABLE_FAILURE = "NON_RETRYABLE_FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    GOVERNANCE_BLOCKED = "GOVERNANCE_BLOCKED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"


class CostSource(str, Enum):
    # reported by the adapter itself; not a truth anchor
    ADAPTER_ATTESTED = "ADAPTER_ATTESTED"
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Usage:
    cost: float | None = None
    cost_source: CostSource = CostSource.UNKNOWN


@dataclass(frozen=True)
class ExecutionRequest:
    """Everything an executor may see for one attempt of one node.

    ``harness`` is the node-scoped handle to tools, permitted state and secret
    resolution. The executor never receives the scheduler or the graph.
    """

    run_id: str
    node_id: str
    attempt_id: str
    execution_id: str
    context_id: str
    node_type: str
    identity: str
    contract: Mapping[str, Any]
    inputs: Mapping[str, Any]
    harness: Any = None


@dataclass(frozen=True)
class ExecutorResult:
    outcome: ExecutorOutcome
    outputs: Mapping[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    # iterations of an executor-internal loop; bounded and reported, never hidden retries
    iterations: int = 0
    detail: str = ""


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalBinding:
    """The exact decision a human approves; an approval never transfers to another binding."""

    graph_id: str
    spec_version: str
    run_id: str
    node_or_transition: str
    action_digest: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def digest(self) -> str:
        return _digest(self.to_dict())


class ApprovalStatus(str, Enum):
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    PENDING = "PENDING"
    # any provider failure; never implies approval
    ERROR = "ERROR"


@dataclass(frozen=True)
class ApprovalDecision:
    status: ApprovalStatus
    request_id: str
    binding: ApprovalBinding | None = None
    decided_by: str | None = None
    detail: str = ""


class NotifyCategory(str, Enum):
    REPORT_IMMEDIATELY = "REPORT_IMMEDIATELY"
    LOG_ONLY = "LOG_ONLY"
    NO_USER_QUERY = "NO_USER_QUERY"


@dataclass(frozen=True)
class NotifyResult:
    delivered: bool
    detail: str = ""


@dataclass(frozen=True)
class ImpactResult:
    supported: bool
    affected: tuple[str, ...] = ()
    detail: str = ""


class TraceWriteError(Exception):
    """A trace event could not be persisted."""
