"""Deterministic installer for the Hermes directory plugin.

    python scripts/install_plugin.py install   [--hermes-home DIR] [--python EXE] [--json]
    python scripts/install_plugin.py reinstall [--hermes-home DIR] [--python EXE] [--json]
    python scripts/install_plugin.py verify    [--hermes-home DIR] [--python EXE] [--json]
    python scripts/install_plugin.py uninstall [--hermes-home DIR] [--purge] [--json]
    python scripts/install_plugin.py rollback  [--hermes-home DIR] [--json]

Hermes home resolution: ``--hermes-home``, else ``HERMES_HOME``, else the
directory two levels above this project when it lives in ``<home>/plugin-src/``.

Layout written to ``<home>/plugins/hermes-graph-engineering``::

    plugin.yaml, __init__.py (entry shim), README.md, LICENSE,
    lib/ge_runtime/**, lib/ge_hermes/**, INSTALL_MANIFEST.json

Only an allowlist of runtime files is copied (never tests, scripts, virtual
environments, caches, build output or local-only directories). The build is
staged and verified outside ``plugins/`` (Hermes never scans a half-installed
copy), then swapped in. Existing installations are moved to
``<home>/plugin-install/hermes-graph-engineering/backups/<timestamp>``;
``rollback`` restores the newest backup. The manifest holds only relative
paths and hashes, so identical sources produce byte-identical installations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boundary_check

PLUGIN_NAME = "hermes-graph-engineering"
PACKAGES = ("ge_runtime", "ge_hermes")
MANIFEST_NAME = "INSTALL_MANIFEST.json"
# retired project name, assembled so this file does not match its own check
OLD_PATH_MARKERS = ("graph-engineering-" + "core", "graph_engineering_" + "core")
FORBIDDEN_PARTS = {".private", ".venv", ".venv311", ".venv313", ".venv314", "tests", "scripts", "build", "dist",
                   "__pycache__", ".pytest_cache", "scratch", "runs", "traces", "receipts"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InstallError(Exception):
    pass


# -- paths ---------------------------------------------------------------------------------------
def _is_link_or_junction(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def resolve_home(explicit: str | None, source: Path) -> Path:
    if explicit:
        home = Path(explicit)
    elif os.environ.get("HERMES_HOME"):
        home = Path(os.environ["HERMES_HOME"])
    elif source.parent.name == "plugin-src":
        home = source.parent.parent
    else:
        raise InstallError("cannot determine the Hermes home; pass --hermes-home")
    home = home.resolve()
    if not home.is_dir():
        raise InstallError("Hermes home does not exist: %s" % home)
    return home


class Layout:
    def __init__(self, home: Path, source: Path = PROJECT_ROOT) -> None:
        self.home = home
        self.source = source.resolve()
        self.plugins = home / "plugins"
        self.target = self.plugins / PLUGIN_NAME
        self.work = home / "plugin-install" / PLUGIN_NAME
        self.backups = self.work / "backups"

    def check(self) -> None:
        """Refuse every layout in which a mistake could touch something other than this plugin."""
        if self.target.name != PLUGIN_NAME or self.target.parent.name != "plugins" or self.target.parent.parent != self.home:
            raise InstallError("refusing unexpected target %s" % self.target)
        for path in (self.home, self.plugins, self.target, self.work.parent, self.work):
            if _is_link_or_junction(path):
                raise InstallError("refusing symlinked or junctioned path %s" % path)
        target = self.target.resolve()
        if target == self.source or self.source in target.parents or target in self.source.parents:
            raise InstallError("source and target overlap")
        if self.plugins.exists() and not self.plugins.is_dir():
            raise InstallError("plugins path is not a directory")
        if self.target.exists():
            if not self.target.is_dir():
                raise InstallError("target exists and is not a directory")
            manifest = self.target / "plugin.yaml"
            if not manifest.is_file() or _manifest_name(manifest) != PLUGIN_NAME:
                raise InstallError("target exists but is not a %s installation; refusing to touch it" % PLUGIN_NAME)


def _manifest_name(path: Path) -> str | None:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get("name") if isinstance(data, dict) else None


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# -- source ----------------------------------------------------------------------------------------
def source_files(source: Path) -> dict[str, Path]:
    """Allowlisted runtime files: relative install path -> source path."""
    src = source / "src"
    for required in ("ge_runtime/__init__.py", "ge_hermes/__init__.py", "ge_hermes/plugin.yaml",
                     "ge_hermes/resources/plugin_init.py.tmpl"):
        if not (src / required).is_file():
            raise InstallError("source incomplete: missing src/%s" % required)
    files: dict[str, Path] = {
        "plugin.yaml": src / "ge_hermes" / "plugin.yaml",
        "__init__.py": src / "ge_hermes" / "resources" / "plugin_init.py.tmpl",
        "README.md": source / "README.md",
        "LICENSE": source / "LICENSE",
    }
    for package in PACKAGES:
        for path in sorted((src / package).rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src)
            if any(part in FORBIDDEN_PARTS or part.endswith(".egg-info") for part in rel.parts):
                continue
            if path.suffix in (".pyc", ".pyo"):
                continue
            files[("lib/" + rel.as_posix())] = path
    return files


def verify_source(source: Path) -> dict[str, object]:
    import yaml

    files = source_files(source)
    manifest = yaml.safe_load(files["plugin.yaml"].read_text(encoding="utf-8"))
    version_ns: dict[str, object] = {}
    exec((source / "src" / "ge_runtime" / "version.py").read_text(encoding="utf-8"), version_ns)
    if manifest.get("name") != PLUGIN_NAME:
        raise InstallError("plugin.yaml name is %r" % manifest.get("name"))
    if str(manifest.get("version")) != version_ns["__version__"]:
        raise InstallError("plugin.yaml version %r != package version %r" % (manifest.get("version"), version_ns["__version__"]))
    if manifest.get("kind") != "standalone":
        raise InstallError("plugin.yaml kind must be standalone")
    return {"version": version_ns["__version__"], "file_count": len(files)}


# -- checks on an installed tree -------------------------------------------------------------------
def _tree_files(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root)
            if "__pycache__" in rel.parts:
                continue  # Hermes/Python may create caches after install
            found.append(rel.as_posix())
    return found


def check_tree(root: Path, python: str) -> dict[str, object]:
    """Verify an installed (or staged) tree. Returns a report; ``ok`` is the verdict."""
    problems: list[str] = []
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest["files"]
    except Exception:
        return {"ok": False, "problems": ["install manifest missing or unreadable"]}
    present = set(_tree_files(root)) - {MANIFEST_NAME}
    missing = sorted(set(expected) - present)
    extra = sorted(present - set(expected))
    problems += ["missing file %s" % name for name in missing]
    problems += ["unexpected file %s" % name for name in extra]
    for rel, digest in expected.items():
        path = root / rel
        if path.is_file() and _sha256(path) != digest:
            problems.append("hash mismatch %s" % rel)
    for rel in present:
        if any(part in FORBIDDEN_PARTS for part in Path(rel).parts[:-1]):
            problems.append("forbidden path component in %s" % rel)
    if _manifest_name(root / "plugin.yaml") != PLUGIN_NAME:
        problems.append("plugin.yaml does not declare %s" % PLUGIN_NAME)

    old_refs = []
    for rel in present:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in OLD_PATH_MARKERS):
            old_refs.append(rel)
    problems += ["old project path referenced in %s" % rel for rel in old_refs]

    hits = boundary_check.sanitize(root)
    problems += ["sanitizer %s:%d [%s]" % (hit.path, hit.line, hit.rule) for hit in hits]

    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('ge_install_probe', sys.argv[1], "
        "submodule_search_locations=[sys.argv[2]])\n"
        "module = importlib.util.module_from_spec(spec); sys.modules['ge_install_probe'] = module\n"
        "spec.loader.exec_module(module)\n"
        "import ge_hermes, ge_runtime\n"
        "print(json.dumps({'register': callable(module.register), 'version': ge_runtime.__version__, "
        "'runtime': ge_runtime.__file__, 'binding': ge_hermes.__file__}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    import_report: dict[str, object] = {}
    try:
        proc = subprocess.run([python, "-c", probe, str(root / "__init__.py"), str(root)], cwd=str(root.parent),
                              env=env, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            problems.append("import check failed: %s" % (proc.stderr.strip().splitlines() or ["?"])[-1])
        else:
            import_report = json.loads(proc.stdout.strip().splitlines()[-1])
            lib = (root / "lib").resolve()
            for key in ("runtime", "binding"):
                if lib not in Path(str(import_report[key])).resolve().parents:
                    problems.append("import check resolved %s outside the installed lib" % key)
            if not import_report.get("register"):
                problems.append("entry shim exposes no register()")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        problems.append("import check could not run: %s" % type(exc).__name__)
    return {
        "ok": not problems,
        "problems": problems,
        "files": len(expected),
        "version": manifest.get("version"),
        "old_path_references": len(old_refs),
        "sanitizer_hits": len(hits),
        "import_version": import_report.get("version"),
    }


# -- actions ---------------------------------------------------------------------------------------
def _stage(layout: Layout) -> Path:
    info = verify_source(layout.source)
    staging = layout.work / ("staging-" + _stamp())
    staging.mkdir(parents=True)
    files = {}
    for rel, src in source_files(layout.source).items():
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if _sha256(dest) != _sha256(src):
            raise InstallError("copy verification failed for %s" % rel)
        files[rel] = _sha256(dest)
    manifest = {"name": PLUGIN_NAME, "version": info["version"], "files": dict(sorted(files.items()))}
    (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                                         newline="\n")
    return staging


def _log(layout: Layout, entry: dict[str, object]) -> None:
    layout.work.mkdir(parents=True, exist_ok=True)
    with open(layout.work / "install-log.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(entry, at=_stamp()), sort_keys=True) + "\n")


def install(layout: Layout, python: str, *, require_existing: bool | None = None) -> dict[str, object]:
    layout.check()
    exists = layout.target.exists()
    if require_existing is True and not exists:
        raise InstallError("reinstall requested but no installation exists")
    staging = _stage(layout)
    report = check_tree(staging, python)
    if not report["ok"]:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError("staged installation failed verification: %s" % "; ".join(report["problems"]))
    layout.plugins.mkdir(parents=True, exist_ok=True)
    backup = None
    if exists:
        backup = layout.backups / _stamp()
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(layout.target, backup)
    try:
        os.replace(staging, layout.target)
    except OSError:
        if backup is not None:
            os.replace(backup, layout.target)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    final = check_tree(layout.target, python)
    if not final["ok"]:
        failed = layout.work / ("failed-" + _stamp())
        os.replace(layout.target, failed)
        if backup is not None:
            os.replace(backup, layout.target)
        raise InstallError("installed tree failed verification (rolled back): %s" % "; ".join(final["problems"]))
    result = {
        "action": "reinstall" if exists else "install",
        "ok": True,
        "target": str(layout.target),
        "backup": str(backup) if backup else None,
        "rollback": "install_plugin.py rollback" if backup else "install_plugin.py uninstall",
        "verification": final,
    }
    _log(layout, {"action": result["action"], "version": final["version"], "backup": result["backup"]})
    return result


def uninstall(layout: Layout, *, purge: bool = False) -> dict[str, object]:
    layout.check()
    if not layout.target.exists():
        return {"action": "uninstall", "ok": True, "removed": False, "detail": "not installed"}
    if not (layout.target / MANIFEST_NAME).is_file():
        raise InstallError("target has no install manifest; refusing to remove it")
    if purge:
        shutil.rmtree(layout.target)
        backup = None
    else:
        backup = layout.backups / _stamp()
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(layout.target, backup)
    _log(layout, {"action": "uninstall", "backup": str(backup) if backup else None, "purge": purge})
    return {"action": "uninstall", "ok": True, "removed": True, "backup": str(backup) if backup else None}


def rollback(layout: Layout) -> dict[str, object]:
    layout.check()
    backups = sorted(p for p in layout.backups.iterdir() if p.is_dir()) if layout.backups.is_dir() else []
    if not backups:
        raise InstallError("no backup available")
    latest = backups[-1]
    if _manifest_name(latest / "plugin.yaml") != PLUGIN_NAME:
        raise InstallError("latest backup is not a %s installation" % PLUGIN_NAME)
    displaced = None
    if layout.target.exists():
        displaced = layout.backups / _stamp()
        os.replace(layout.target, displaced)
    os.replace(latest, layout.target)
    _log(layout, {"action": "rollback", "restored": str(latest), "displaced": str(displaced) if displaced else None})
    return {"action": "rollback", "ok": True, "restored": str(latest), "displaced": str(displaced) if displaced else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("install", "reinstall", "verify", "uninstall", "rollback"))
    parser.add_argument("--hermes-home")
    parser.add_argument("--python", default=sys.executable, help="interpreter used for the import check")
    parser.add_argument("--purge", action="store_true", help="uninstall without keeping a backup")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        layout = Layout(resolve_home(args.hermes_home, PROJECT_ROOT))
        if args.action == "install":
            result = install(layout, args.python)
        elif args.action == "reinstall":
            result = install(layout, args.python, require_existing=True)
        elif args.action == "verify":
            layout.check()
            if not layout.target.exists():
                raise InstallError("not installed")
            result = dict(check_tree(layout.target, args.python), action="verify", target=str(layout.target))
        elif args.action == "uninstall":
            result = uninstall(layout, purge=args.purge)
        else:
            result = rollback(layout)
    except InstallError as exc:
        result = {"action": args.action, "ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%s: %s" % (result["action"], "PASS" if result.get("ok") else "FAIL"))
        for key in ("target", "backup", "rollback", "restored", "error"):
            if result.get(key):
                print("  %s: %s" % (key, result[key]))
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else result
        for problem in verification.get("problems", []) if isinstance(verification, dict) else []:
            print("  problem: %s" % problem)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
