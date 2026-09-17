"""Multi-profile loading of the installed entry shim, without a Hermes installation.

Mirrors what a multi-profile Hermes gateway does: one process imports each profile's
plugin directory under its own module name and calls ``register(ctx)`` with that
profile's data directory. Runs in CI; the same scenario against the real Hermes loader
lives in ``tests/hermes/test_real_hermes_loader.py``.
"""
import json
import os
import subprocess
import sys

import install_plugin

SCRIPT = r"""
import importlib.util, json, sys
from pathlib import Path

class Ctx:
    def __init__(self, data_dir):
        self.state = type("State", (), {"data_dir": data_dir})()
        self.commands = {}
    def get_config(self, key, default=None):
        return default
    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler
        return object()
    def register_tool(self, **kwargs):
        return object()
    def register_skill(self, *args, **kwargs):
        return object()

results = []
for index, home in enumerate(sys.argv[1:]):
    home = Path(home)
    root = home / "plugins" / "hermes-graph-engineering"
    name = "profile_plugin_%d" % index
    try:
        spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        ctx = Ctx(home / "plugin-data" / "graph-engineering")
        module.register(ctx)
        created = json.loads(ctx.commands["ge-create"]("--template text-pipeline --json"))
        listed = json.loads(ctx.commands["ge-status"]("--json"))["runs"]
        results.append({"loaded": True, "error": None, "created": created["run_id"],
                        "visible": [r["run_id"] for r in listed], "runtime": sys.modules["ge_runtime"].__file__})
    except ImportError as exc:
        results.append({"loaded": False, "error": str(exc)})
print(json.dumps(results))
"""


def _install(tmp_path, name):
    home = tmp_path / name
    home.mkdir()
    assert install_plugin.install(install_plugin.Layout(home), sys.executable)["ok"]
    return home


def _run(homes, cwd):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    proc = subprocess.run([sys.executable, "-c", SCRIPT, *map(str, homes)], capture_output=True, text=True,
                          timeout=180, env=env, cwd=str(cwd))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_identical_copies_load_in_one_process_with_isolated_state(tmp_path):
    a, b = _install(tmp_path, "profile-a"), _install(tmp_path, "profile-b")
    first, second = _run([a, b], tmp_path)
    assert first["loaded"] and second["loaded"]
    assert first["created"] != second["created"]
    assert first["visible"] == [first["created"]] and second["visible"] == [second["created"]]
    assert (a / "plugin-data" / "graph-engineering" / "runs" / first["created"]).is_dir()
    assert not (a / "plugin-data" / "graph-engineering" / "runs" / second["created"]).exists()
    # the second profile reused the packages already loaded from the first installation
    assert first["runtime"] == second["runtime"]


def test_different_copy_fails_closed(tmp_path):
    a, b = _install(tmp_path, "profile-a"), _install(tmp_path, "profile-b")
    changed = b / "plugins" / "hermes-graph-engineering"
    (changed / "README.md").write_text("different build\n", encoding="utf-8")
    manifest = json.loads((changed / "INSTALL_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"]["README.md"] = install_plugin._sha256(changed / "README.md")
    (changed / "INSTALL_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    first, second = _run([a, b], tmp_path)
    assert first["loaded"] is True
    assert second["loaded"] is False and "PLUGIN_CONFIGURATION_INVALID" in second["error"]


def test_copy_without_manifest_fails_closed(tmp_path):
    a, b = _install(tmp_path, "profile-a"), _install(tmp_path, "profile-b")
    (b / "plugins" / "hermes-graph-engineering" / "INSTALL_MANIFEST.json").unlink()
    first, second = _run([a, b], tmp_path)
    assert first["loaded"] is True and second["loaded"] is False
