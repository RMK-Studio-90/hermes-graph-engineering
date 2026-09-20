"""Discovery, registration, collisions, smoke and restart through the real Hermes plugin loader.

Requires a Hermes installation: set ``GE_HERMES_PYTHON`` to the interpreter that
can import ``hermes_cli``. Each test installs the plugin into a throwaway Hermes
home, so no real Hermes configuration or plugin directory is touched.
"""
import json
import os
import subprocess

import pytest

import install_plugin

HERMES_PYTHON = os.environ.get("GE_HERMES_PYTHON")
PROBE = install_plugin.PROJECT_ROOT / "scripts" / "hermes_probe.py"

pytestmark = pytest.mark.skipif(not HERMES_PYTHON, reason="GE_HERMES_PYTHON not set (no Hermes interpreter)")


def _home(tmp_path, enabled=True, autonomous=False):
    home = tmp_path / "hermes-home"
    home.mkdir()
    config = "plugins:\n  enabled:\n  - hermes-graph-engineering\n" if enabled else "plugins:\n  enabled: []\n"
    if autonomous:
        config += ("  entries:\n    hermes-graph-engineering:\n      settings:\n"
                   "        autonomous_agent_execution: true\n")
    (home / "config.yaml").write_text(config, encoding="utf-8")
    result = install_plugin.install(install_plugin.Layout(home), HERMES_PYTHON)
    assert result["ok"], result
    return home


def _probe(home, *flags):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "HERMES_HOME"}}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([HERMES_PYTHON, str(PROBE), "--home", str(home), *flags], capture_output=True,
                          text=True, encoding="utf-8", timeout=300, env=env, cwd=str(home))
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        pytest.fail("probe produced no JSON: %s" % (proc.stderr[-2000:]))
    return proc.returncode, report


def _failed(report):
    return [c for c in report["checks"] if not c["passed"]]


def test_discovery_registration_collisions_and_live_smoke(tmp_path):
    home = _home(tmp_path)
    code, report = _probe(home, "--smoke", "--agent-smoke", "--reload")
    assert code == 0 and report["ok"], _failed(report)
    names = {c["check"] for c in report["checks"]}
    for required in ("discovery:discovered", "discovery:commands_registered", "discovery:no_builtin_command_collision",
                     "discovery:no_plugin_command_collision", "discovery:tool_registered", "smoke:graph_dependencies",
                     "smoke:graph_validation", "smoke:builtin_run_verified", "agent:run_verified",
                     "reload:commands_registered"):
        assert required in names
    assert report["data"]["discovery_plugin"]["source"] == "user"


def test_autonomous_execution_through_real_hermes(tmp_path):
    """PLUGIN_INTEGRATION_TEST: real loader, real subagent lifecycle service and thread pool.

    Only the worker's model turn is replaced by a deterministic stand-in (see the probe), so this
    needs no model, provider or credentials; it does not prove a live model run. Worker context
    propagation (creating graphs) needs Hermes 0.21.1 or newer; older hosts fail that one check.
    """
    home = _home(tmp_path, autonomous=True)
    code, report = _probe(home, "--autonomous-smoke")
    assert report["data"]["hermes"].get("version"), report["data"]["hermes"]
    assert code == 0 and report["ok"], _failed(report)
    names = {c["check"] for c in report["checks"]}
    for required in ("autonomous:host_capability_detected", "autonomous:unavailable_outside_agent_turn_is_explicit",
                     "autonomous:manual_submit_still_works", "autonomous:plan_gate_preserved",
                     "autonomous:executed_in_agent_turn", "autonomous:sequential_dispatch",
                     "autonomous:launch_carries_no_routing_choice",
                     "autonomous:worker_cannot_touch_the_dispatched_run", "autonomous:worker_cannot_create_graphs",
                     "autonomous:run_verified", "autonomous:approval_gate_stops_dispatch",
                     "autonomous:continues_after_operator_approval"):
        assert required in names, required


def test_survives_restart_in_a_new_process(tmp_path):
    home = _home(tmp_path)
    code, first = _probe(home, "--smoke")
    assert code == 0, _failed(first)
    run_id = first["data"]["builtin_run"]
    code, second = _probe(home, "--expect-run", run_id)
    assert code == 0 and second["ok"], _failed(second)
    assert second["data"]["pid"] != first["data"]["pid"]


def test_not_loaded_unless_enabled(tmp_path):
    home = _home(tmp_path, enabled=False)
    code, report = _probe(home)
    assert code == 1
    by_name = {c["check"]: c for c in report["checks"]}
    assert by_name["discovery:discovered"]["passed"] is True
    assert by_name["discovery:enabled"]["passed"] is False




MULTIPLEX_SCRIPT = r"""
import json, sys
from pathlib import Path
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli.plugins import get_plugin_manager, get_plugin_command_handler
out = []
for home in sys.argv[1:]:
    token = set_hermes_home_override(Path(home))
    try:
        manager = get_plugin_manager()
        manager.discover_and_load()
        row = next(r for r in manager.list_plugins() if r["key"] == "hermes-graph-engineering")
        handler = get_plugin_command_handler("ge-create")
        created = json.loads(handler("--template text-pipeline --json")) if row["enabled"] else {}
        runs = json.loads(get_plugin_command_handler("ge-status")("--json"))["runs"] if row["enabled"] else []
        out.append({"enabled": row["enabled"], "error": row["error"], "created": created.get("run_id"),
                    "visible": [r["run_id"] for r in runs]})
    finally:
        reset_hermes_home_override(token)
print(json.dumps(out))
"""


def test_multiplexed_profiles_share_process_but_not_state(tmp_path):
    """One gateway process serves several profile homes, each with its own plugin copy and state."""
    homes = []
    for name in ("profile_a", "profile_b", "profile_c"):
        sub = tmp_path / name
        sub.mkdir()
        homes.append(_home(sub))
    # profile_c gets a content-different installation, which must fail closed
    changed = homes[2] / "plugins" / "hermes-graph-engineering"
    (changed / "README.md").write_text("different build\n", encoding="utf-8")
    manifest = json.loads((changed / "INSTALL_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"]["README.md"] = install_plugin._sha256(changed / "README.md")
    (changed / "INSTALL_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "HERMES_HOME"}}
    proc = subprocess.run([HERMES_PYTHON, "-c", MULTIPLEX_SCRIPT, *map(str, homes)], capture_output=True, text=True,
                          encoding="utf-8", timeout=300, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr[-2000:]
    a, b, c = json.loads(proc.stdout.strip().splitlines()[-1])
    assert a["enabled"] and b["enabled"] and not a["error"] and not b["error"]
    assert a["created"] and b["created"] and a["created"] != b["created"]
    assert a["visible"] == [a["created"]] and b["visible"] == [b["created"]]  # no cross-profile state
    assert c["enabled"] is False and "PLUGIN_CONFIGURATION_INVALID" in c["error"]
