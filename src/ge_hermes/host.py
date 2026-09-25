"""Hermes implementation of the ``AgentWorker`` protocol.

The only host APIs used are documented plugin APIs:

* ``PluginContext.subagent_lifecycle`` ("Public Subagent Lifecycle API"): starts a *fresh*
  child Hermes session per node attempt. The child gets only the goal and context Graph
  Engineering hands it (the node's explicit artifacts), never the parent's conversation.
  It runs under the host's own model, routing and permission policy and can only narrow
  them. Graph Engineering neither selects nor configures a model or provider.
* ``PluginContext.register_hook`` with ``subagent_start`` and ``pre_tool_call``: the first
  links each child session to the node it works on (recorded in the run journal as proof
  of a fresh, isolated context); the second enforces the node's risk class at call time by
  blocking every tool outside the class's allowlist.

Worker rights by risk class (see ``ge_runtime.policy``)::

    READ_ONLY        toolsets: file              tools: read_file, search_files, skill_view, skills_list
    WORKSPACE_WRITE  toolsets: file              tools: + write_file, patch
    LOCAL_EXEC       toolsets: file, terminal    tools: + terminal, process_manage, execute_code
    NETWORK          toolsets: file, terminal, web   tools: + web_search, web_extract
    higher classes   run only after an operator approved the node; the host's own permissions apply

Nested delegation, clarifying questions and graph-changing ``ge_graph`` calls are blocked for
every worker. When the host refuses to narrow toolsets at launch (its session enables only
composite toolsets), the worker is launched with the host's toolsets and the ``pre_tool_call``
allowlist still enforces the class; the journal records that mode.

This is the single module allowed to import a Hermes module, and only the two names of the
lifecycle API, lazily and inside a function. Everything is feature-detected: a host without
the API, or a call outside an active agent turn, yields ``EXECUTOR_UNAVAILABLE``.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Mapping

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.dispatch import WorkerContext, clip, extract_outputs
from ge_runtime.guard import READ_ONLY_ACTIONS
from ge_runtime.policy import RiskClass, worker_level

# Documented failure text of ``launch`` outside an active agent turn.
NO_SESSION_MARKER = "No active Hermes parent session"
REQUIRED_METHODS = ("launch", "wait", "result", "cancel")
WAIT_SLICE_SECONDS = 30.0
CANCEL_GRACE_SECONDS = 15.0
WORKER_MARKER = "Graph Engineering worker reference: "

TOOLSETS_FOR_LEVEL: dict[RiskClass, tuple[str, ...]] = {
    RiskClass.READ_ONLY: ("file",),
    RiskClass.WORKSPACE_WRITE: ("file",),
    RiskClass.LOCAL_EXEC: ("file", "terminal"),
    RiskClass.NETWORK: ("file", "terminal", "web"),
}
_READ_TOOLS = frozenset({"read_file", "search_files", "skill_view", "skills_list", "todo_list", "ge_graph"})
TOOLS_FOR_LEVEL: dict[RiskClass, frozenset[str]] = {
    RiskClass.READ_ONLY: _READ_TOOLS,
    RiskClass.WORKSPACE_WRITE: _READ_TOOLS | {"write_file", "patch"},
    RiskClass.LOCAL_EXEC: _READ_TOOLS | {"write_file", "patch", "terminal", "process_manage", "execute_code"},
    RiskClass.NETWORK: _READ_TOOLS | {"write_file", "patch", "terminal", "process_manage", "execute_code",
                                      "web_search", "web_extract"},
}
ALWAYS_BLOCKED = frozenset({"delegate_task", "clarify", "cronjob_manage", "kanban_create", "skill_manage"})


def toolsets_for_risk(risk: str | None) -> tuple[str, ...] | None:
    level = worker_level(risk)
    return TOOLSETS_FOR_LEVEL.get(level) if level is not None else None


def _state(value: Any) -> str:
    return str(getattr(value, "value", value))


def _unavailable(detail: str) -> ExecutorResult:
    return ExecutorResult(ExecutorOutcome.EXECUTOR_UNAVAILABLE, detail=detail)


class WorkerRegistry:
    """Process-wide map of live Graph Engineering workers (correlation id / child session -> node)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._expected: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}

    def expect(self, context: WorkerContext) -> dict[str, Any]:
        level = worker_level(context.risk)
        info = {"run_id": context.run_id, "node": context.node_id, "attempt": context.attempt,
                "correlation_id": context.correlation_id, "context_digest": context.context_digest,
                "risk": context.risk, "level": level.value if level else None,
                "tools": sorted(TOOLS_FOR_LEVEL[level]) if level is not None else None,
                "blocked_calls": [], "child_session_id": None, "parent_session_id": None, "subagent_id": None}
        with self._lock:
            self._expected[context.correlation_id] = info
        return info

    def forget(self, correlation_id: str) -> dict[str, Any] | None:
        with self._lock:
            info = self._expected.pop(correlation_id, None)
            if info and info.get("child_session_id"):
                self._sessions.pop(info["child_session_id"], None)
            return dict(info) if info else None

    # -- Hermes hooks (public plugin hook API) ------------------------------------
    def on_subagent_start(self, **kwargs: Any) -> None:
        goal = str(kwargs.get("child_goal") or "")
        marker = goal.rfind(WORKER_MARKER)
        if marker < 0:
            return None
        rest = goal[marker + len(WORKER_MARKER):].split()
        correlation = rest[0] if rest else ""
        with self._lock:
            info = self._expected.get(correlation)
            if info is None:
                return None
            info.update(child_session_id=kwargs.get("child_session_id"),
                        parent_session_id=kwargs.get("parent_session_id"),
                        subagent_id=kwargs.get("child_subagent_id"), started_at=time.time())
            if info["child_session_id"]:
                self._sessions[str(info["child_session_id"])] = info
        return None

    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", **_kwargs: Any) -> Any:
        with self._lock:
            info = self._sessions.get(str(session_id or ""))
        if info is None:
            return None
        reason = None
        if tool_name in ALWAYS_BLOCKED:
            reason = "%s is not available to a Graph Engineering node worker" % tool_name
        elif tool_name == "ge_graph":
            action = (args or {}).get("action") if isinstance(args, Mapping) else None
            if action not in READ_ONLY_ACTIONS:
                reason = "a node worker cannot change graphs (ge_graph %s)" % action
        elif info["tools"] is not None and tool_name not in info["tools"]:
            reason = "%s exceeds this node's risk class %s (allowed: %s)" % (
                tool_name, info["risk"], ", ".join(info["tools"]))
        if reason is None:
            return None
        with self._lock:
            info["blocked_calls"].append(tool_name)
        return {"action": "block", "message": "Graph Engineering policy: " + reason}


