"""Verify the installed plugin through Hermes' own plugin loader.

Run with the Hermes interpreter (the one that has ``hermes_cli`` importable):

    <hermes-python> scripts/hermes_probe.py --home <hermes-home> [--smoke] [--agent-smoke]
                                            [--expect-run RUN_ID] [--reload]

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
* ``--expect-run``: a run created by an earlier process is still loadable and
  verified (state survives restart)
* ``--reload``: force re-discovery in-process and re-check registration

Prints one JSON report; exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

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
    parser.add_argument("--expect-run")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    os.environ["HERMES_HOME"] = os.path.abspath(args.home)

    report = Report()
    report.data["pid"] = os.getpid()
    try:
        check_registration(report, "discovery")
        if args.smoke:
            smoke(report)
        if args.agent_smoke:
            agent_smoke(report)
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
