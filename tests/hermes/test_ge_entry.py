"""``/ge <task>`` and ``ge_graph action=auto``: the autopilot through the standard plugin surface only.

No desktop-specific hook is involved: commands return text (``fn(raw_args) -> str``), a new agent
turn is requested through the public ``PluginContext.inject_message`` API, workers run through
the public subagent lifecycle API, and ``post_llm_call`` continues unfinished runs.
"""
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import ge_hermes

TASK = '1. Create `hello.txt` containing "HELLO GE2" 2. Summarize what was created'


class LaunchError(ValueError):
    pass


class Lifecycle:
    """The documented lifecycle service: works only inside an agent turn, like Hermes."""

    def __init__(self, workspace):
        self.workspace, self.in_turn, self.requests = Path(workspace), False, []

    def launch(self, request):
        if not self.in_turn:
            raise LaunchError("No active Hermes parent session is available.")
        self.requests.append(request)
        node = re.search(r"Node: (\S+)", request.goal).group(1)
        if node.startswith("create_hello"):
            (self.workspace / "hello.txt").write_text("HELLO GE2\n", encoding="utf-8")
        reply = '```json\n{"result": "did %s"}\n```' % node
        return SimpleNamespace(subagent_id="sa-%d" % len(self.requests), reply=reply)

    def wait(self, handle, timeout_seconds=None):
        return SimpleNamespace(completed=True)

    def cancel(self, handle, reason):
        pass

    def result(self, handle):
        return SimpleNamespace(terminal_state="SUCCEEDED", summary=handle.reply, structured_payload=None)


class Context:
    def __init__(self, data_dir, workspace, lifecycle, settings=None):
        self.state = SimpleNamespace(data_dir=data_dir)
        self.settings = dict({"workspace_root": str(workspace)}, **(settings or {}))
        self.subagent_lifecycle = lifecycle
        self.commands, self.tools, self.hooks, self.injected = {}, {}, {}, []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler
        return object()

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = handler
        return object()

    def register_skill(self, name, path, description=""):
        return object()

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def inject_message(self, content, role="user", session_key=None):
        self.injected.append(content)
        return True


@pytest.fixture
def lifecycle_api(monkeypatch):
    module = types.ModuleType("agent.subagent_lifecycle")
    module.SubagentLaunchRequest = lambda **kw: SimpleNamespace(**kw)
    module.SubagentLifecycleError = LaunchError
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", module)


@pytest.fixture
def plugin(tmp_path, lifecycle_api):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lifecycle = Lifecycle(workspace)
    ctx = Context(tmp_path / "data", workspace, lifecycle)
    ge_hermes.register(ctx)
    return ctx


def agent_turn(ctx, run_id, session="s1"):
    """What the host does with an injected message: an agent turn whose model calls ge_graph auto."""
    ctx.subagent_lifecycle.in_turn = True
    try:
        while True:
            result = json.loads(ctx.tools["ge_graph"]({"action": "auto", "run_id": run_id}, session_id=session))
            if not result.get("continue"):
                return result
    finally:
        ctx.subagent_lifecycle.in_turn = False
        for hook in ctx.hooks["post_llm_call"]:
            hook(session_id=session, platform="cli")


def test_ge_task_runs_to_a_verified_result_without_manual_steps(plugin):
    text = plugin.commands["ge"](TASK)
    assert isinstance(text, str)  # the standard command contract; no host-specific result objects
    run_id = re.search(r"autopilot started: (\S+)", text).group(1)
    assert "NEEDS_AGENT_TURN" in text and "Continuing automatically in an agent turn." in text
    [prompt] = plugin.injected  # requested through the public inject_message API
    assert "action=auto" in prompt and run_id in prompt
    result = agent_turn(plugin, run_id)
    assert result["final_report"]["outcome"] == "SUCCEEDED" and result["final_report"]["verified"] is True
    assert result["final_report"]["verification_level"] == "EVIDENCE"
    assert len(plugin.injected) == 1  # finished: no further turns requested
    status = json.loads(plugin.commands["ge-status"](run_id + " --json"))
    assert status["status"]["status"] == "SUCCEEDED" and status["status"]["finalization"] == "COMPLETE"
    requests = plugin.subagent_lifecycle.requests
    assert [r.allowed_toolsets for r in requests] == [("file",), ("file",)]  # WORKSPACE_WRITE, READ_ONLY
    assert len({r.correlation_id for r in requests}) == 2


