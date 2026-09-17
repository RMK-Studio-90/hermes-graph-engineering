"""HERMES_IMPORT_ISOLATED: only ge_hermes may know about Hermes, and it uses PluginContext only."""
import re

import boundary_check

HERMES_ROOTS = {"hermes_cli", "hermes_plugins", "hermes_constants", "agent", "gateway", "tools", "tui_gateway"}


def test_binding_imports_only_stdlib_runtime_and_approved(src_dir, policy):
    violations = boundary_check.import_violations(src_dir / "ge_hermes", {"ge_hermes", "ge_runtime"}, policy)
    assert violations == [], "\n".join("%s:%d %s" % (v.path, v.line, v.module) for v in violations)


def test_no_package_imports_host_internals(src_dir):
    """Hermes internals are not a plugin contract; the binding receives PluginContext via register(ctx)."""
    for package in ("ge_runtime", "ge_hermes"):
        refs = boundary_check.iter_imports(src_dir / package)
        bad = [r for r in refs if r.root in HERMES_ROOTS or r.root.lower().startswith("hermes")]
        assert bad == [], (package, bad)


def test_binding_manifest_present(src_dir):
    manifest = (src_dir / "ge_hermes" / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(r"^name:\s*hermes-graph-engineering\s*$", manifest, re.M)
    assert re.search(r"^kind:\s*standalone\s*$", manifest, re.M)
    assert re.search(r"^license:\s*MIT\s*$", manifest, re.M)


class _RecordingContext:
    """Minimal stand-in for the verified PluginContext registration surface."""

    def __init__(self, data_dir):
        self.calls = []
        self.state = type("State", (), {"data_dir": data_dir})()

    def get_config(self, key, default=None):
        return default

    def __getattr__(self, name):
        if name.startswith("register"):
            return lambda *a, **k: self.calls.append((name, a, k)) or object()
        raise AttributeError(name)


def test_register_only_uses_plugin_context_registrars(tmp_path):
    import ge_hermes

    ctx = _RecordingContext(tmp_path / "data")
    assert ge_hermes.register(ctx) is None
    kinds = [name for name, _a, _k in ctx.calls]
    assert kinds.count("register_command") == len(ge_hermes.COMMANDS)
    assert kinds.count("register_tool") == 1 and kinds.count("register_skill") == 1
    assert set(kinds) == {"register_command", "register_tool", "register_skill"}
    # registration itself must not create state or touch the filesystem
    assert not (tmp_path / "data").exists()


def test_command_names_are_hyphenated_namespaced_and_safe():
    import ge_hermes

    assert len(set(ge_hermes.COMMANDS)) == len(ge_hermes.COMMANDS)
    for name in ge_hermes.COMMANDS:
        assert "_" not in name
        assert re.fullmatch(r"ge(-[a-z]+)*", name)
    # Hermes core owns /graph and /plan; another local plugin family uses graph_* names
    assert not {"graph", "plan", "run", "status"} & set(ge_hermes.COMMANDS)
    assert not [name for name in ge_hermes.COMMANDS if name.startswith("graph")]


def test_binding_disclaimer():
    import ge_hermes

    assert "not affiliated with, endorsed by, or sponsored by Nous Research" in ge_hermes.DISCLAIMER
