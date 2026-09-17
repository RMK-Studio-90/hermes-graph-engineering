"""Installer: install, reinstall, verify, uninstall and rollback in isolated Hermes homes."""
import json
import shutil
import sys

import pytest

import install_plugin
from install_plugin import InstallError, Layout, check_tree, install, rollback, uninstall

PY = sys.executable


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "hermes-home"
    (root / "plugins" / "unrelated-plugin").mkdir(parents=True)
    (root / "plugins" / "unrelated-plugin" / "plugin.yaml").write_text("name: unrelated-plugin\n", encoding="utf-8")
    return root


@pytest.fixture
def layout(home):
    return Layout(home)


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def test_install_layout_and_verification(layout, home):
    result = install(layout, PY)
    assert result["ok"] and result["action"] == "install" and result["backup"] is None
    target = home / "plugins" / "hermes-graph-engineering"
    files = set(_snapshot(target))
    assert {"plugin.yaml", "__init__.py", "README.md", "LICENSE", "INSTALL_MANIFEST.json",
            "lib/ge_runtime/__init__.py", "lib/ge_runtime/engine.py", "lib/ge_hermes/__init__.py",
            "lib/ge_hermes/plugin.yaml", "lib/ge_hermes/resources/skills/graph-engineering/SKILL.md"} <= files
    top_level = {name.split("/")[0] for name in files}
    assert top_level == {"plugin.yaml", "__init__.py", "README.md", "LICENSE", "INSTALL_MANIFEST.json", "lib"}
    for forbidden in (".private", "tests", "scripts", ".venv311", ".venv314", "build", "dist", "egg-info"):
        assert not [name for name in files if forbidden in name], forbidden
    manifest = json.loads((target / "INSTALL_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "hermes-graph-engineering"
    assert set(manifest["files"]) == files - {"INSTALL_MANIFEST.json"}
    assert not [p for p in manifest["files"] if ":" in p or p.startswith("/")]  # relative paths only
    verification = result["verification"]
    assert verification["old_path_references"] == 0 and verification["sanitizer_hits"] == 0
    assert verification["import_version"] == manifest["version"]
    assert (home / "plugins" / "unrelated-plugin" / "plugin.yaml").is_file()
    assert not list((home / "plugins").glob("*staging*"))


def test_reinstall_is_deterministic_and_keeps_backup(layout, home):
    install(layout, PY)
    target = home / "plugins" / "hermes-graph-engineering"
    first = _snapshot(target)
    result = install(layout, PY, require_existing=True)
    assert result["action"] == "reinstall" and result["backup"]
    assert _snapshot(target) == first  # byte-identical
    backups = list(layout.backups.iterdir())
    assert len(backups) == 1 and _snapshot(backups[0]) == first
    assert sorted(p.name for p in (home / "plugins").iterdir()) == ["hermes-graph-engineering", "unrelated-plugin"]


def test_reinstall_requires_existing_installation(layout):
    with pytest.raises(InstallError):
        install(layout, PY, require_existing=True)


def test_verify_detects_tampering_and_extra_files(layout, home):
    install(layout, PY)
    target = home / "plugins" / "hermes-graph-engineering"
    (target / "lib" / "ge_runtime" / "engine.py").write_text("# tampered\n", encoding="utf-8")
    (target / "lib" / "notes.md").write_text("extra\n", encoding="utf-8")
    report = check_tree(target, PY)
    assert not report["ok"]
    assert "hash mismatch lib/ge_runtime/engine.py" in report["problems"]
    assert "unexpected file lib/notes.md" in report["problems"]


def test_verify_detects_old_path_reference(layout, home):
    install(layout, PY)
    target = home / "plugins" / "hermes-graph-engineering"
    manifest_path = target / "INSTALL_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    readme = target / "README.md"
    readme.write_text("see graph-engineering-" + "core\n", encoding="utf-8")
    manifest["files"]["README.md"] = install_plugin._sha256(readme)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = check_tree(target, PY)
    assert report["old_path_references"] == 1 and not report["ok"]


def test_uninstall_removes_only_this_plugin(layout, home):
    install(layout, PY)
    result = uninstall(layout)
    assert result["removed"] and result["backup"]
    assert not (home / "plugins" / "hermes-graph-engineering").exists()
    assert (home / "plugins").is_dir()
    assert (home / "plugins" / "unrelated-plugin" / "plugin.yaml").is_file()
    assert uninstall(layout) == {"action": "uninstall", "ok": True, "removed": False, "detail": "not installed"}


def test_uninstall_purge_and_rollback(layout, home):
    install(layout, PY)
    install(layout, PY)
    restored = rollback(layout)
    assert restored["ok"] and (home / "plugins" / "hermes-graph-engineering" / "plugin.yaml").is_file()
    uninstall(layout, purge=True)
    assert not (home / "plugins" / "hermes-graph-engineering").exists()
    assert (home / "plugins" / "unrelated-plugin").is_dir()


def test_refuses_foreign_directory_at_target(layout, home):
    foreign = home / "plugins" / "hermes-graph-engineering"
    foreign.mkdir(parents=True)
    (foreign / "plugin.yaml").write_text("name: something-else\n", encoding="utf-8")
    with pytest.raises(InstallError, match="not a hermes-graph-engineering installation"):
        install(layout, PY)
    with pytest.raises(InstallError):
        uninstall(layout)
    assert (foreign / "plugin.yaml").read_text(encoding="utf-8") == "name: something-else\n"


def test_refuses_target_without_install_manifest(layout, home):
    target = home / "plugins" / "hermes-graph-engineering"
    target.mkdir(parents=True)
    (target / "plugin.yaml").write_text("name: hermes-graph-engineering\n", encoding="utf-8")
    with pytest.raises(InstallError, match="no install manifest"):
        uninstall(layout)
    assert target.is_dir()


def test_refuses_overlapping_source_and_target(tmp_path):
    fake_home = install_plugin.PROJECT_ROOT
    layout = Layout(fake_home)
    layout.target = install_plugin.PROJECT_ROOT
    with pytest.raises(InstallError):
        layout.check()


def test_refuses_missing_home(tmp_path):
    with pytest.raises(InstallError):
        install_plugin.resolve_home(str(tmp_path / "missing"), install_plugin.PROJECT_ROOT)


def test_private_material_never_installed(tmp_path, home):
    """Install from a source copy that contains local-only material and prove none of it is installed."""
    source = tmp_path / "source"
    shutil.copytree(install_plugin.PROJECT_ROOT, source, ignore=shutil.ignore_patterns(
        ".venv*", "__pycache__", ".pytest_cache", "build", "dist", "*.egg-info", ".private", ".git"))
    canary = "canary-" + "local-only-7c1e"
    for rel in (".private/notes.md", "scratch/work.md", "tests/fixture_secret.md", ".venv/lib/x.py", "runs/r1/state.json"):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text(canary + "\n", encoding="utf-8")
    install(Layout(home, source=source), PY)
    installed = _snapshot(home / "plugins" / "hermes-graph-engineering")
    assert not [name for name, data in installed.items() if canary.encode() in data]
    assert not [name for name in installed if name.split("/")[0] in {".private", "scratch", "tests", ".venv", "runs"}]


def test_failed_staging_verification_leaves_existing_install_untouched(layout, home, monkeypatch):
    install(layout, PY)
    target = home / "plugins" / "hermes-graph-engineering"
    before = _snapshot(target)
    monkeypatch.setattr(install_plugin, "check_tree", lambda root, python: {"ok": False, "problems": ["forced"]})
    with pytest.raises(InstallError, match="forced"):
        install(layout, PY)
    assert _snapshot(target) == before
    assert not [p for p in layout.work.iterdir() if p.name.startswith("staging-")]


def test_cli_json_output(home, capsys):
    code = install_plugin.main(["install", "--hermes-home", str(home), "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["ok"] is True
    code = install_plugin.main(["verify", "--hermes-home", str(home), "--json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    shutil.rmtree(home / "plugins" / "hermes-graph-engineering" / "lib" / "ge_hermes")
    code = install_plugin.main(["verify", "--hermes-home", str(home), "--json"])
    assert code == 1 and json.loads(capsys.readouterr().out)["ok"] is False
