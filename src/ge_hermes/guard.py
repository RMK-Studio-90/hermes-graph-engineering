"""Compatibility re-export: the guards live in the host-independent core (``ge_runtime.guard``)."""
from ge_runtime.guard import *  # noqa: F401,F403
from ge_runtime.guard import (  # noqa: F401
    READ_ONLY_ACTIONS,
    RUN_MUTATING_ACTIONS,
    WorkerScope,
    current_worker,
    enforce_not_dispatching,
    enforce_worker_restrictions,
    exclusive_dispatch,
    worker_scope,
)
