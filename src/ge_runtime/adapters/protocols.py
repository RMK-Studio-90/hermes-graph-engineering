"""Extension-point protocols.

Each adapter declares a ``name``; adapters whose failure may affect run
correctness also declare ``required``. The runtime decides how an adapter
failure affects a run; adapters only report normalized results.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .types import (
    ApprovalBinding,
    ApprovalDecision,
    ExecutionRequest,
    ExecutorResult,
    ImpactResult,
    NotifyCategory,
    NotifyResult,
)


@runtime_checkable
class ExecutorAdapter(Protocol):
    name: str

    def execute(self, request: ExecutionRequest) -> ExecutorResult:
        """Run one attempt of one node and return a normalized result."""

    def cancel(self, execution_id: str) -> bool:
        """Request cancellation; return True if the request was accepted."""


@runtime_checkable
class ApprovalProvider(Protocol):
    name: str

    def request(self, binding: ApprovalBinding) -> str:
        """Open an approval request for ``binding`` and return its request id."""

    def poll(self, request_id: str) -> ApprovalDecision:
        """Return the current decision; failures return ERROR, never APPROVED."""


@runtime_checkable
class TraceSink(Protocol):
    name: str
    required: bool

    def emit(self, event: Mapping[str, Any]) -> None:
        """Persist one trace event or raise TraceWriteError."""

    def flush(self) -> None:
        """Flush buffered events or raise TraceWriteError."""


@runtime_checkable
class Notifier(Protocol):
    name: str

    def notify(self, category: NotifyCategory, message: str) -> NotifyResult:
        """Deliver a (redacted) message; report failure instead of raising."""


@runtime_checkable
class KnowledgeGraphAdapter(Protocol):
    name: str
    required: bool

    def register_spec(self, meta: Mapping[str, Any]) -> None:
        """Record an approved spec in the knowledge graph."""

    def register_run(self, receipt: Mapping[str, Any]) -> None:
        """Record a terminal run receipt in the knowledge graph."""

    def impact(self, node_ref: str) -> ImpactResult:
        """Return the downstream impact of ``node_ref`` if supported."""
