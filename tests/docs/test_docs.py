"""Release guarantees: command docs match the implementation, versions agree, examples work, CI is sane."""
import re
import tomllib

import yaml

import ge_hermes
from ge_hermes.commands import CommandSet
from ge_hermes.service import GraphService
from ge_hermes.templates import build_template
from ge_runtime import __version__
from ge_runtime.engine import GraphEngine
from ge_runtime.spec import admit
from ge_runtime.store import RunStore


def _implemented(tmp_path):
    service = GraphService(tmp_path, lambda key, default=None: default)
    return {name: (description, hint) for name, _h, description, hint in CommandSet(service).handlers()}


def test_command_reference_matches_registered_commands(repo_root, tmp_path):
    implemented = _implemented(tmp_path)
    assert tuple(implemented) == ge_hermes.COMMANDS
    text = (repo_root / "docs" / "COMMANDS.md").read_text(encoding="utf-8")
    documented = re.findall(r"^## `/(ge[a-z-]*)`$", text, re.M)
    assert documented == list(ge_hermes.COMMANDS)
    sections = re.split(r"^## ", text, flags=re.M)
    for name, (description, hint) in implemented.items():
        section = next(s for s in sections if s.startswith("`/%s`" % name))
        for field in ("**Purpose:**", "**Syntax:**", "**Arguments:**", "**Example:**", "**State requirements:**"):
            assert field in section, (name, field)
        syntax = re.search(r"\*\*Syntax:\*\* (.+)", section).group(1)
        assert "/" + name in syntax
        for token in re.findall(r"--[a-z]+", hint):
            assert token in section, (name, token)


def test_readme_lists_every_command(repo_root):
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    for name in ge_hermes.COMMANDS:
        assert "`/%s" % name in readme, name


def test_version_is_consistent_everywhere(repo_root):
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((repo_root / "src" / "ge_hermes" / "plugin.yaml").read_text(encoding="utf-8"))
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    latest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M).group(1)
    assert pyproject["project"]["version"] == __version__ == str(manifest["version"]) == latest
    classifiers = pyproject["project"]["classifiers"]
    tested = {"3.11", "3.14"}
    assert {c.rsplit(" ", 1)[-1] for c in classifiers if re.search(r"Python :: 3\.\d+$", c)} == tested


def test_example_spec_equals_template_and_runs(repo_root, tmp_path):
    text = (repo_root / "examples" / "text-pipeline" / "spec.yaml").read_text(encoding="utf-8")
    assert admit(text).digest == admit(build_template("text-pipeline")).digest
    engine = GraphEngine(RunStore(tmp_path))
    run_id = engine.create(text)["run_id"]
    engine.decide(run_id, "plan", approve=True, decided_by="test")
    state = engine.execute(run_id)
    assert state["status"] == "SUCCEEDED"
    assert state["nodes"]["transform_input"]["outputs"] == {"value": "HELLO GRAPH ENGINEERING"}
    assert engine.verify(run_id)["verified"] is True


def test_ci_workflow_structure(repo_root):
    workflow = yaml.safe_load((repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert True in workflow or "on" in workflow  # YAML 1.1 parses the key `on` as True
    job = workflow["jobs"]["test"]
    assert sorted(job["strategy"]["matrix"]["python-version"]) == ["3.11", "3.14"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    for required in ("pip install -e", "pytest", "python -m build", "install_plugin.py install",
                     "install_plugin.py uninstall", "boundary_check.py sanitize"):
        assert required in runs, required
    assert "secrets." not in (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_release_documents_exist(repo_root):
    for name in ("README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md",
                 "docs/COMMANDS.md", "docs/ARCHITECTURE.md", "examples/text-pipeline/README.md"):
        assert (repo_root / name).is_file(), name
    assert (repo_root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
