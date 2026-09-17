"""CORE_IMPORT_GRAPH_CLEAN: ge_runtime imports only stdlib, itself and approved third-party roots."""
from pathlib import Path

import boundary_check


def test_core_package_exists(src_dir):
    assert (src_dir / "ge_runtime" / "__init__.py").is_file()


def test_core_import_graph_clean(src_dir, policy):
    violations = boundary_check.import_violations(src_dir / "ge_runtime", {"ge_runtime"}, policy)
    assert violations == [], "\n".join("%s:%d %s" % (v.path, v.line, v.module) for v in violations)


def test_core_never_imports_binding_or_optional_areas(src_dir):
    forbidden = {"ge_hermes", "adapters", "presets"}
    refs = boundary_check.iter_imports(src_dir / "ge_runtime")
    assert refs, "scanner found no imports at all; the check would be vacuous"
    assert not [r for r in refs if r.root in forbidden]


def _make_pkg(tmp_path: Path, body: str) -> Path:
    pkg = tmp_path / "ge_runtime"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return pkg


def test_negative_control_binding_import_detected(tmp_path, policy):
    pkg = _make_pkg(tmp_path, "import ge_hermes\n")
    assert [v.module for v in boundary_check.import_violations(pkg, {"ge_runtime"}, policy)] == ["ge_hermes"]


def test_negative_control_host_internal_detected(tmp_path, policy):
    pkg = _make_pkg(tmp_path, "from hermes_cli.plugins import PluginContext\nimport gateway.run\n")
    found = {v.root for v in boundary_check.import_violations(pkg, {"ge_runtime"}, policy)}
    assert found == {"hermes_cli", "gateway"}


def test_negative_control_private_integration_detected(tmp_path, test_policy):
    private_root = "acme_integration"
    pkg = _make_pkg(tmp_path, "import %s\nfrom adapters import x\n" % private_root)
    found = {v.root for v in boundary_check.import_violations(pkg, {"ge_runtime"}, test_policy)}
    assert found == {private_root, "adapters"}


def test_negative_control_unapproved_third_party_detected(tmp_path, policy):
    pkg = _make_pkg(tmp_path, "import pydantic\nimport yaml\nimport json\n")
    found = [v.module for v in boundary_check.import_violations(pkg, {"ge_runtime"}, policy)]
    assert found == ["pydantic"]


def test_negative_control_dynamic_import_detected(tmp_path, policy):
    pkg = _make_pkg(tmp_path, "import importlib\nname = 'x'\nimportlib.import_module(name)\n")
    found = [v.module for v in boundary_check.import_violations(pkg, {"ge_runtime"}, policy)]
    assert found == ["<dynamic-import>"]


def test_relative_imports_resolve_to_own_package(tmp_path):
    pkg = _make_pkg(tmp_path, "from .sub import thing\n")
    (pkg / "sub.py").write_text("from . import other\n", encoding="utf-8")
    modules = {r.module for r in boundary_check.iter_imports(pkg)}
    assert modules == {"ge_runtime.sub", "ge_runtime"}