REGISTRY = WorkerRegistry()


class HermesHostExecutor:
    def __init__(self, ctx: Any, registry: WorkerRegistry | None = None) -> None:
        # Held, never touched at registration: capability is probed when work is dispatched.
        self._ctx = ctx
        self.registry = registry or REGISTRY
        self._workers: dict[str, dict[str, Any]] = {}

    def _lifecycle(self) -> Any:
        try:
            return getattr(self._ctx, "subagent_lifecycle", None)
        except Exception:  # an older host, or a host without the lifecycle module
            return None

    def supports_agent_execution(self) -> bool:
        service = self._lifecycle()
        return service is not None and all(callable(getattr(service, name, None)) for name in REQUIRED_METHODS)

    def worker_info(self, context: WorkerContext) -> dict[str, Any] | None:
        """What the host reported about the worker of this attempt (for the run journal)."""
        return self._workers.pop(context.correlation_id, None)

    def execute_agent_work(self, work_order: Mapping[str, Any], context: WorkerContext) -> ExecutorResult:
        if not self.supports_agent_execution():
            return _unavailable("host agent execution unavailable: the host exposes no agent execution API")
        service = self._lifecycle()
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleError
        except ImportError:
            return _unavailable("host agent execution unavailable: the host exposes no agent execution API")

        info = self.registry.expect(context)
        try:
            return self._run(service, SubagentLaunchRequest, SubagentLifecycleError, work_order, context, info)
        finally:
            final = self.registry.forget(context.correlation_id) or info
            self._workers[context.correlation_id] = {
                k: final.get(k) for k in ("child_session_id", "parent_session_id", "subagent_id", "risk", "level",
                                          "tools", "blocked_calls", "toolsets", "restriction")}

    def _run(self, service: Any, request_type: Any, error_type: Any, work_order: Mapping[str, Any],
             context: WorkerContext, info: dict[str, Any]) -> ExecutorResult:
        goal = context.goal + "\n\n" + WORKER_MARKER + context.correlation_id
        toolsets = context.allowed_toolsets

        def request(allowed: tuple[str, ...] | None) -> Any:
            return request_type(
                goal=goal,
                context=context.context,
                role="leaf",
                allowed_toolsets=allowed,
                correlation_id=context.correlation_id,
                metadata={"graph_engineering": {"run_id": context.run_id, "node": context.node_id,
                                                "attempt": context.attempt, "risk": context.risk,
                                                "context_digest": context.context_digest}},
            )

        info["toolsets"] = list(toolsets) if toolsets else None
        info["restriction"] = "toolsets+tool_guard" if toolsets else "tool_guard"
        try:
            try:
                handle = service.launch(request(toolsets))
            except error_type as exc:
                if toolsets and ("broaden" in str(exc) or "Unknown toolsets" in str(exc)):
                    # the host cannot narrow this session's toolsets at launch; the call-time guard still
                    # enforces the risk class
                    info["restriction"] = "tool_guard (host refused toolset narrowing: %s)" % clip(exc, 120)
                    info["toolsets"] = None
                    handle = service.launch(request(None))
                else:
                    raise
        except error_type as exc:
            if NO_SESSION_MARKER in str(exc):
                return _unavailable("host agent execution unavailable: no active agent session (run this from an "
                                    "agent turn, for example through the ge_graph tool)")
            return ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail="host rejected the task: " + clip(exc))
        except Exception as exc:  # the host could not create a worker; nothing ran
            return _unavailable("host agent execution unavailable: worker could not be created (%s)"
                                % type(exc).__name__)
        if not info.get("subagent_id"):
            info["subagent_id"] = getattr(handle, "subagent_id", None)

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
