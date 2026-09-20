"""Recursion and concurrency protection for autonomous agent execution.

A worker that executes one Graph Engineering node must not start, run, resume
or submit graphs, and two callers must not drive the same run at once. Three
layers, from strongest to weakest assumption about the host:

* ``exclusive_dispatch``: one dispatch at a time per state directory (V1 executes
  agent nodes sequentially). A nested dispatch attempted from a worker thread is
  refused whatever the host does with thread context.
* ``enforce_not_dispatching``: while a run is being dispatched, the agent tool
  refuses ``run``/``resume``/``submit``/``verify`` on that run for every caller,
  again independent of thread context. Operator slash commands are unaffected.
* ``worker_scope``: marks the execution context as a node worker with a
  ``contextvars.ContextVar``. Hosts that copy the caller's context into their
  worker threads (Hermes does from 0.21.1) carry the marker into everything the
  worker does, and the tool then refuses every state-changing action, including
  creating new graphs. Where the host does not propagate context this layer is
  simply absent, and the first two still hold.

State directories are profile scoped, so profiles never contend or leak into
each other. Nothing here is persisted: recovery relies on Graph Engineering's
own state and trace (see ``dispatch``).
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
from dataclasses import dataclass
from typing import Iterator

from ge_runtime.errors import ErrorCode, GraphEngineeringError

# Actions that only read: allowed even inside a worker context.
READ_ONLY_ACTIONS = frozenset({"validate", "plan", "status", "work", "explain", "list"})


@dataclass(frozen=True)
class WorkerScope:
    run_id: str
    node_id: str


_WORKER: contextvars.ContextVar[WorkerScope | None] = contextvars.ContextVar("ge_hermes_worker_scope", default=None)
_LEASES: dict[str, str] = {}  # state directory -> run currently being dispatched
_LEASES_LOCK = threading.Lock()
# Tool actions that change or finalize a run; refused while that run is being dispatched.
RUN_MUTATING_ACTIONS = frozenset({"run", "resume", "submit", "verify"})


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


def enforce_not_dispatching(state_key: str, run_id: str, action: str) -> None:
    """Refuse run-mutating tool actions on a run that is being dispatched right now."""
    with _LEASES_LOCK:
        busy = _LEASES.get(state_key) == run_id
    if busy and action in RUN_MUTATING_ACTIONS:
        raise GraphEngineeringError(
            ErrorCode.DISPATCH_IN_PROGRESS,
            "run %s is being executed by the host right now; %r is not available until it stops" % (run_id, action),
            {"run_id": run_id})


@contextlib.contextmanager
def exclusive_dispatch(state_key: str, run_id: str) -> Iterator[bool]:
    """Yield True if this caller now holds the dispatch lease of the state directory, False if another does."""
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
