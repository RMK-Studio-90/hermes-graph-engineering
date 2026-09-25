"""Hermes implementation of ``HostAgentExecutor``.

The only host API used is the documented public subagent lifecycle exposed as
``PluginContext.subagent_lifecycle`` ("Public Subagent Lifecycle API" in the
Hermes developer guide). It starts a fresh child Hermes session that runs under
the host's own model, routing, tool and permission policy, and it can only narrow
those permissions. Graph Engineering therefore neither selects nor configures a
model or provider, and it gains no permission the Hermes session does not have.

This is the single module allowed to import a Hermes module, and only the two
names of that documented API, lazily and inside a function. Everything is
feature-detected: a host without the API, or a call outside an active agent turn
(the API needs one), yields ``EXECUTOR_UNAVAILABLE`` and nothing is started.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult

from ge_runtime.dispatch import WorkerContext, clip, extract_outputs

# Documented failure text of ``launch`` outside an active agent turn.
NO_SESSION_MARKER = "No active Hermes parent session"
REQUIRED_METHODS = ("launch", "wait", "result", "cancel")
WAIT_SLICE_SECONDS = 30.0
CANCEL_GRACE_SECONDS = 15.0


def _state(value: Any) -> str:
    return str(getattr(value, "value", value))


def _unavailable(detail: str) -> ExecutorResult:
    return ExecutorResult(ExecutorOutcome.EXECUTOR_UNAVAILABLE, detail=detail)


class HermesHostExecutor:
    def __init__(self, ctx: Any) -> None:
        # Held, never touched at registration: capability is probed when work is dispatched.
        self._ctx = ctx

    def _lifecycle(self) -> Any:
        try:
            return getattr(self._ctx, "subagent_lifecycle", None)
        except Exception:  # an older host, or a host without the lifecycle module
            return None

    def supports_agent_execution(self) -> bool:
        service = self._lifecycle()
        return service is not None and all(callable(getattr(service, name, None)) for name in REQUIRED_METHODS)

    def execute_agent_work(self, work_order: Mapping[str, Any], context: WorkerContext) -> ExecutorResult:
        if not self.supports_agent_execution():
            return _unavailable("host agent execution unavailable: the host exposes no agent execution API")
        service = self._lifecycle()
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleError
        except ImportError:
            return _unavailable("host agent execution unavailable: the host exposes no agent execution API")

        request = SubagentLaunchRequest(
            goal=context.goal,
            context=context.context,
            role="leaf",
            correlation_id=context.correlation_id,
            metadata={"graph_engineering": {"run_id": context.run_id, "node": context.node_id,
                                            "attempt": context.attempt}},
        )
        try:
            handle = service.launch(request)
        except SubagentLifecycleError as exc:
            if NO_SESSION_MARKER in str(exc):
                return _unavailable("host agent execution unavailable: no active agent session (run this from an "
                                    "agent turn, for example through the ge_graph tool)")
            return ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE,
                                  detail="host rejected the task: " + clip(exc))
        except Exception as exc:  # the host could not create a worker; nothing ran
            return _unavailable("host agent execution unavailable: worker could not be created (%s)"
                                % type(exc).__name__)

        deadline = time.monotonic() + context.timeout_seconds
        completed = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            terminal = service.wait(handle, timeout_seconds=min(WAIT_SLICE_SECONDS, remaining))
            if getattr(terminal, "completed", False):
                completed = True
                break
        if not completed:
            service.cancel(handle, reason="Graph Engineering node timeout")
            service.wait(handle, timeout_seconds=CANCEL_GRACE_SECONDS)
            return ExecutorResult(ExecutorOutcome.TIMEOUT,
                                  detail="host worker exceeded %d seconds" % context.timeout_seconds)
        return self._map_result(service.result(handle), work_order)

    @staticmethod
    def _map_result(result: Any, work_order: Mapping[str, Any]) -> ExecutorResult:
        state = _state(getattr(result, "terminal_state", "UNKNOWN"))
        if state == "SUCCEEDED":
            declared = work_order["outputs"]
            payload = getattr(result, "structured_payload", None)
            outputs, why = extract_outputs(payload, declared) if payload else (None, "no structured payload")
            if outputs is None:  # native structured output is optional; fall back to the text reply
                outputs, why = extract_outputs(getattr(result, "summary", None), declared)
            if outputs is None:
                return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail="host returned invalid result: " + why)
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs=outputs)
        if state in ("CANCELLED", "INTERRUPTED"):
            return ExecutorResult(ExecutorOutcome.CANCELLED, detail="host worker was %s" % state.lower())
        classification = getattr(result, "error_classification", None) or state
        message = getattr(result, "error_message", None)
        detail = "host execution failed (%s)" % clip(classification, 60)
        if message:
            detail += "; host diagnostic: " + clip(message, 300)
        return ExecutorResult(ExecutorOutcome.RETRYABLE_FAILURE, detail=detail)
