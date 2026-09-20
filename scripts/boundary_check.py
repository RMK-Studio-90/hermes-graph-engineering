"""Dependency-boundary and public-sanitizer checks.

Used by the boundary test-suite and runnable directly:

    python scripts/boundary_check.py sanitize [ROOT]
    python scripts/boundary_check.py imports [ROOT]

All policy values live in ``scripts/sanitizer_policy.json`` (the single
documented sanitizer exemption). This module itself contains no policy values.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = Path(__file__).resolve().with_name("sanitizer_policy.json")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    excerpt: str


@dataclass(frozen=True)
class ImportRef:
    path: str
    line: int
    module: str

    @property
    def root(self) -> str:
        return self.module.split(".", 1)[0]


def load_policy(policy_file: Path = POLICY_FILE, *, local_overlay: bool = True) -> dict:
    """Load the public policy, merged with the optional local-only overlay.

    The overlay (``local_overlay`` in the policy, relative to the repository
    root) adds deployment-specific markers without publishing them. Lists are
    extended; nothing in the public policy can be removed by the overlay.
    """
    policy = json.loads(Path(policy_file).read_text(encoding="utf-8"))
    overlay_rel = policy.get("local_overlay")
    overlay_path = Path(policy_file).resolve().parents[1] / overlay_rel if overlay_rel else None
    if local_overlay and overlay_path is not None and overlay_path.is_file():
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        for key in ("relaxed_zones", "strict_rules", "always_rules", "excluded_dirs"):
            policy[key] = list(policy.get(key, [])) + list(overlay.get(key, []))
        for key in ("forbidden_roots", "forbidden_prefixes", "allowed_third_party"):
            if key == "allowed_third_party":
                continue  # an overlay may only tighten the import policy
            policy["import_policy"][key] = list(policy["import_policy"][key]) + list(
                overlay.get("import_policy", {}).get(key, []))
    return policy


# -- sanitizer ------------------------------------------------------------------

def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_excluded(rel: str, policy: dict) -> bool:
    for part in rel.split("/")[:-1]:
        if part in policy["excluded_dirs"]:
            return True
        if any(part.endswith(suffix) for suffix in policy["excluded_dir_suffixes"]):
            return True
    return False


def _is_scanned(path: Path, policy: dict) -> bool:
    return any(path.name.endswith(suffix) for suffix in policy["scanned_suffixes"])


def iter_public_files(root: Path, policy: dict):
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = _rel(path, root)
        if _is_excluded(rel, policy) or not _is_scanned(path, policy):
            continue
        yield path, rel


def sanitize(root: Path = REPO_ROOT, policy: dict | None = None) -> list[Hit]:
    """Return every unauthorized hit below ``root`` (file paths and contents)."""
    policy = policy or load_policy()
    root = Path(root)
    strict = [(r["id"], re.compile(r["regex"])) for r in policy["strict_rules"]]
    always = [(r["id"], re.compile(r["regex"])) for r in policy["always_rules"]]
    hits: list[Hit] = []
    for path, rel in iter_public_files(root, policy):
        if rel in policy["exempt_files"]:
            continue
        relaxed = any(rel.startswith(zone) for zone in policy["relaxed_zones"])
        rules = always if relaxed else strict + always
        # line 0 = the relative path itself, which is public as well
        lines = [rel] + path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines):
            for rule_id, pattern in rules:
                if pattern.search(line):
                    hits.append(Hit(rel, number, rule_id, line.strip()[:120]))
    return hits


# -- import graph ---------------------------------------------------------------

def _module_name_for(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def iter_imports(package_dir: Path) -> list[ImportRef]:
    """Collect absolute module names imported anywhere below ``package_dir``.

    Relative imports are resolved against the package. Calls to
    ``import_module``/``__import__`` are reported too; non-literal arguments are
    reported as ``<dynamic-import>`` so they cannot bypass the policy silently.
    """
    package_dir = Path(package_dir)
    source_root = package_dir.parent
    refs: list[ImportRef] = []
    for path in sorted(package_dir.rglob("*.py")):
        module = _module_name_for(path, source_root)
        is_pkg = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(source_root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.append(ImportRef(rel, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = module.split(".")
                    if not is_pkg:
                        base = base[:-1]
                    if node.level > 1:
                        base = base[: len(base) - (node.level - 1)]
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                refs.append(ImportRef(rel, node.lineno, target))
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in {"import_module", "__import__"} and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        refs.append(ImportRef(rel, node.lineno, arg.value))
                    else:
                        refs.append(ImportRef(rel, node.lineno, "<dynamic-import>"))
    return refs


def documented_host_api_exemptions(policy: dict) -> set[tuple[str, str]]:
    """(file, module) pairs allowed to import a host module because the host documents it as a plugin API."""
    return {(e["file"], e["module"]) for e in policy["import_policy"].get("documented_host_apis", [])}


def import_violations(package_dir: Path, own_packages: set[str], policy: dict | None = None) -> list[ImportRef]:
    """Return imports below ``package_dir`` that break the dependency policy.

    Allowed roots: stdlib, ``own_packages`` and the approved third-party roots.
    Forbidden roots/prefixes fail even if they would otherwise be allowed,
    except for the package's own names.
    """
    policy = policy or load_policy()
    imp = policy["import_policy"]
    own = set(own_packages)
    forbidden = set(imp["forbidden_roots"]) - own
    prefixes = tuple(imp["forbidden_prefixes"])
    exempt = documented_host_api_exemptions(policy)
    allowed = set(sys.stdlib_module_names) | own | set(imp["allowed_third_party"])
    bad: list[ImportRef] = []
    for ref in iter_imports(package_dir):
        root = ref.root
        if ref.module == "<dynamic-import>":
            bad.append(ref)
        elif root in own or (ref.path, ref.module) in exempt:
            continue
        elif root in forbidden or root.lower().startswith(prefixes) or root not in allowed:
            bad.append(ref)
    return bad


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"sanitize", "imports"}:
        print(__doc__)
        return 2
    root = Path(argv[1]) if len(argv) > 1 else REPO_ROOT
    if argv[0] == "sanitize":
        hits = sanitize(root)
        for hit in hits:
            print("%s:%d [%s] %s" % (hit.path, hit.line, hit.rule, hit.excerpt))
        print("PUBLIC_SANITIZER_HITS = %d" % len(hits))
        return 1 if hits else 0
    policy = load_policy()
    core = import_violations(root / "src" / "ge_runtime", {"ge_runtime"}, policy)
    binding = import_violations(root / "src" / "ge_hermes", {"ge_hermes", "ge_runtime"}, policy)
    for ref in core + binding:
        print("%s:%d imports %s" % (ref.path, ref.line, ref.module))
    print("CORE_IMPORT_VIOLATIONS = %d" % len(core))
    print("BINDING_IMPORT_VIOLATIONS = %d" % len(binding))
    return 1 if core or binding else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
