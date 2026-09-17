"""Hermes binding against a PluginContext double: registration, commands, tool, config."""
import json

import pytest

import ge_hermes
from ge_hermes.config import parse_config
from ge_runtime.errors import ErrorCode, GraphEngineeringError

SMOKE = "Create an execution graph for: 1. read input 2. validate input 3. transform input 4. verify result"


class FakeContext:
    """Implements only the PluginContext surface the binding is allowed to use."""

    def __init__(self, data_dir, settings=None, builtin_commands=()):
        self.state = type("State", (), {"data_dir": data_dir})()
        self.settings = dict(settings or {})
        self.builtin_commands = set(builtin_commands)
        self.commands, self.tools, self.skills = {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        if name in self.builtin_commands:
            return None  # Hermes refuses names that collide with built-in commands
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}
        return object()

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}
        return object()

    def register_skill(self, name, path, description="", frontmatter=None):
        assert path.is_file()
        self.skills[name] = path
        return object()


@pytest.fixture
def ctx(tmp_path):
    context = FakeContext(tmp_path / "plugin-data")
    ge_hermes.register(context)
    return context


def run(ctx, name, args=""):
    return ctx.commands[name]["handler"](args)


def run_json(ctx, name, args=""):
    return json.loads(run(ctx, name, args + " --json"))


def tool(ctx, **args):
    return json.loads(ctx.tools["ge_graph"]["handler"](args))


def test_registration_surface(ctx):
    assert tuple(ctx.commands) == ge_hermes.COMMANDS
    assert all(entry["description"] for entry in ctx.commands.values())
    assert set(ctx.tools) == {"ge_graph"} and ctx.tools["ge_graph"]["toolset"] == "graph_engineering"
    schema = ctx.tools["ge_graph"]["schema"]
    assert schema["name"] == "ge_graph" and "approve" not in schema["parameters"]["properties"]["action"]["enum"]
    assert set(ctx.skills) == {"graph-engineering"}
    assert ge_hermes.LAST_REGISTRATION["refused_commands"] == []
    assert ge_hermes.LAST_REGISTRATION["commands"] == list(ge_hermes.COMMANDS)


def test_collision_refusal_is_detected_not_assumed(tmp_path):
    context = FakeContext(tmp_path, builtin_commands={"ge-run"})
    ge_hermes.register(context)
    assert "ge-run" not in context.commands
    assert ge_hermes.LAST_REGISTRATION["refused_commands"] == ["ge-run"]


def test_help_lists_commands_and_executors(ctx):
    text = run(ctx, "ge")
    assert "/ge-analyze" in text and "builtin(enabled)" in text and "recent runs: none" in text


def test_analyze_creates_inspectable_draft_run(ctx):
    result = run_json(ctx, "ge-analyze", SMOKE)
    run_id = result["run_id"]
    assert result["plan"]["execution_order"] == ["read_input", "validate_input", "transform_input", "verify_result"]
    assert run_json(ctx, "ge-status", run_id)["status"]["status"] == "DRAFT"
    assert run_json(ctx, "ge-plan", "last")["run_id"] == run_id
    text = run(ctx, "ge-explain", run_id)
    assert "why: verify result" in text
    preview = run_json(ctx, "ge-analyze", "--preview " + SMOKE)
    assert preview["run_id"] is None


def test_template_run_end_to_end_via_commands(ctx):
    created = run_json(ctx, "ge-create", "--template text-pipeline --text \"  make   me loud \"")
    run_id = created["run_id"]
    assert "GE_ERROR GATE_FAILED" in run(ctx, "ge-run", run_id)
    assert "approved plan" in run(ctx, "ge-approve", run_id)
    text = run(ctx, "ge-run", run_id)
    assert text.startswith("Run %s: SUCCEEDED" % run_id)
    assert "VERIFIED" in run(ctx, "ge-verify", run_id)
    assert run_json(ctx, "ge-status", run_id)["status"]["completed_nodes"] == [
        "read_input", "validate_input", "transform_input", "verify_result"]


def test_validate_command_reports_errors_with_codes(ctx, tmp_path):
    spec = run_json(ctx, "ge-analyze", "--preview " + SMOKE)["spec"]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert run(ctx, "ge-validate", str(path)).startswith("VALID")
    spec["nodes"][0]["depends_on"] = ["verify_result"]
    text = run(ctx, "ge-validate", json.dumps(spec))
    assert text.startswith("GE_ERROR DEPENDENCY_CYCLE") and "cycle:" in text
    assert run(ctx, "ge-validate", "").startswith("GE_ERROR INVALID_ARGUMENT")
    assert run(ctx, "ge-status", "not a run").startswith("GE_ERROR INVALID_ARGUMENT")


def test_submit_and_gate_commands(ctx):
    run_id = run_json(ctx, "ge-analyze", "1. write the report 2. verify the report")["run_id"]
    run(ctx, "ge-approve", run_id + " plan")
    state = run_json(ctx, "ge-run", run_id)
    assert state["status"]["holds"][0] == {"kind": "WAITING_FOR_APPROVAL", "node": "write_the_report", "gate": "approval"}
    run(ctx, "ge-approve", run_id + " write_the_report:approval")
    state = run_json(ctx, "ge-submit", '%s write_the_report {"result": "report text"}' % run_id)
    assert state["work_orders"][0]["node"] == "verify_the_report"
    state = run_json(ctx, "ge-submit", '%s verify_the_report {"result": "checked", "passed": true}' % run_id)
    assert state["status"]["status"] == "SUCCEEDED"


