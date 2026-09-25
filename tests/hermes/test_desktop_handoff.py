"""A desktop command must schedule an agent turn, not render work as text."""
import importlib.util
import os
import types
from pathlib import Path

import pytest

from ge_hermes.commands import CommandSet
from ge_hermes.service import GraphService
from ge_hermes.tool import make_tool_handler
from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
import json


class Host:
    def __init__(self):
        self.calls = []

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        self.calls.append(order["node"])
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"result": "actual test output"})


@pytest.fixture
def service(tmp_path):
    settings = {"autonomous_agent_execution": True}
    return GraphService(tmp_path, lambda key, default: settings.get(key, default), Host())


def handler(service, name, *, desktop=True):
    fn = next(fn for key, fn, _, _ in CommandSet(service).handlers() if key == name)
    return fn.agent_turn_handler if desktop else fn


def test_task_text_is_handed_to_agent_intact_without_creating_bad_bullet_graph(service):
    task = "Build the runner.\n" + "- constraint, not an execution step\n" * 70
    result = handler(service, "ge")(task)
    assert result["type"] == "send"
    assert result["message"].endswith(task.strip())
    assert not service.engine().list_runs()
    assert "recent runs: none" in handler(service, "ge")("help")


def test_approval_hands_off_exact_run_and_agent_executes_it_once(service):
    created = json.loads(handler(service, "ge-analyze")("Read status --json"))
    run_id = created["run_id"]
    result = handler(service, "ge-approve")(run_id + " plan")
    assert result["type"] == "send"
    assert "action=resume" in result["message"] and run_id in result["message"]
    assert service.host_executor.calls == []  # slash handler is not an agent turn
    assert service.engine().load(run_id)[0]["approvals"]["plan"]["status"] == "APPROVED"
    tool = make_tool_handler(service)
    executed = json.loads(tool({"action": "resume", "run_id": run_id}))
    assert executed["status"]["status"] == "SUCCEEDED"
    assert len(service.host_executor.calls) == 1
    receipt = json.loads(tool({"action": "verify", "run_id": run_id}))
    assert receipt["ok"]
    assert handler(service, "ge-run")(run_id)["type"] == "send"


@pytest.mark.parametrize("entrypoint", ["slash.exec", "command.dispatch"])
def test_desktop_transport_preserves_send_directive(service, monkeypatch, entrypoint):
    path = os.environ.get("GE_HOST_METHODS") or str(Path(__file__).resolve().parents[3] / "methods_tools.py")
    if not Path(path).is_file():
        try:
            host_spec = importlib.util.find_spec("tui_gateway.methods_tools")
        except ModuleNotFoundError:
            host_spec = None
        if host_spec is None:
            pytest.skip("Hermes desktop transport is not installed")
        path = host_spec.origin
    spec = importlib.util.spec_from_file_location("tui_gateway._ge_transport_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = types.SimpleNamespace(
        _methods={}, contextlib=__import__("contextlib"),
        _ok=lambda rid, result: {"result": result},
        _err=lambda rid, code, message: {"error": {"code": code, "message": message}},
        _sessions={"desktop": {}}, _profile_scoped=lambda f: f,
        _WORKER_BLOCKED_COMMANDS=set(), _SESSION_CONTROL_SLASHES=set(), _PENDING_INPUT_COMMANDS=set(),
    )
    module.register(server)
    plugin = types.SimpleNamespace(resolve_plugin_command_result=lambda result: result)
    server._tools_mod = lambda name: plugin
    server._plugin_command_handler = lambda name: handler(service, name, desktop=False)
    server._sess_nowait = lambda params, rid: ({}, None)
    server._live_slash_command_output = lambda *args: None
    server._bundle_key_for = lambda name: None
    server._is_profile_skill_command = lambda *args: False
    server._resolve_name = lambda name: name
    server._dispatch_quick = lambda *args: None
    params = {"session_id": "desktop", "name": "ge", "arg": "Read status", "command": "ge Read status"}
    result = server._methods[entrypoint](1, params)["result"]
    assert result["type"] == "send" and result["message"].endswith("Read status")
    assert "output" not in result
    server._plugin_command_handler = lambda name: lambda arg: "ordinary plugin output"
    plain = server._methods[entrypoint](2, params)["result"]
    assert plain["output"] == "ordinary plugin output"
