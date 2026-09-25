"""Shared glue between Hermes entry points (commands, tool, hooks) and ge_runtime.

Configuration is read on every call so settings changes apply without a restart;
the state directory is fixed at registration time.

Autopilot continuation uses only public plugin APIs: ``PluginContext.inject_message``
queues a new agent turn (CLI: always; gateway: with ``allow_gateway_injection``), and the
``post_llm_call`` hook notices when a turn ended while an autopilot run it drove still has
work that needs an agent turn.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from ge_runtime.autopilot import CONTINUE, NEEDS_AGENT_TURN, Autopilot
from ge_runtime.dispatch import STOP_HOST_UNAVAILABLE, AutonomousDispatcher, HostAgentExecutor
from ge_runtime.engine import GraphEngine, pending_work
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.evidence import EvidenceRunner
from ge_runtime.executors import ExecutorCatalog
from ge_runtime.spec import MAX_SPEC_BYTES, load_spec_text
from ge_runtime.states import RUN_TERMINAL, RunStatus
from ge_runtime.store import RunStore

from .config import PluginConfig, parse_config
from .host import toolsets_for_risk
from .templates import TEMPLATES, build_template

LATEST_ALIASES = ("last", "latest")
SPEC_FILE_SUFFIXES = (".json", ".yaml", ".yml")
logger = logging.getLogger("ge_hermes")

CONTINUATION_PROMPT = (
    "Graph Engineering autopilot: run {run_id} has work that needs an agent turn. Call the ge_graph tool with "
    "action=auto and run_id={run_id} now, and call it again while its response says \"continue\": true. Do not "
    "create a new graph, do not do the nodes' work yourself and do not approve anything; the autopilot dispatches "
    "each node to its own worker and checks the results. When the response carries a final_report, reply with a "
    "short summary of it. If it names an operator action, tell the user exactly that command.")


class GraphService:
    def __init__(self, data_dir: str | Path, get_config: Callable[[str, Any], Any],
                 host_executor: HostAgentExecutor | None = None,
                 injector: Callable[[str], Any] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self._get_config = get_config
        self.host_executor = host_executor
        self.injector = injector  # PluginContext.inject_message, when the host offers it
        self._continuations: dict[str, str | None] = {}  # run_id -> origin session id (None: any session)
        self._lock = threading.Lock()

    def config(self) -> PluginConfig:
        return parse_config(self._get_config)

    def engine(self) -> GraphEngine:
        config = self.config()
        store = RunStore(self.data_dir)
        return GraphEngine(
            store,
            ExecutorCatalog(disabled=config.disabled_executors),
            require_plan_approval=config.require_plan_approval,
            max_steps=config.max_steps,
            evidence=EvidenceRunner(store, exclude=self.data_dir),
        )

    def host_supported(self) -> bool:
        """Whether the running host can execute agent work (feature detection, never raises)."""
        try:
            return self.host_executor is not None and bool(self.host_executor.supports_agent_execution())
        except Exception:
            return False

    def autonomous_status(self) -> dict[str, Any]:
        return {"enabled": self.config().autonomous_agent_execution, "host_supported": self.host_supported(),
                "continuation": self.injector is not None}

    def workspace(self) -> str:
        root = self.config().workspace_root or os.getcwd()
        path = Path(root).expanduser()
        if not path.is_dir():
            raise GraphEngineeringError(ErrorCode.PLUGIN_CONFIGURATION_INVALID,
                                        "workspace_root %s is not a directory" % path.name)
        return str(path.resolve())

    # -- legacy run/resume dispatch (autonomous_agent_execution) ---------------------
    def drive(self, engine: GraphEngine, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """After run/resume: let the host execute waiting agent nodes, if enabled.

        Returns the latest state and a report (``None`` when autonomous execution is off or
        there was nothing to dispatch). A host that cannot execute yields a report carrying
        ``HOST_EXECUTION_UNAVAILABLE``; the run itself is untouched and stays manually usable.
        """
        config = self.config()
        if not config.autonomous_agent_execution:
            return state, None
        loaded, spec = engine.load(state["run_id"])
        if not pending_work(loaded, spec):
            return state, None
        if not self.host_supported():
            return state, {"enabled": True, "supported": False, "dispatched": [], "stopped": STOP_HOST_UNAVAILABLE,
                           "error": ErrorCode.HOST_EXECUTION_UNAVAILABLE.value,
                           "message": "host agent execution unavailable: this Hermes host does not expose an agent "
                                      "execution API; submit node outputs manually"}
        dispatcher = AutonomousDispatcher(engine, self.host_executor, state_key=str(self.data_dir.resolve()),
                                          node_timeout_seconds=config.autonomous_node_timeout_seconds,
                                          call_budget_seconds=config.autonomous_call_budget_seconds,
                                          toolsets_for=toolsets_for_risk)
        report = dispatcher.drive(state["run_id"])
        return engine.load(state["run_id"])[0], report

    # -- autopilot --------------------------------------------------------------------
    def autopilot(self, engine: GraphEngine | None = None) -> Autopilot:
        config = self.config()
        return Autopilot(engine or self.engine(), self.host_executor if self.host_supported() else None,
                         policy=config.policy, budget=config.budget, state_key=str(self.data_dir.resolve()),
                         node_timeout_seconds=config.autonomous_node_timeout_seconds,
                         toolsets_for=toolsets_for_risk)

    def start_autopilot(self, *, task: str | None = None, spec: Any = None, created_by: str,
                        origin: Mapping[str, Any] | None = None) -> str:
        pilot = self.autopilot()
        return pilot.start(task=task, spec=spec, created_by=created_by, workspace=self.workspace(), origin=origin)

    def drive_autopilot(self, run_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        pilot = self.autopilot()
        state, _spec = pilot.engine.load(run_id)
        if not (state.get("autopilot") or {}).get("mode"):
            pilot.adopt(run_id)
        report = pilot.drive(run_id, call_budget_seconds=self.config().autonomous_call_budget_seconds)
        self._track(run_id, report, session_id)
        report["continue"] = report["status"] == CONTINUE
        return report

    def _track(self, run_id: str, report: Mapping[str, Any], session_id: str | None) -> None:
        with self._lock:
            if report.get("status") in (CONTINUE, NEEDS_AGENT_TURN):
                self._continuations[run_id] = session_id
            else:
                self._continuations.pop(run_id, None)

    def request_agent_turn(self, run_id: str, session_id: str | None = None) -> bool:
        """Queue a new agent turn that continues ``run_id`` (public ``inject_message``).

        Loop guard: a new turn is requested only if the run was driven since the previous
        request; a turn that did not drive the run ends the automatic continuation.
        """
        if self.injector is None or not self.config().autopilot_continue_turns:
            return False
        engine = self.engine()
        try:
            state, _spec = engine.load(run_id)
        except GraphEngineeringError:
            return False
        if RunStatus(state["status"]) in RUN_TERMINAL:
            return False
        autopilot = state.get("autopilot") or {}
        calls = int((autopilot.get("usage") or {}).get("drive_calls") or 0)
        previous = autopilot.get("turn_requested_at_call")
        if previous is not None and calls <= int(previous):
            with self._lock:
                self._continuations.pop(run_id, None)
            return False
        if calls >= int((autopilot.get("budget") or {}).get("max_drive_calls") or 200):
            return False
        try:
            accepted = bool(self.injector(CONTINUATION_PROMPT.format(run_id=run_id)))
        except Exception as exc:  # the host refused (for example gateway injection not allowed)
            logger.warning("hermes-graph-engineering: could not request an agent turn: %s", exc)
            accepted = False
        engine.update_autopilot(run_id, {"turn_requested_at_call": calls,
                                         "continued_turns": int(autopilot.get("continued_turns") or 0) + 1},
                                event="autopilot.turn_requested", session=session_id, accepted=accepted)
        if accepted:
            with self._lock:
                self._continuations[run_id] = session_id
        return accepted

    def on_turn_finished(self, session_id: str = "", platform: str = "", **_kwargs: Any) -> None:
        """``post_llm_call`` hook: an agent turn ended; continue unfinished autopilot runs it drove."""
        if platform == "subagent" or self.injector is None:
            return None
        with self._lock:
            candidates = [run for run, origin in self._continuations.items() if origin in (None, session_id)]
        for run_id in candidates:
            try:
                state, _spec = self.engine().load(run_id)
                last = ((state.get("autopilot") or {}).get("last") or {}).get("status")
                if RunStatus(state["status"]) in RUN_TERMINAL or last not in (CONTINUE, NEEDS_AGENT_TURN):
                    with self._lock:
                        self._continuations.pop(run_id, None)
                    continue
                self.request_agent_turn(run_id, session_id or None)
            except GraphEngineeringError as exc:
                logger.warning("hermes-graph-engineering: continuation of %s skipped: %s", run_id, exc)
        return None

    # -- misc -------------------------------------------------------------------------
    def resolve_run(self, engine: GraphEngine, token: Any) -> str:
        if not isinstance(token, str) or not token.strip():
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "a run id (or 'last') is required")
        token = token.strip()
        if token in LATEST_ALIASES:
            runs = engine.store.list_runs()
            if not runs:
                raise GraphEngineeringError(ErrorCode.RUN_NOT_FOUND, "there are no runs yet")
            return runs[0]
        engine.store.run_dir(token)  # validates the id format
        return token

    @staticmethod
    def spec_from_source(source: Any) -> Any:
        """Accept a spec mapping, inline JSON/YAML text, or a path to a .json/.yaml file."""
        if isinstance(source, Mapping):
            return dict(source)
        if not isinstance(source, str) or not source.strip():
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "a spec (inline JSON/YAML or file path) is required")
        text = source.strip()
        if "\n" not in text and text.lower().endswith(SPEC_FILE_SUFFIXES):
            path = Path(text.strip('"'))
            if not path.is_file():
                raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "spec file not found: %s" % path.name)
            if path.stat().st_size > MAX_SPEC_BYTES:
                raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "spec file exceeds %d bytes" % MAX_SPEC_BYTES)
            try:
                return load_spec_text(path.read_text(encoding="utf-8"))
            except (GraphEngineeringError, UnicodeDecodeError) as exc:
                # parser messages quote file content; never echo a local file into chat
                raise GraphEngineeringError(ErrorCode.GRAPH_INVALID,
                                            "spec file %s is not valid JSON or YAML" % path.name) from exc
        return load_spec_text(text)

    @staticmethod
    def spec_from_template(name: str, text: str | None = None) -> dict[str, Any]:
        if name not in TEMPLATES:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "unknown template %r (known: %s)" % (
                name, ", ".join(sorted(TEMPLATES))))
        return build_template(name, text)
