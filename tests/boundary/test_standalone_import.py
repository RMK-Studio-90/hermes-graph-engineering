"""Runtime imports without Hermes and without any private integration.

Each check copies only the packages under test into an empty directory and
imports every module in a fresh interpreter whose import system refuses host
internals, the binding, optional adapters/presets and private integrations.
Nothing from the repository root (adapters/, presets/, scripts/) is reachable.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

PROBE = textwrap.dedent(
    """
    import importlib, importlib.abc, json, pkgutil, sys

    BLOCKED_ROOTS = set(json.loads(sys.argv[1]))
    BLOCKED_PREFIXES = tuple(json.loads(sys.argv[2]))
    PACKAGES = json.loads(sys.argv[3])
    attempts = []

    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            root = fullname.split(".", 1)[0]
            if root in BLOCKED_ROOTS or root.lower().startswith(BLOCKED_PREFIXES):
                attempts.append(fullname)
                raise ModuleNotFoundError("blocked by standalone probe: " + fullname)
            return None

    sys.meta_path.insert(0, Blocker())
    imported = []
    for name in PACKAGES:
        pkg = importlib.import_module(name)
        imported.append(name)
        for info in pkgutil.walk_packages(pkg.__path__, prefix=name + "."):
            importlib.import_module(info.name)
            imported.append(info.name)
    print(json.dumps({"imported": imported, "blocked_attempts": attempts}))
    """
)


def _run_probe(tmp_path, src_dir, packages, blocked_roots, blocked_prefixes):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    for package in packages:
        shutil.copytree(src_dir / package, isolated / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    assert not (isolated / "adapters").exists() and not (isolated / "presets").exists()
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
    env["PYTHONPATH"] = str(isolated)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(sorted(blocked_roots)),
         json.dumps(list(blocked_prefixes)), json.dumps(packages)],
        cwd=isolated, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _host_block(policy):
    imp = policy["import_policy"]
    return set(imp["forbidden_roots"]) - {"ge_hermes"}, ("hermes",)


def test_runtime_imports_without_hermes(tmp_path, src_dir, policy):
    roots, prefixes = _host_block(policy)
    result = _run_probe(tmp_path, src_dir, ["ge_runtime"], roots | {"ge_hermes"}, prefixes)
    assert "ge_runtime.adapters.defaults" in result["imported"]
    assert result["blocked_attempts"] == []


def test_runtime_imports_without_private_integration(tmp_path, src_dir):
    private = "acme"
    roots = {private + "_integration", "adapters", "presets", "ge_hermes"}
    result = _run_probe(tmp_path, src_dir, ["ge_runtime"], roots, (private,))
    assert "ge_runtime.adapters.protocols" in result["imported"]
    assert result["blocked_attempts"] == []


def test_binding_imports_without_hermes(tmp_path, src_dir, policy):
    roots, prefixes = _host_block(policy)
    result = _run_probe(tmp_path, src_dir, ["ge_runtime", "ge_hermes"], roots, prefixes)
    assert "ge_hermes" in result["imported"]
    assert result["blocked_attempts"] == []


def test_probe_negative_control(tmp_path):
    """The blocker must actually fire, otherwise the tests above prove nothing."""
    fake = tmp_path / "fake_src"
    (fake / "ge_runtime").mkdir(parents=True)
    (fake / "ge_runtime" / "__init__.py").write_text("import hermes_cli\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="blocked by standalone probe: hermes_cli"):
        _run_probe(tmp_path, fake, ["ge_runtime"], set(), ("hermes",))
