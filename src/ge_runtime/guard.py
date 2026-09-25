"""Recursion and concurrency protection for autonomous agent execution.

A worker that executes one Graph Engineering node must not start, run, resume
or submit graphs, and two callers must not drive the same run at once. Layers,
from strongest to weakest assumption about the host:

* the durable dispatch lease (``ge_runtime.dispatch.DispatchLease``): one owner
  per run across threads, processes and hosts sharing the state directory;
  persisted with owner, heartbeat and expiry.
* ``exclusive_dispatch``: one dispatch at a time per state directory inside a
  process. A nested dispatch attempted from a worker thread is refused whatever
  the host does with thread context.
* ``enforce_not_dispatching``: while a run is being dispatched (in this process
  or, via its live lease, in any other), the agent tool refuses
  ``run``/``resume``/``submit``/``verify``/``auto`` on that run for every caller.
  Operator slash commands are unaffected.
* ``worker_scope``: marks the execution context as a node worker with a
  ``contextvars.ContextVar``. Hosts that copy the caller's context into their
  worker threads carry the marker into everything the worker does, and the tool
  then refuses every state-changing action, including creating new graphs.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import ErrorCode, GraphEngineeringError

# Actions that only read: allowed even inside a worker context.
READ_ONLY_ACTIONS = frozenset({"validate", "plan", "status", "work", "explain", "list"})


@dataclass(frozen=True)
class WorkerScope:
    run_id: str
    node_id: str


_WORKER: contextvars.ContextVar[WorkerScope | None] = contextvars.ContextVar("ge_worker_scope", default=None)
_LEASES: dict[str, str] = {}  # state directory -> run currently being dispatched by this process
_LEASES_LOCK = threading.Lock()
# Tool actions that change or finalize a run; refused while that run is being dispatched.
RUN_MUTATING_ACTIONS = frozenset({"run", "resume", "submit", "verify", "auto"})


def current_worker() -> WorkerScope | None:
    return _WORKER.get()


@contextlib.contextmanager
def worker_scope(scope: WorkerScope) -> Iterator[None]:
    token = _WORKER.set(scope)
    try:
        yield
    finally:
        _WORKER.reset(token)


def enforce_worker_restrictions(action: str) -> None:
    """Refuse state-changing tool actions from inside a node worker."""
    scope = current_worker()
    if scope is not None and action not in READ_ONLY_ACTIONS:
        raise GraphEngineeringError(
            ErrorCode.WORKER_CONTEXT_RESTRICTED,
            "action %r is not available while executing a Graph Engineering node; return the node's outputs "
            "instead" % action,
            {"run_id": scope.run_id, "node": scope.node_id, "allowed_actions": sorted(READ_ONLY_ACTIONS)})


def enforce_not_dispatching(state_key: str, run_id: str, action: str, store: Any = None) -> None:
    """Refuse run-mutating tool actions on a run that is being dispatched right now (any process)."""
    if action not in RUN_MUTATING_ACTIONS:
        return
    with _LEASES_LOCK:
        busy = _LEASES.get(state_key) == run_id
    owner = None
    if not busy and store is not None:
        from .dispatch import lease_is_live

        lease = store.read_lease(run_id)
        busy = lease_is_live(lease)
        owner = (lease or {}).get("owner")
    if busy:
        details = {"run_id": run_id}
        if owner:
            details["lease_owner"] = owner
        raise GraphEngineeringError(
            ErrorCode.DISPATCH_IN_PROGRESS,
            "run %s is being executed by the host right now; %r is not available until it stops" % (run_id, action),
            details)


@contextlib.contextmanager
def exclusive_dispatch(state_key: str, run_id: str) -> Iterator[bool]:
    """Yield True if this caller now holds the in-process dispatch slot of the state directory."""
    with _LEASES_LOCK:
        acquired = state_key not in _LEASES
        if acquired:
            _LEASES[state_key] = run_id
    try:
        yield acquired
    finally:
        if acquired:
            with _LEASES_LOCK:
                _LEASES.pop(state_key, None)
