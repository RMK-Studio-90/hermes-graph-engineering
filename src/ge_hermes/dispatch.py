"""Compatibility re-export: the dispatcher lives in the host-independent core (``ge_runtime.dispatch``)."""
from ge_runtime.dispatch import *  # noqa: F401,F403
from ge_runtime.dispatch import (  # noqa: F401
    AgentWorker,
    AutonomousDispatcher,
    DispatchLease,
    HostAgentExecutor,
    WorkerContext,
    build_worker_context,
    clip,
    extract_outputs,
    lease_is_live,
)