def test_an_unfinished_run_is_continued_in_the_next_turn_with_a_loop_guard(plugin, monkeypatch):
    run_id = re.search(r"autopilot started: (\S+)", plugin.commands["ge"](TASK)).group(1)
    service_settings = plugin.settings
    service_settings["autonomous_call_budget_seconds"] = 10
    from ge_runtime import autopilot as autopilot_module

    real_drive = autopilot_module.Autopilot.drive
    calls = {"n": 0}

    def one_step(self, run, call_budget_seconds=None):
        calls["n"] += 1
        return real_drive(self, run, call_budget_seconds=0 if calls["n"] == 1 else call_budget_seconds)

    monkeypatch.setattr(autopilot_module.Autopilot, "drive", one_step)
    plugin.subagent_lifecycle.in_turn = True
    first = json.loads(plugin.tools["ge_graph"]({"action": "auto", "run_id": run_id}, session_id="s1"))
    assert first["continue"] is True and first["next"] == {"action": "auto", "run_id": run_id}
    plugin.subagent_lifecycle.in_turn = False
    for hook in plugin.hooks["post_llm_call"]:  # the model stopped calling; the turn ends
        hook(session_id="s1", platform="cli")
    assert len(plugin.injected) == 2  # a new turn was requested because the run advanced
    for hook in plugin.hooks["post_llm_call"]:  # a turn that did not drive the run
        hook(session_id="s1", platform="cli")
    assert len(plugin.injected) == 2  # loop guard: no endless re-injection
    result = agent_turn(plugin, run_id)
    assert result["final_report"]["verified"] is True


def test_turns_of_other_sessions_and_workers_do_not_trigger_continuations(plugin):
    run_id = re.search(r"autopilot started: (\S+)", plugin.commands["ge"](TASK)).group(1)
    plugin.subagent_lifecycle.in_turn = True
    json.loads(plugin.tools["ge_graph"]({"action": "auto", "run_id": run_id}, session_id="owner"))
    for hook in plugin.hooks["post_llm_call"]:
        hook(session_id="someone-else", platform="cli")
        hook(session_id="owner", platform="subagent")
    assert len(plugin.injected) == 1


def test_auto_from_the_tool_with_a_task_needs_no_slash_command(plugin):
    plugin.subagent_lifecycle.in_turn = True
    result = json.loads(plugin.tools["ge_graph"]({"action": "auto", "task": TASK}, session_id="s1"))
    assert result["final_report"]["verified"] is True
    assert "Graph Engineering result: SUCCEEDED - VERIFIED" in result["summary"]


def test_risky_tasks_stop_for_the_operator_and_continue_after_approval(plugin):
    text = plugin.commands["ge"]("1. Summarize the release notes 2. Deploy the service to production")
    run_id = re.search(r"autopilot started: (\S+)", text).group(1)
    result = agent_turn(plugin, run_id)
    assert result["autopilot"]["status"] == "NEEDS_OPERATOR"
    command = result["autopilot"]["next"]["operator"][0]["command"]
    assert command == "/ge-approve %s deploy_the_service_to_production:approval" % run_id
    assert "deploy" not in " ".join(r.goal for r in plugin.subagent_lifecycle.requests).lower().split("node: ")[-1][:6]
    reply = plugin.commands["ge-approve"](command[len("/ge-approve "):])
    assert reply.startswith("approved deploy_the_service_to_production:approval")
    assert "Continuing automatically in an agent turn." in reply
    final = agent_turn(plugin, run_id)
    assert final["final_report"]["outcome"] == "SUCCEEDED"


def test_the_worker_tool_guard_is_registered_through_public_hooks(plugin):
    assert set(plugin.hooks) == {"subagent_start", "pre_tool_call", "post_llm_call"}


def test_status_recovers_a_lost_receipt(plugin):
    plugin.subagent_lifecycle.in_turn = True
    result = json.loads(plugin.tools["ge_graph"]({"action": "auto", "task": TASK}, session_id="s1"))
    run_id = result["run_id"]
    receipt = Path(plugin.state.data_dir) / "runs" / run_id / "receipt.json"
    original = json.loads(receipt.read_text(encoding="utf-8"))
    receipt.unlink()  # lost (for example a crash of the storage or an operator mistake)
    text = plugin.commands["ge-status"](run_id)
    recovered = json.loads(receipt.read_text(encoding="utf-8"))
    assert "(recovery)" in text and recovered["generated_by"] == "recovery"
    assert recovered["verified"] is True and recovered["run_id"] == original["run_id"]
    assert [c["check"] for c in recovered["checks"]] == [c["check"] for c in original["checks"]]
