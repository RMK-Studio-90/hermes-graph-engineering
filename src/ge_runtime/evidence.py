"""Deterministic evidence: what the runtime checks itself instead of trusting an agent.

A node may declare ``evidence`` and a graph may declare ``acceptance``: lists of checks the
runtime executes after the node's outputs passed their contract and criteria (node
evidence) or when every node succeeded (acceptance). An agent saying "tests pass" is never
evidence; an exit code the runtime observed is.

Check types::

    {"type": "command", "argv": ["python", "-m", "pytest", "-q"], "cwd": ".", "expect_exit": 0,
     "timeout_seconds": 300, "stdout_contains": "passed"}          exit code (+ output) of a real process
    {"type": "file_exists", "path": "out/report.md"}                 a file exists (and is non-empty with
                                                                      "non_empty": true)
    {"type": "file_absent", "path": "tmp/lock"}
    {"type": "file_contains", "path": "README.md", "text": "..."}    substring (or "pattern": regex)
    {"type": "file_sha256", "path": "dist/app.bin", "sha256": "<hex>"}  or "from_output": "<output name>"
                                                                      (the agent's claimed hash is checked)
    {"type": "git_diff", "allowed_paths": ["src/*"], "require_changes": true, "max_files": 20}
                                                                      files the attempt changed, relative to a
                                                                      baseline taken right before it started
    {"type": "output_equals_file", "output": "summary", "path": "out/summary.txt"}

Paths are relative to the run's workspace and may not escape it. Every evaluation is stored
as a checksummed artifact (``artifacts/evidence-<node>-<attempt>.json``: exit codes, output
digests and tails, file hashes, changed files) and referenced by digest from the node state,
the journal and the receipt.

Read-only guard: a node the policy classified ``READ_ONLY`` must not change the workspace.
The runtime fingerprints the workspace before the attempt and compares afterwards; any
change fails the node with ``POLICY_VIOLATION`` (never retried, never repaired).
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .errors import ErrorCode, GraphEngineeringError
from .spec import GraphSpec, digest_of

EVIDENCE_TYPES = ("command", "file_exists", "file_absent", "file_contains", "file_sha256", "git_diff",
                  "output_equals_file")
MAX_CHECKS = 20
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 300
TAIL_CHARS = 4000
MAX_FINGERPRINT_FILES = 50_000
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------- admission
def _rel_path_error(value: Any, where: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "%s: path must be a non-empty relative path" % where
    if len(value) > 500:
        return "%s: path too long" % where
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", value) or ".." in pure.parts:
        return "%s: path %r must stay inside the workspace" % (where, value)
    return None


def evidence_errors(checks: Any, where: str, outputs: Mapping[str, Any] | None = None) -> list[str]:
    """Validate an ``evidence``/``acceptance`` list at spec admission."""
    if checks is None:
        return []
    if not isinstance(checks, list):
        return ["%s must be a list" % where]
    if len(checks) > MAX_CHECKS:
        return ["%s: at most %d checks" % (where, MAX_CHECKS)]
    errors: list[str] = []
    allowed = {
        "command": {"argv", "cwd", "expect_exit", "timeout_seconds", "stdout_contains", "description"},
        "file_exists": {"path", "non_empty", "description"},
        "file_absent": {"path", "description"},
        "file_contains": {"path", "text", "pattern", "description"},
        "file_sha256": {"path", "sha256", "from_output", "description"},
        "git_diff": {"allowed_paths", "require_changes", "max_files", "description"},
        "output_equals_file": {"output", "path", "description"},
    }
    for index, check in enumerate(checks):
        cwhere = "%s[%d]" % (where, index)
        if not isinstance(check, Mapping):
            errors.append("%s must be a mapping" % cwhere)
            continue
        kind = check.get("type")
        if kind not in EVIDENCE_TYPES:
            errors.append("%s: unknown evidence type %r (known: %s)" % (cwhere, kind, ", ".join(EVIDENCE_TYPES)))
            continue
        extra = sorted(set(check) - allowed[kind] - {"type"})
        if extra:
            errors.append("%s: unknown field(s) %s" % (cwhere, ", ".join(extra)))
        if "path" in allowed[kind]:
            problem = _rel_path_error(check.get("path"), cwhere)
            if problem:
                errors.append(problem)
        if kind == "command":
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv) \
                    or len(argv) > 64:
                errors.append("%s: argv must be a non-empty list of strings" % cwhere)
            if "cwd" in check:
                problem = _rel_path_error(check.get("cwd"), cwhere)
                if problem and check.get("cwd") != ".":
                    errors.append(problem)
            expect = check.get("expect_exit", 0)
            if not isinstance(expect, int) or isinstance(expect, bool):
                errors.append("%s: expect_exit must be an integer" % cwhere)
            timeout = check.get("timeout_seconds", DEFAULT_TIMEOUT)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
                errors.append("%s: timeout_seconds must be 1..%d" % (cwhere, MAX_TIMEOUT))
            if "stdout_contains" in check and not isinstance(check["stdout_contains"], str):
                errors.append("%s: stdout_contains must be a string" % cwhere)
        elif kind == "file_exists" and not isinstance(check.get("non_empty", False), bool):
            errors.append("%s: non_empty must be a boolean" % cwhere)
        elif kind == "file_contains":
            if ("text" in check) == ("pattern" in check):
                errors.append("%s: exactly one of text or pattern is required" % cwhere)
            elif "text" in check and not (isinstance(check["text"], str) and check["text"]):
                errors.append("%s: text must be a non-empty string" % cwhere)
            elif "pattern" in check:
                try:
                    re.compile(check["pattern"])
                except (re.error, TypeError):
                    errors.append("%s: invalid pattern" % cwhere)
        elif kind == "file_sha256":
            if ("sha256" in check) == ("from_output" in check):
                errors.append("%s: exactly one of sha256 or from_output is required" % cwhere)
            elif "sha256" in check and not (isinstance(check["sha256"], str) and _HEX64.match(check["sha256"])):
                errors.append("%s: sha256 must be 64 lowercase hex characters" % cwhere)
            elif "from_output" in check and outputs is not None and check["from_output"] not in outputs:
                errors.append("%s: from_output references undeclared output %r" % (cwhere, check["from_output"]))
        elif kind == "git_diff":
            paths = check.get("allowed_paths", ["*"])
            if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
                errors.append("%s: allowed_paths must be a non-empty list of glob patterns" % cwhere)
            if not isinstance(check.get("require_changes", False), bool):
                errors.append("%s: require_changes must be a boolean" % cwhere)
            max_files = check.get("max_files", 1000)
            if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 0:
                errors.append("%s: max_files must be a non-negative integer" % cwhere)
        elif kind == "output_equals_file":
            if outputs is not None and check.get("output") not in outputs:
                errors.append("%s: output references undeclared output %r" % (cwhere, check.get("output")))
            if outputs is None:
                errors.append("%s: output_equals_file is only valid on nodes" % cwhere)
    return errors


# ---------------------------------------------------------------------- execution helpers
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(workspace: Path, rel: str) -> Path:
    target = (workspace / rel.replace("\\", "/")).resolve()
    root = workspace.resolve()
    if target != root and root not in target.parents:
        raise GraphEngineeringError(ErrorCode.EVIDENCE_FAILED, "path %r escapes the workspace" % rel)
    return target


def _tail(text: str) -> str:
    return text if len(text) <= TAIL_CHARS else "..." + text[-TAIL_CHARS:]


def workspace_fingerprint(workspace: Path, exclude: Path | None = None) -> dict[str, Any]:
    """Map of relative path -> [size, mtime_ns, sha256] for every regular file (bounded)."""
    files: dict[str, list[Any]] = {}
    root = workspace.resolve()
    excluded = exclude.resolve() if exclude is not None else None
    truncated = False
    for current, dirs, names in os.walk(root):
        cur = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and (excluded is None or (cur / d).resolve() != excluded))
        for name in sorted(names):
            path = cur / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            if len(files) >= MAX_FINGERPRINT_FILES:
                truncated = True
                break
            rel = path.relative_to(root).as_posix()
            digest = _sha256_file(path) if stat.st_size <= (8 << 20) else "size:%d" % stat.st_size
            files[rel] = [stat.st_size, digest]
        if truncated:
            break
    return {"files": files, "truncated": truncated, "digest": digest_of(files)}


def git_changes(workspace: Path) -> dict[str, str] | None:
    """Changed/untracked paths of a git work tree -> content digest ('deleted' for removals)."""
    try:
        proc = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    changes: dict[str, str] = {}
    entries = proc.stdout.decode("utf-8", "replace").split("\0")
    index = 0
    top = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], capture_output=True,
                         timeout=60)
    repo_root = Path(top.stdout.decode().strip()) if top.returncode == 0 else workspace
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1  # the rename source follows
        path = repo_root / rel
        try:
            rel_ws = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            continue
        if path.is_file():
            changes[rel_ws] = _sha256_file(path)
        else:
            changes[rel_ws] = "deleted"
    return changes


# ---------------------------------------------------------------------- runner
class EvidenceRunner:
    """Engine collaborator that executes and verifies deterministic evidence."""

    def __init__(self, store: Any = None, *, exclude: str | os.PathLike[str] | None = None,
                 run_commands: bool = True) -> None:
        self.store = store  # bound by the engine when left unset
        self.exclude = Path(exclude) if exclude else None
        self.run_commands = run_commands

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _workspace(state: Mapping[str, Any]) -> Path | None:
        ws = state.get("workspace")
        if not ws:
            return None
        path = Path(ws)
        return path if path.is_dir() else None

    @staticmethod
    def _risk(state: Mapping[str, Any], node_id: str) -> str | None:
        return (((state.get("policy") or {}).get("decisions") or {}).get(node_id) or {}).get("risk")

    def _needs_git_baseline(self, checks: list[Mapping[str, Any]]) -> bool:
        return any(c.get("type") == "git_diff" for c in checks)

    # -- engine hooks ------------------------------------------------------------
    def before_attempt(self, state: dict[str, Any], spec: GraphSpec, node_id: str) -> None:
        node = spec.node(node_id)
        node_state = state["nodes"][node_id]
        workspace = self._workspace(state)
        baseline: dict[str, Any] = {}
        if workspace is not None:
            if self._needs_git_baseline(list(node.get("evidence") or [])):
                baseline["git"] = git_changes(workspace)
            if self._risk(state, node_id) == "READ_ONLY" and node.get("executor") != "builtin":
                baseline["fingerprint"] = workspace_fingerprint(workspace, self.exclude)
        if baseline:
            name = "baseline-%s-%d" % (node_id, node_state["attempts"])
            ref = self._store().write_artifact(state["run_id"], name, baseline)
            node_state["baseline"] = ref
        else:
            node_state.pop("baseline", None)

    def after_attempt(self, state: dict[str, Any], spec: GraphSpec, node_id: str,
                      outputs: Mapping[str, Any]) -> tuple[ErrorCode, str] | None:
        node = spec.node(node_id)
        node_state = state["nodes"][node_id]
        checks = list(node.get("evidence") or [])
        baseline = self._baseline(state, node_state)
        workspace = self._workspace(state)
        results: list[dict[str, Any]] = []
        violation = None
        if "fingerprint" in baseline and workspace is not None:
            after = workspace_fingerprint(workspace, self.exclude)
            changed = _diff_fingerprints(baseline["fingerprint"], after)
            results.append({"check": "read_only_workspace", "type": "policy_guard", "passed": not changed,
                            "detail": "workspace unchanged" if not changed else
                            "READ_ONLY node changed %d file(s): %s" % (len(changed), ", ".join(changed[:10])),
                            "changed": changed[:200]})
            if changed:
                violation = (ErrorCode.POLICY_VIOLATION,
                             "policy violation: READ_ONLY node %s changed the workspace (%s)" % (
                                 node_id, ", ".join(changed[:10])))
        if not checks and not results:
            node_state["evidence"] = []
            return None
        for index, check in enumerate(checks):
            results.append(self.run_check(check, index, state, workspace, outputs, baseline))
        artifact = "evidence-%s-%d" % (node_id, node_state["attempts"])
        ref = self._store().write_artifact(state["run_id"], artifact, {
            "node": node_id, "attempt": node_state["attempts"], "results": results})
        summary = [{"check": r["check"], "type": r["type"], "passed": r["passed"], "detail": r["detail"],
                    "artifact": ref["artifact"], "digest": ref["digest"]} for r in results]
        node_state["evidence"] = summary
        if violation:
            return violation
        failed = [r for r in results if not r["passed"]]
        if failed:
            return (ErrorCode.EVIDENCE_FAILED, "evidence failed: " + "; ".join(
                "%s: %s" % (r["check"], r["detail"]) for r in failed)[:1500])
        return None

    def acceptance(self, state: dict[str, Any], spec: GraphSpec) -> tuple[ErrorCode, str] | None:
        checks = list(spec.data.get("acceptance") or [])
        if not checks:
            return None
        workspace = self._workspace(state)
        results = [self.run_check(check, index, state, workspace, {}, {}) for index, check in enumerate(checks)]
        ref = self._store().write_artifact(state["run_id"], "acceptance-%d" % (len(state.get("spec_revisions")
                                                                                        or []) + 1),
                                                {"results": results})
        state["evidence"] = {"acceptance": [{"check": r["check"], "type": r["type"], "passed": r["passed"],
                                             "detail": r["detail"], "artifact": ref["artifact"],
                                             "digest": ref["digest"]} for r in results]}
        failed = [r for r in results if not r["passed"]]
        if failed:
            return (ErrorCode.EVIDENCE_FAILED, "acceptance failed: " + "; ".join(
                "%s: %s" % (r["check"], r["detail"]) for r in failed)[:1500])
        return None

    # -- verification ------------------------------------------------------------
    def verify_node(self, state: Mapping[str, Any], spec: GraphSpec, node_id: str) -> list[tuple[str, bool, str]]:
        node_state = state["nodes"][node_id]
        declared = list(spec.node(node_id).get("evidence") or [])
        entries = list(node_state.get("evidence") or [])
        if not declared and not entries:
            return []
        results = []
        ok, detail = self._verify_entries(state, entries)
        if declared and not [e for e in entries if e.get("type") != "policy_guard"]:
            ok, detail = False, "declared evidence was never evaluated"
        results.append(("evidence", ok, detail))
        return results

    def verify_acceptance(self, state: Mapping[str, Any], spec: GraphSpec) -> list[tuple[str, bool, str]]:
        checks = list(spec.data.get("acceptance") or [])
        results: list[tuple[str, bool, str]] = []
        if checks:
            entries = list((state.get("evidence") or {}).get("acceptance") or [])
            ok, detail = self._verify_entries(state, entries)
            results.append(("acceptance:recorded", ok and len(entries) == len(checks), detail))
            workspace = self._workspace(state)
            fresh = [self.run_check(c, i, state, workspace, {}, {}) for i, c in enumerate(checks)]
            failed = [r for r in fresh if not r["passed"]]
            results.append(("acceptance:re-checked", not failed,
                            "; ".join("%s: %s" % (r["check"], r["detail"]) for r in failed)
                            or "%d acceptance check(s) re-evaluated now" % len(fresh)))
        if spec.data.get("require_evidence"):
            level = self.verification_level(state, spec)
            results.append(("evidence_coverage", level != "SELF_REPORTED",
                            "verification level %s" % level))
        return results

    def _verify_entries(self, state: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> tuple[bool, str]:
        store = self._store()
        seen: dict[str, Any] = {}
        for entry in entries:
            name = entry.get("artifact")
            if name not in seen:
                try:
                    record = store.read_artifact_record(state["run_id"], name)
                except GraphEngineeringError as exc:
                    return False, "evidence artifact %s: %s" % (name, exc.message)
                if record.get("checksum") != entry.get("digest"):
                    return False, "evidence artifact %s does not match its recorded digest" % name
                seen[name] = record["payload"]
            recorded = {r["check"]: r for r in seen[name].get("results", [])}
            match = recorded.get(entry.get("check"))
            if match is None or match.get("passed") is not True:
                return False, "evidence %s did not pass (%s)" % (entry.get("check"),
                                                                  (match or {}).get("detail", "missing"))
        return True, "%d evidence check(s) passed; artifacts verified" % len(entries)

    def verification_level(self, state: Mapping[str, Any], spec: GraphSpec) -> str:
        """DETERMINISTIC (no agent work), EVIDENCE (every agent node is covered by passing evidence of its
        own or of a downstream node / the acceptance checks) or SELF_REPORTED."""
        agent_nodes = [n["id"] for n in spec.nodes if n["executor"] != "builtin"]
        if not agent_nodes:
            return "DETERMINISTIC"
        acceptance = (state.get("evidence") or {}).get("acceptance") or []
        if spec.data.get("acceptance") and acceptance and all(e.get("passed") for e in acceptance):
            return "EVIDENCE"
        proven = {n["id"] for n in spec.nodes
                  if [e for e in state["nodes"][n["id"]].get("evidence") or [] if e.get("type") != "policy_guard"]
                  and all(e.get("passed") for e in state["nodes"][n["id"]]["evidence"])}
        for node_id in agent_nodes:
            if node_id in proven:
                continue
            downstream = _descendants(spec, node_id)
            if not downstream & proven:
                return "SELF_REPORTED"
        return "EVIDENCE"

    # -- checks ------------------------------------------------------------------
    def run_check(self, check: Mapping[str, Any], index: int, state: Mapping[str, Any], workspace: Path | None,
                  outputs: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
        kind = check.get("type")
        name = "%s[%d]" % (kind, index)
        result: dict[str, Any] = {"check": name, "type": kind, "passed": False, "detail": "", "spec": dict(check)}
        if workspace is None:
            result["detail"] = "the run has no workspace; evidence cannot be checked"
            return result
        try:
            getattr(self, "_check_" + str(kind))(check, workspace, outputs, baseline, result)
        except GraphEngineeringError as exc:
            result["passed"], result["detail"] = False, exc.message
        except Exception as exc:  # a checker bug fails the check, never the run
            result["passed"], result["detail"] = False, "checker raised %s" % type(exc).__name__
        return result

    def _check_command(self, check, workspace, outputs, baseline, result) -> None:
        if not self.run_commands:
            result["detail"] = "command evidence is disabled by configuration"
            return
        cwd = _resolve(workspace, check.get("cwd") or ".")
        timeout = int(check.get("timeout_seconds", DEFAULT_TIMEOUT))
        started = time.monotonic()
        try:
            proc = subprocess.run(list(check["argv"]), cwd=str(cwd), capture_output=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
            code: int | None = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            code, stdout, stderr, timed_out = None, exc.stdout or b"", exc.stderr or b"", True
        except OSError as exc:
            result["detail"] = "command could not start (%s)" % type(exc).__name__
            result["exit_code"] = None
            return
        out, err = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        expect = int(check.get("expect_exit", 0))
        passed = code == expect and not timed_out
        if passed and check.get("stdout_contains") and check["stdout_contains"] not in out:
            passed = False
        result.update(passed=passed, exit_code=code, timed_out=timed_out,
                      duration_seconds=round(time.monotonic() - started, 3),
                      stdout_sha256=_sha256_bytes(stdout), stderr_sha256=_sha256_bytes(stderr),
                      stdout_tail=_tail(out), stderr_tail=_tail(err))
        if timed_out:
            result["detail"] = "timed out after %ds" % timeout
        elif code != expect:
            result["detail"] = "exit code %s, expected %s; stderr: %s" % (code, expect, " ".join(err.split())[-300:]
                                                                         or "-")
        elif not passed:
            result["detail"] = "stdout does not contain %r" % check["stdout_contains"]
        else:
            result["detail"] = "exit code %s as expected" % code

    def _check_file_exists(self, check, workspace, outputs, baseline, result) -> None:
        path = _resolve(workspace, check["path"])
        if not path.is_file():
            result["detail"] = "%s does not exist" % check["path"]
            return
        size = path.stat().st_size
        result["sha256"], result["size"] = _sha256_file(path), size
        if check.get("non_empty") and size == 0:
            result["detail"] = "%s is empty" % check["path"]
            return
        result["passed"], result["detail"] = True, "%s exists (%d bytes)" % (check["path"], size)

    def _check_file_absent(self, check, workspace, outputs, baseline, result) -> None:
        path = _resolve(workspace, check["path"])
        result["passed"] = not path.exists()
        result["detail"] = "%s is absent" % check["path"] if result["passed"] else "%s exists" % check["path"]

    def _check_file_contains(self, check, workspace, outputs, baseline, result) -> None:
        path = _resolve(workspace, check["path"])
        if not path.is_file():
            result["detail"] = "%s does not exist" % check["path"]
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        result["sha256"] = _sha256_file(path)
        if "text" in check:
            passed = check["text"] in text
        else:
            passed = re.search(check["pattern"], text, re.M) is not None
        result["passed"] = passed
        result["detail"] = "%s %s the expected content" % (check["path"], "contains" if passed else "lacks")

    def _check_file_sha256(self, check, workspace, outputs, baseline, result) -> None:
        path = _resolve(workspace, check["path"])
        if not path.is_file():
            result["detail"] = "%s does not exist" % check["path"]
            return
        actual = _sha256_file(path)
        expected = check.get("sha256") if "sha256" in check else outputs.get(check["from_output"])
        if isinstance(expected, str) and expected.startswith("sha256:"):
            expected = expected[len("sha256:"):]
        result.update(sha256=actual, expected=expected, passed=actual == expected)
        result["detail"] = "sha256 matches" if actual == expected else "sha256 %s != expected %s" % (
            actual[:16], str(expected)[:16])

    def _check_output_equals_file(self, check, workspace, outputs, baseline, result) -> None:
        path = _resolve(workspace, check["path"])
        if not path.is_file():
            result["detail"] = "%s does not exist" % check["path"]
            return
        content = path.read_text(encoding="utf-8", errors="replace")
        claimed = outputs.get(check["output"])
        result["sha256"] = _sha256_file(path)
        result["passed"] = isinstance(claimed, str) and claimed.strip() == content.strip()
        result["detail"] = "output %s matches %s" % (check["output"], check["path"]) if result["passed"] else \
            "output %s does not match the content of %s" % (check["output"], check["path"])

    def _check_git_diff(self, check, workspace, outputs, baseline, result) -> None:
        now = git_changes(workspace)
        if now is None:
            result["detail"] = "workspace is not a git work tree"
            return
        before = baseline.get("git") or {}
        changed = sorted(p for p, digest in now.items() if before.get(p) != digest)
        changed += sorted(p for p in before if p not in now)  # reverted during the attempt
        allowed = list(check.get("allowed_paths") or ["*"])
        outside = [p for p in changed if not any(fnmatch.fnmatch(p, pattern) for pattern in allowed)]
        result["changed"] = changed[:500]
        problems = []
        if outside:
            problems.append("changes outside %s: %s" % (allowed, ", ".join(outside[:10])))
        if check.get("require_changes") and not changed:
            problems.append("no changes were made")
        if len(changed) > int(check.get("max_files", 1000)):
            problems.append("%d files changed (max %s)" % (len(changed), check.get("max_files")))
        result["passed"] = not problems
        result["detail"] = "; ".join(problems) or "%d file(s) changed within %s" % (len(changed), allowed)

    # -- misc --------------------------------------------------------------------
    def _baseline(self, state: Mapping[str, Any], node_state: Mapping[str, Any]) -> dict[str, Any]:
        ref = node_state.get("baseline")
        if not ref:
            return {}
        try:
            return dict(self._store().read_artifact(state["run_id"], ref["artifact"]))
        except GraphEngineeringError:
            return {}

    def _store(self) -> Any:
        if self.store is None:
            raise GraphEngineeringError(ErrorCode.EVIDENCE_FAILED, "evidence needs the run store")
        return self.store


def _diff_fingerprints(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    old, new = before.get("files") or {}, after.get("files") or {}
    return sorted({p for p in set(old) | set(new) if old.get(p) != new.get(p)})


def _descendants(spec: GraphSpec, node_id: str) -> set[str]:
    found: set[str] = set()
    pending = [node_id]
    while pending:
        current = pending.pop()
        for child in spec.dependents(current):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found