def test_cancel_retry_resume_commands(ctx):
    run_id = run_json(ctx, "ge-analyze", "1. research topic 2. summarize")["run_id"]
    run(ctx, "ge-approve", run_id)
    run(ctx, "ge-run", run_id)
    assert "recovered: nothing" in run(ctx, "ge-resume", run_id)
    assert run(ctx, "ge-retry", run_id + " research_topic").startswith("GE_ERROR INVALID_TRANSITION")
    assert "CANCELLED" in run(ctx, "ge-cancel", run_id)


def test_commands_never_raise(ctx, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("ge_hermes.commands.analyze_task", explode)
    assert run(ctx, "ge-analyze", "task").startswith("GE_ERROR INTERNAL: RuntimeError")


def test_tool_agent_flow_and_operator_only_actions(ctx):
    analyzed = tool(ctx, action="analyze", task=SMOKE)
    run_id = analyzed["run_id"]
    assert tool(ctx, action="run", run_id=run_id)["error"] == "GATE_FAILED"
    assert tool(ctx, action="approve", run_id=run_id)["error"] == "INVALID_ARGUMENT"
    run(ctx, "ge-approve", run_id)
    state = tool(ctx, action="run", run_id=run_id)
    count = 0
    while state["work_orders"]:
        order = state["work_orders"][0]
        outputs = {"result": "ok"}
        if "passed" in order["outputs"]:
            outputs["passed"] = True
        state = tool(ctx, action="submit", run_id=run_id, node=order["node"], outputs=outputs)
        count += 1
    assert count == 4 and state["status"]["status"] == "SUCCEEDED"
    assert tool(ctx, action="verify", run_id="last")["receipt"]["verified"] is True
    assert tool(ctx, action="list")["runs"][0]["run_id"] == run_id
    assert tool(ctx, action="explain", run_id=run_id)["explanation"]["nodes"][0]["id"] == "read_input"


def test_tool_handles_bad_input(ctx):
    handler = ctx.tools["ge_graph"]["handler"]
    assert json.loads(handler("{not json"))["error"] == "INVALID_ARGUMENT"
    assert json.loads(handler({"action": "explode"}))["error"] == "INVALID_ARGUMENT"
    assert json.loads(handler({"action": "create", "spec": "nodes: [1"}))["error"] == "GRAPH_INVALID"
    assert json.loads(handler({"action": "validate", "template": "text-pipeline"}))["valid"] is True


@pytest.mark.parametrize("settings, fragment", [
    ({"require_plan_approval": "yes"}, "require_plan_approval"),
    ({"disabled_executors": "agent"}, "disabled_executors"),
    ({"disabled_executors": ["gpu"]}, "unknown executor"),
    ({"max_steps": 0}, "max_steps"),
    ({"max_steps": True}, "max_steps"),
])
def test_invalid_configuration_fails_closed(tmp_path, settings, fragment):
    with pytest.raises(GraphEngineeringError) as info:
        parse_config(lambda key, default: settings.get(key, default))
    assert info.value.code is ErrorCode.PLUGIN_CONFIGURATION_INVALID and fragment in info.value.message
    context = FakeContext(tmp_path, settings)
    ge_hermes.register(context)  # registration still succeeds so the error is reported, not hidden
    assert run(context, "ge-status").startswith("GE_ERROR PLUGIN_CONFIGURATION_INVALID")
    assert json.loads(context.tools["ge_graph"]["handler"]({"action": "list"}))["error"] == "PLUGIN_CONFIGURATION_INVALID"


def test_valid_configuration_is_applied(tmp_path):
    config = parse_config(lambda key, default: {"disabled_executors": ["agent"], "max_steps": 7}.get(key, default))
    assert config.disabled_executors == ("agent",) and config.max_steps == 7 and config.require_plan_approval is True
    context = FakeContext(tmp_path, {"require_plan_approval": False, "disabled_executors": ["agent"]})
    ge_hermes.register(context)
    assert run_json(context, "ge-analyze", SMOKE)["error"] == "EXECUTOR_UNAVAILABLE"
    run_id = run_json(context, "ge-create", "--template text-pipeline")["run_id"]
    assert run_json(context, "ge-run", run_id)["status"]["status"] == "SUCCEEDED"


def test_unparseable_spec_file_is_not_echoed(ctx, tmp_path):
    unparseable = "line one: [unclosed\nprivate-content-marker"
    path = tmp_path / "broken.yaml"
    path.write_text(unparseable, encoding="utf-8")
    text = run(ctx, "ge-validate", str(path))
    assert text.startswith("GE_ERROR GRAPH_INVALID") and "broken.yaml" in text
    assert "private-content-marker" not in text and "unclosed" not in text


def test_state_write_failure_reaches_the_user_as_error_code(ctx, monkeypatch):
    import ge_runtime.store as store_module

    def refuse(path, text):
        raise PermissionError("simulated")

    monkeypatch.setattr(store_module, "atomic_write_text", refuse)
    text = run(ctx, "ge-create", "--template text-pipeline")
    assert text.startswith("GE_ERROR STATE_WRITE_FAILED")
