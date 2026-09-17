"""PRIVATE_ARTIFACTS_PACKAGE_EXCLUDED: local-only material never reaches a public artifact.

The wheel and sdist are built offline (``--no-isolation``) from a scratch copy
of the project that contains the local-only directory plus a canary file, so
the project tree itself is never modified by the build.
"""
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

PRIVATE_DIR = ".private"
CANARY = "canary-" + "local-only-evidence-7f3a"
COPY_IGNORE = shutil.ignore_patterns(
    ".venv*", "__pycache__", ".pytest_cache", "*.pyc", "build", "dist", "*.egg-info")


def test_private_dir_is_git_ignored(repo_root):
    lines = [line.strip() for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert PRIVATE_DIR + "/" in lines


def test_private_dir_pruned_from_sdist_manifest(repo_root):
    lines = [line.strip() for line in (repo_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()]
    assert "prune " + PRIVATE_DIR in lines


def test_local_private_dir_is_never_tracked_content(repo_root):
    """A local-only directory may exist in a developer checkout; it must never be part of the public tree."""
    private = repo_root / PRIVATE_DIR
    if not private.is_dir():
        pytest.skip("no local-only directory in this checkout (expected in a fresh clone)")
    tracked = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert PRIVATE_DIR + "/" in tracked


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory, repo_root):
    work = tmp_path_factory.mktemp("pkg")
    project = work / "project"
    shutil.copytree(repo_root, project, ignore=COPY_IGNORE)
    private = project / PRIVATE_DIR
    private.mkdir(exist_ok=True)
    (private / "canary.md").write_text(CANARY + "\n", encoding="utf-8")
    outdir = work / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel",
         "--outdir", str(outdir), str(project)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1
    return wheels[0], sdists[0]


def _wheel_members(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _sdist_members(sdist: Path) -> dict[str, bytes]:
    members = {}
    with tarfile.open(sdist, "r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile():
                members[member.name] = tf.extractfile(member).read()
    return members


def _assert_no_private_material(members: dict[str, bytes], policy: dict) -> None:
    names = list(members)
    assert not [n for n in names if PRIVATE_DIR in n.split("/")], names
    rules = [re.compile(r["regex"]) for r in policy["strict_rules"] + policy["always_rules"]]
    exempt = tuple(policy["exempt_files"])
    for name, data in members.items():
        text = data.decode("utf-8", errors="replace")
        assert CANARY not in text, name
        if name.endswith(exempt):
            continue
        for line in [name] + text.splitlines():
            hits = [r.pattern for r in rules if r.search(line)]
            assert not hits, (name, line[:120], hits)


def test_wheel_excludes_private_material(artifacts, policy):
    wheel, _ = artifacts
    members = _wheel_members(wheel)
    assert "ge_runtime/__init__.py" in members
    assert "ge_runtime/py.typed" in members
    assert "ge_hermes/plugin.yaml" in members
    assert not [n for n in members if n.startswith(("tests/", "scripts/"))]
    _assert_no_private_material(members, policy)


def test_sdist_excludes_private_material(artifacts, policy):
    _, sdist = artifacts
    members = _sdist_members(sdist)
    assert any(n.endswith("/src/ge_runtime/__init__.py") for n in members)
    assert any(n.endswith("/PKG-INFO") for n in members)
    _assert_no_private_material(members, policy)


def test_package_metadata_is_public_clean(artifacts):
    wheel, sdist = artifacts
    metadata = next(data for name, data in _wheel_members(wheel).items()
                    if name.endswith(".dist-info/METADATA")).decode("utf-8")
    pkg_info = next(data for name, data in _sdist_members(sdist).items()
                    if name.count("/") == 1 and name.endswith("/PKG-INFO")).decode("utf-8")
    for text in (metadata, pkg_info):
        assert "Name: hermes-graph-engineering" in text
        assert "License-Expression: MIT" in text
        assert "not affiliated with, endorsed by, or sponsored by Nous Research" in text


RUNTIME_MODULES = ("analyze", "contracts", "engine", "errors", "executors", "report", "spec", "store")
BINDING_FILES = ("commands.py", "config.py", "service.py", "templates.py", "tool.py", "plugin.yaml",
                 "resources/plugin_init.py.tmpl", "resources/skills/graph-engineering/SKILL.md")


def test_wheel_contains_complete_runtime_and_binding(artifacts):
    wheel, _ = artifacts
    members = _wheel_members(wheel)
    for module in RUNTIME_MODULES:
        assert "ge_runtime/%s.py" % module in members, module
    for name in BINDING_FILES:
        assert "ge_hermes/" + name in members, name
    assert not [n for n in members if "install_plugin" in n or "hermes_probe" in n]


def test_sdist_contains_binding_resources(artifacts):
    _, sdist = artifacts
    members = _sdist_members(sdist)
    for name in BINDING_FILES:
        assert any(n.endswith("/src/ge_hermes/" + name) for n in members), name
    assert not [n for n in members if "/.venv" in n or "/build/" in n or "/dist/" in n]


def test_no_old_project_path_references_in_artifacts(artifacts):
    marker = "graph-engineering-" + "core"
    for members in (_wheel_members(artifacts[0]), _sdist_members(artifacts[1])):
        assert not [name for name, data in members.items()
                    if marker in name or marker.encode() in data or marker.replace("-", "_").encode() in data]
