"""Verify the installed plugin through Hermes' own plugin loader.

Run with the Hermes interpreter (the one that has ``hermes_cli`` importable):

    <hermes-python> scripts/hermes_probe.py --home <hermes-home> [--smoke] [--agent-smoke]
                                            [--autonomous-smoke] [--expect-run RUN_ID] [--reload]

Checks (all through Hermes APIs, never by importing the plugin directly):

* discovery: ``PluginManager.list_plugins()`` reports the plugin enabled, with
  version, no error, its commands and tool
* registration: every expected ``/ge*`` command resolves through
  ``get_plugin_command_handler``; tool ``ge_graph`` is in the tool registry;
  the skill is registered
* collisions: no expected command is a Hermes built-in, and no other plugin
  registers the same (gateway-normalized) command name
* ``--smoke``: task -> graph via ``/ge-analyze``, validation, explain, and a
  deterministic builtin run (create, approve, run, verify) via real handlers
* ``--agent-smoke``: agent flow through the real tool registry dispatch
  (analyze, run, work orders, submissions, verify)
* ``--autonomous-smoke``: autonomous agent execution through the real plugin loader and the
  real Hermes subagent lifecycle service. The home's config must enable
  ``plugins.entries.hermes-graph-engineering.settings.autonomous_agent_execution``.
  This is the PLUGIN_INTEGRATION_TEST: only the worker's model turn is replaced by a
  deterministic stand-in, so it needs no model, provider or credentials. It proves host
  capability detection, the outside-a-turn fallback, dispatch inside a bound agent turn,
  plan/approval gates, worker recursion protection through the host's own thread pool, and
  verification. It is NOT a live model run (LIVE_MODEL_EXECUTION_TEST).
* ``--expect-run``: a run created by an earlier process is still loadable and
  verified (state survives restart)
* ``--reload``: force re-discovery in-process and re-check registration

Prints one JSON report; exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import types

PLUGIN = "hermes-graph-engineering"
EXPECTED_COMMANDS = (
    "ge", "ge-analyze", "ge-create", "ge-validate", "ge-plan", "ge-approve", "ge-run", "ge-status",
    "ge-verify", "ge-resume", "ge-explain", "ge-submit", "ge-retry", "ge-cancel",
)
SMOKE_TASK = ("Create an execution graph for: 1. read input 2. validate input "
              "3. transform input 4. verify result")


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.data: dict = {}

    def check(self, name: str, passed: bool, detail: object = "") -> bool:
        self.checks.append({"check": name, "passed": bool(passed), "detail": detail})
        return bool(passed)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c["passed"] for c in self.checks)


def _json_command(handler_for, name: str, args: str) -> dict:
    handler = handler_for(name)
    if handler is None:
        raise RuntimeError("command /%s not registered" % name)
    return json.loads(handler(args + " --json"))


def check_registration(report: Report, label: str) -> None:
    from hermes_cli.commands import resolve_command
    from hermes_cli.plugins import get_plugin_command_handler, get_plugin_commands, get_plugin_manager
    from tools.registry import registry

    manager = get_plugin_manager()
    manager.discover_and_load()
    plugins = {row["key"]: row for row in manager.list_plugins()}
    row = plugins.get(PLUGIN)
    report.data["%s_plugin" % label] = row
    report.check("%s:discovered" % label, row is not None, "listed by PluginManager.list_plugins")
    if row is None:
        return
    report.check("%s:enabled" % label, row["enabled"] is True and not row["error"], row["error"] or "enabled")
    report.check("%s:source_user" % label, row["source"] == "user", row["source"])
    report.check("%s:version" % label, bool(row["version"]), row["version"])

    commands = get_plugin_commands()
    ours = sorted(name for name, entry in commands.items() if entry.get("plugin_key") == PLUGIN)
    report.data["%s_commands" % label] = ours
    report.check("%s:commands_registered" % label, ours == sorted(EXPECTED_COMMANDS),
                 {"missing": sorted(set(EXPECTED_COMMANDS) - set(ours)), "extra": sorted(set(ours) - set(EXPECTED_COMMANDS))})
    report.check("%s:command_handlers_resolve" % label,
                 all(callable(get_plugin_command_handler(name)) for name in EXPECTED_COMMANDS), "get_plugin_command_handler")
    builtin_clash = [name for name in EXPECTED_COMMANDS if resolve_command(name) is not None]
    report.check("%s:no_builtin_command_collision" % label, not builtin_clash, builtin_clash or "none")
    normalized_ours = {name.replace("_", "-") for name in EXPECTED_COMMANDS}
    foreign = sorted(
        "%s (%s)" % (name, entry.get("plugin_key"))
        for name, entry in commands.items()
        if entry.get("plugin_key") != PLUGIN and name.replace("_", "-") in normalized_ours
    )
    report.check("%s:no_plugin_command_collision" % label, not foreign, foreign or "none")

    entry = registry.get_entry("ge_graph", scope=manager.scope_key)
    report.check("%s:tool_registered" % label, entry is not None and entry.toolset == "graph_engineering",
                 getattr(entry, "toolset", None))
    skill = manager.find_plugin_skill("%s:graph-engineering" % PLUGIN)
    report.check("%s:skill_registered" % label, skill is not None and os.path.isfile(str(skill)), str(skill))
    report.check("%s:counts" % label, row["commands"] == len(EXPECTED_COMMANDS) and row["tools"] == 1,
                 {"commands": row["commands"], "tools": row["tools"]})


def smoke(report: Report) -> None:
    from hermes_cli.plugins import get_plugin_command_handler

    analyzed = _json_command(get_plugin_command_handler, "ge-analyze", SMOKE_TASK)
    plan = analyzed.get("plan") or {}
    nodes = {node["id"]: node for node in plan.get("nodes", [])}
    expected_chain = ["read_input", "validate_input", "transform_input", "verify_result"]
    report.data["analyzed_run"] = analyzed.get("run_id")
    report.check("smoke:graph_created", analyzed.get("ok") is True and bool(analyzed.get("run_id")), analyzed.get("run_id"))
    report.check("smoke:graph_nodes", list(nodes) == expected_chain, list(nodes))
    report.check("smoke:graph_dependencies",
                 [nodes.get(n, {}).get("depends_on") for n in expected_chain] ==
                 [[], ["read_input"], ["validate_input"], ["transform_input"]],
                 {n: nodes.get(n, {}).get("depends_on") for n in expected_chain})
    report.check("smoke:execution_order", plan.get("execution_order") == expected_chain, plan.get("execution_order"))

    validated = _json_command(get_plugin_command_handler, "ge-validate", json.dumps(analyzed.get("spec")))
    report.check("smoke:graph_validation", validated.get("valid") is True, validated.get("spec_digest"))
    bad = dict(analyzed.get("spec") or {})
    bad_nodes = [dict(n) for n in bad.get("nodes", [])]
    if bad_nodes:
        bad_nodes[0]["depends_on"] = [bad_nodes[-1]["id"]]
        bad["nodes"] = bad_nodes
    rejected = _json_command(get_plugin_command_handler, "ge-validate", json.dumps(bad))
    report.check("smoke:cycle_rejected", rejected.get("error") == "DEPENDENCY_CYCLE", rejected.get("error"))

    explained = _json_command(get_plugin_command_handler, "ge-explain", analyzed["run_id"])
    report.check("smoke:explain", len(explained.get("nodes", [])) == 4, [n["id"] for n in explained.get("nodes", [])])
    status = _json_command(get_plugin_command_handler, "ge-status", analyzed["run_id"])
    report.check("smoke:status_inspectable", status.get("status", {}).get("status") == "DRAFT",
                 status.get("status", {}).get("status"))
    blocked = _json_command(get_plugin_command_handler, "ge-run", analyzed["run_id"])
    report.check("smoke:unapproved_run_refused", blocked.get("error") == "GATE_FAILED", blocked.get("error"))

    created = _json_command(get_plugin_command_handler, "ge-create", "--template text-pipeline")
    run_id = created.get("run_id")
    report.data["builtin_run"] = run_id
    _json_command(get_plugin_command_handler, "ge-approve", "%s plan" % run_id)
    ran = _json_command(get_plugin_command_handler, "ge-run", run_id)
    report.check("smoke:builtin_run_succeeded", ran.get("status", {}).get("status") == "SUCCEEDED",
                 ran.get("status", {}).get("status"))
    receipt = _json_command(get_plugin_command_handler, "ge-verify", run_id)
    report.check("smoke:builtin_run_verified", receipt.get("verified") is True,
                 [c["check"] for c in receipt.get("checks", []) if not c["passed"]] or "all checks passed")


def agent_smoke(report: Report) -> None:
    from hermes_cli.plugins import get_plugin_command_handler, get_plugin_manager
    from tools.registry import registry

    scope = get_plugin_manager().scope_key

    def tool(**args):
        raw = registry.dispatch("ge_graph", args, scope=scope)
        return json.loads(raw) if isinstance(raw, str) else raw

    analyzed = tool(action="analyze", task=SMOKE_TASK)
    run_id = analyzed.get("run_id")
    report.check("agent:analyze_via_tool", analyzed.get("ok") is True and bool(run_id), run_id)
    refused = tool(action="run", run_id=run_id)
    report.check("agent:tool_cannot_skip_plan_approval", refused.get("error") == "GATE_FAILED", refused.get("error"))
    _json_command(get_plugin_command_handler, "ge-approve", "%s plan" % run_id)
    state = tool(action="run", run_id=run_id)
    submissions = 0
    while state.get("work_orders"):
        order = state["work_orders"][0]
        outputs = {"result": "done: %s" % order["purpose"]}
        if "passed" in order["outputs"]:
            outputs["passed"] = True
        state = tool(action="submit", run_id=run_id, node=order["node"], outputs=outputs)
        submissions += 1
        if submissions > 10:
            break
    report.check("agent:work_orders_submitted", submissions == 4, submissions)
    report.check("agent:run_succeeded", state.get("status", {}).get("status") == "SUCCEEDED",
                 state.get("status", {}).get("status"))
    verified = tool(action="verify", run_id=run_id)
    report.check("agent:run_verified", verified.get("receipt", {}).get("verified") is True, run_id)
    report.data["agent_run"] = run_id


def _agent_node(node_id: str, previous: str | None = None, gated: bool = False) -> dict:
    inputs = {"task": {"from": "graph.inputs.task", "type": "string", "required": True}}
    if previous:
        inputs["previous"] = {"from": "%s.result" % previous, "type": "string", "required": True}
    return {
        "id": node_id, "name": node_id, "purpose": "harmless probe step %s" % node_id,
        "depends_on": [previous] if previous else [], "executor": "agent", "requires": ["agent.reasoning"],
        "operation": None, "instructions": "Report a short note about step %s in 'result'." % node_id,
        "inputs": inputs,
        "outputs": {"result": {"type": "string", "required": True, "description": "short note"}},
        "success_criteria": [{"output": "result", "check": "non_empty"}], "on_failure": "stop",
        "retry": {"max_attempts": 1}, "idempotent": False, "side_effects": False,
        "gates": [{"id": "approval", "type": "approval", "owner": "operator", "description": "probe gate"}] if gated else [],
        "owner": "agent", "rollback_boundary": None,
    }


def _agent_graph(graph_id: str, *nodes: dict) -> str:
    return json.dumps({"schema_version": 1, "graph_id": graph_id, "title": graph_id, "description": "probe",
                       "inputs": {"task": "probe"}, "nodes": list(nodes)})


def autonomous_smoke(report: Report) -> None:
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_cli.plugins import get_plugin_command_handler, get_plugin_manager
    from tools import delegate_tool
    from tools.registry import registry

    scope = get_plugin_manager().scope_key

    def tool(**args):
        raw = registry.dispatch("ge_graph", args, scope=scope)
        return json.loads(raw) if isinstance(raw, str) else raw

    def command(name: str, args: str) -> dict:
        return _json_command(get_plugin_command_handler, name, args)

    def create(spec: str) -> str:
        return command("ge-create", spec)["run_id"]

    helped = command("ge", "")
    auto = helped.get("autonomous_agent_execution") or {}
    report.data["autonomous_capability"] = auto
    report.check("autonomous:enabled_in_config", auto.get("enabled") is True,
                 "set plugins.entries.%s.settings.autonomous_agent_execution: true in the probe home" % PLUGIN)
    report.check("autonomous:host_capability_detected", auto.get("host_supported") is True, auto)

    # 1. no agent turn: explicit fallback, manual mode still works
    manual = create(_agent_graph("probe-manual", _agent_node("m1")))
    command("ge-approve", "%s plan" % manual)
    outside = tool(action="run", run_id=manual)
    report.check("autonomous:unavailable_outside_agent_turn_is_explicit",
                 outside.get("autonomous", {}).get("error") == "HOST_EXECUTION_UNAVAILABLE"
                 and outside["status"]["holds"][0]["kind"] == "WAITING_FOR_SUBMISSION", outside.get("autonomous"))
    done = tool(action="submit", run_id=manual, node="m1", outputs={"result": "by hand"})
    report.check("autonomous:manual_submit_still_works", done["status"]["status"] == "SUCCEEDED",
                 done["status"]["status"])

    # 2. inside a bound agent turn, with only the worker's model turn stood in
    built: list[dict] = []
    guard_results: list[dict] = []
    current: dict[str, str] = {}

    class StandInChild:
        provider = None
        model = None
        _delegate_role = "leaf"
        _delegate_depth = 1

        def __init__(self, ident: str) -> None:
            self._subagent_id = ident

        def interrupt(self, _reason) -> None: ...

        def hard_interrupt(self, _reason, *, tool_reason=None) -> None: ...

    def build(**kwargs):
        built.append(kwargs)
        return StandInChild("ge-probe-%d" % len(built))

    def run_child(_index, goal, _child, _parent):
        node_id = re.search(r"^Node: (\S+)", goal, re.M).group(1)
        # the worker tries to start or drive graphs; the host's own thread pool must carry the guard
        guard_results.append({
            "run": tool(action="run", run_id=current["run"]).get("error"),
            "analyze": tool(action="analyze", task="1. recurse").get("error"),
            "submit": tool(action="submit", run_id=current["run"], node=node_id, outputs={"result": "forged"}).get("error"),
        })
        reply = "Finished.\n```json\n%s\n```" % json.dumps({"result": "probe-output-" + node_id})
        return {"status": "completed", "summary": reply, "api_calls": 1, "duration_seconds": 0.01}

    parent = types.SimpleNamespace(session_id="ge-probe-parent", enabled_toolsets=None)
    saved = (delegate_tool._build_child_preserving_parent_tools, delegate_tool._run_child_lifecycle)
    delegate_tool._build_child_preserving_parent_tools, delegate_tool._run_child_lifecycle = build, run_child
    try:
        gated_plan = create(_agent_graph("probe-draft", _agent_node("d1")))
        with bind_subagent_parent(parent):
            refused = tool(action="run", run_id=gated_plan)
        report.check("autonomous:plan_gate_preserved", refused.get("error") == "GATE_FAILED" and not built,
                     refused.get("error"))

        chain = create(_agent_graph("probe-chain", _agent_node("n1"), _agent_node("n2", "n1")))
        command("ge-approve", "%s plan" % chain)
        current["run"] = chain
        with bind_subagent_parent(parent):
            result = tool(action="run", run_id=chain)
        report.check("autonomous:executed_in_agent_turn", result["status"]["status"] == "SUCCEEDED",
                     result.get("autonomous") or result)
        report.check("autonomous:sequential_dispatch",
                     [d["node"] for d in result.get("autonomous", {}).get("dispatched", [])] == ["n1", "n2"],
                     result.get("autonomous", {}).get("dispatched"))
        report.check("autonomous:launch_carries_no_routing_choice",
                     len(built) == 2 and all(k.get("model") is None and not k.get("toolsets") for k in built),
                     [{"model": k.get("model"), "toolsets": k.get("toolsets")} for k in built])
        blocked = {"WORKER_CONTEXT_RESTRICTED", "DISPATCH_IN_PROGRESS"}
        report.check("autonomous:worker_cannot_touch_the_dispatched_run",
                     len(guard_results) == 2 and all({g["run"], g["submit"]} <= blocked for g in guard_results),
                     guard_results)
        # needs the host to copy the caller's context into worker threads (Hermes 0.21.1 and newer)
        report.check("autonomous:worker_cannot_create_graphs",
                     len(guard_results) == 2 and all(g["analyze"] == "WORKER_CONTEXT_RESTRICTED" for g in guard_results),
                     guard_results)
        receipt = tool(action="verify", run_id=chain)["receipt"]
        report.check("autonomous:run_verified", receipt["verified"] is True,
                     [c["check"] for c in receipt["checks"] if not c["passed"]] or "all checks passed")
        report.data["autonomous_run"] = chain

        gated = create(_agent_graph("probe-gate", _agent_node("g1"), _agent_node("g2", "g1", gated=True)))
        command("ge-approve", "%s plan" % gated)
        current["run"] = gated
        with bind_subagent_parent(parent):
            first = tool(action="run", run_id=gated)
        report.check("autonomous:approval_gate_stops_dispatch",
                     [d["node"] for d in first["autonomous"]["dispatched"]] == ["g1"]
                     and first["status"]["holds"][0]["kind"] == "WAITING_FOR_APPROVAL", first["status"]["holds"])
        command("ge-approve", "%s g2:approval" % gated)
        with bind_subagent_parent(parent):
            second = tool(action="run", run_id=gated)
        report.check("autonomous:continues_after_operator_approval", second["status"]["status"] == "SUCCEEDED",
                     second["status"]["status"])
    finally:
        delegate_tool._build_child_preserving_parent_tools, delegate_tool._run_child_lifecycle = saved
    report.data["live_model_execution"] = "NOT_RUN: the worker's model turn was replaced by a deterministic stand-in"


def expect_run(report: Report, run_id: str) -> None:
    from hermes_cli.plugins import get_plugin_command_handler

    status = _json_command(get_plugin_command_handler, "ge-status", run_id)
    report.check("restart:run_state_survived", status.get("status", {}).get("status") == "SUCCEEDED",
                 status.get("status", {}).get("status") or status.get("error"))
    receipt = _json_command(get_plugin_command_handler, "ge-verify", run_id)
    report.check("restart:run_still_verified", receipt.get("verified") is True, run_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--agent-smoke", action="store_true")
    parser.add_argument("--autonomous-smoke", action="store_true")
    parser.add_argument("--expect-run")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    os.environ["HERMES_HOME"] = os.path.abspath(args.home)

    report = Report()
    report.data["pid"] = os.getpid()
    try:
        import hermes_cli

        report.data["hermes"] = {"version": getattr(hermes_cli, "__version__", None),
                                 "package": os.path.dirname(os.path.abspath(hermes_cli.__file__))}
    except Exception as exc:  # the probe reports it; discovery below fails on its own if Hermes is unusable
        report.data["hermes"] = {"error": type(exc).__name__}
    try:
        check_registration(report, "discovery")
        if args.smoke:
            smoke(report)
        if args.agent_smoke:
            agent_smoke(report)
        if args.autonomous_smoke:
            autonomous_smoke(report)
        if args.expect_run:
            expect_run(report, args.expect_run)
        if args.reload:
            from hermes_cli.plugins import discover_plugins

            discover_plugins(force=True)
            check_registration(report, "reload")
    except Exception as exc:  # report, never hide
        report.check("probe:exception", False, "%s: %s" % (type(exc).__name__, exc))
    print(json.dumps({"ok": report.ok, "checks": report.checks, "data": report.data}, indent=2, default=str))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
