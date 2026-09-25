"""Autopilot planning: task text -> admitted graph with deterministic evidence, and replanning.

``plan_task`` is the deterministic ANALYZE + PLAN step used when no smarter planner (for
example the host agent writing its own spec) is available:

* the task is split into steps (``ge_runtime.analyze.split_steps``); every step becomes an
  ``agent`` node in a chain, carrying the whole task and the previous result;
* evidence is extracted from the task text itself, never from an agent's later claims:
  backticked commands (```python -m pytest -q```) become ``command`` checks, "create
  ``notes/todo.md``" becomes ``file_exists``, "``out.txt`` containing "OK"" becomes
  ``file_contains``; all of them are attached to a final builtin ``acceptance`` checkpoint;
* each node's side-effect flags follow the policy classification of its text, so a node that
  would reach outside the workspace is non-idempotent, gated and never retried automatically.

``replan`` revises a subgraph after a structural failure (see ``ge_runtime.autopilot``).
"""
from __future__ import annotations

import json
import re
import shlex
import unicodedata
from typing import Any, Mapping

from .analyze import MAX_TASK_CHARS, classify, split_steps, _CAPABILITY_FOR
from .errors import ErrorCode, GraphEngineeringError
from .policy import RiskClass, classify_node
from .spec import GRAPH_ID_RE, NODE_ID_RE, admit

COMMAND_WORDS = {"python", "python3", "pytest", "npm", "npx", "node", "make", "bash", "sh", "go", "cargo", "ruff",
                 "mypy", "tox", "uv", "pnpm", "yarn", "deno", "ruby", "rake", "mvn", "gradle", "dotnet", "php",
                 "jest", "vitest", "flake8", "black", "pylint", "git", "cat", "test", "grep", "diff", "ls"}
_BACKTICK = re.compile(r"`([^`\n]{1,300})`")
_FILE = r"[`\"']?((?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z0-9]{1,8})[`\"']?"
_CREATES = re.compile(r"\b(?:create|write|generate|produce|save|erstelle|schreibe|speichere|lege)\b[^.\n]{0,40}?"
                      + _FILE, re.I)
_CONTAINS = re.compile(_FILE + r"\s+(?:that\s+)?(?:containing|contains|with\s+(?:the\s+)?(?:text|content|contents)"
                       r"|mit\s+(?:dem\s+)?(?:inhalt|text)|die\s+enth(?:ä|ae)lt|enth(?:ä|ae)lt)\s*:?\s*"
                       r"[\"“„']([^\"”“']{1,500})[\"”“']", re.I)
_READ_ONLY_GIT = {"diff", "status", "log", "show"}


def _slug(text: str, separator: str, limit: int) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", separator, ascii_text).strip(separator)[:limit].strip(separator)


def extract_evidence(task: str) -> list[dict[str, Any]]:
    """Deterministic checks stated in the task text itself."""
    checks: list[dict[str, Any]] = []
    for raw in _BACKTICK.findall(task):
        try:
            argv = shlex.split(raw)
        except ValueError:
            continue
        if not argv:
            continue
        head = argv[0]
        if head.startswith("./") or head.split("/")[-1] in COMMAND_WORDS:
            if head == "git" and (len(argv) < 2 or argv[1] not in _READ_ONLY_GIT):
                continue  # a git command that changes state is work, not evidence
            checks.append({"type": "command", "argv": argv, "expect_exit": 0, "timeout_seconds": 600})
    contained = set()
    for path, text in _CONTAINS.findall(task):
        contained.add(path)
        checks.append({"type": "file_contains", "path": path, "text": text})
    for path in _CREATES.findall(task):
        if path not in contained and not path.lower().startswith(("http", "www.")):
            checks.append({"type": "file_exists", "path": path, "non_empty": True})
    unique, seen = [], set()
    for check in checks:
        key = json.dumps(check, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(check)
    return unique[:20]


def plan_task(task: str, *, graph_id: str | None = None) -> dict[str, Any]:
    """Build and admit the autopilot graph for ``task``."""
    if not isinstance(task, str) or not task.strip():
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "task text is required")
    if len(task) > MAX_TASK_CHARS:
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "task exceeds %d characters" % MAX_TASK_CHARS)
    preamble, steps = split_steps(task)
    if len(steps) > 12:  # a long specification is context for one worker, not a dozen tiny nodes
        steps = [task.strip()]
        preamble = ""
    title = (preamble or steps[0])[:120]
    graph_id = graph_id or _slug(title, "-", 48) or "task"
    if not GRAPH_ID_RE.match(graph_id):
        graph_id = "task-" + graph_id.strip("-_")[:40] if graph_id.strip("-_") else "task"
    evidence = extract_evidence(task)
    nodes: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, step in enumerate(steps, start=1):
        base = _slug(step, "_", 40) or "step"
        if not base[0].isalpha():
            base = "step_" + base
        node_id, suffix = base[:40], 2
        while node_id in used or node_id == "acceptance":
            node_id = "%s_%d" % (base[:36], suffix)
            suffix += 1
        used.add(node_id)
        signals = classify(step)
        requires = sorted({cap for signal, cap in _CAPABILITY_FOR.items() if signals[signal]}) or ["agent.reasoning"]
        previous = nodes[-1]["id"] if nodes else None
        inputs: dict[str, Any] = {"task": {"from": "graph.inputs.task", "type": "string"}}
        if previous:
            inputs["previous"] = {"from": "%s.result" % previous, "type": "string"}
        node: dict[str, Any] = {
            "id": node_id,
            "name": step[:80],
            "purpose": step,
            "depends_on": [previous] if previous else [],
            "executor": "agent",
            "requires": requires,
            # the fixed text uses no risk verbs: the policy classifies the node by its step, not by boilerplate
            "instructions": ("Step %d of %d: %s. The full task is in input 'task'; the previous step's result is in "
                             "input 'previous'. Do the work in the workspace, then report what you did in 'result'. "
                             "The runtime verifies the outcome deterministically afterwards; report only what you "
                             "actually produced." % (index, len(steps), step)),
            "inputs": inputs,
            "outputs": {"result": {"type": "string", "description": "what this step did or found"}},
            "success_criteria": [{"output": "result", "check": "non_empty"}],
            "on_failure": "stop",
        }
        risk = RiskClass(classify_node(node)["risk"])
        if risk.rank >= RiskClass.EXTERNAL_SIDE_EFFECT.rank:
            node.update({"side_effects": True, "idempotent": False,
                         "gates": [{"id": "approval", "type": "approval", "owner": "operator",
                                    "description": "step reaches outside the workspace (%s)" % risk.value}],
                         "rollback_boundary": "boundary_%s" % node_id[:50]})
        else:
            node.update({"side_effects": False, "idempotent": True, "retry": {"max_attempts": 2}})
        nodes.append(node)
    if evidence:
        nodes.append({
            "id": "acceptance",
            "name": "acceptance checks",
            "purpose": "Deterministic acceptance: the checks stated in the task, run by the runtime itself",
            "depends_on": [nodes[-1]["id"]],
            "executor": "builtin",
            "operation": {"op": "verify"},
            "outputs": {"passed": {"type": "boolean"}},
            "success_criteria": [{"output": "passed", "check": "true"}],
            "evidence": evidence,
            "on_failure": "stop",
        })
    raw = {
        "schema_version": 1,
        "graph_id": graph_id,
        "title": title,
        "description": task.strip()[:2000],
        "inputs": {"task": task.strip()},
        "nodes": nodes,
        "require_evidence": True,
    }
    spec = admit(raw)
    return {"spec": spec.data, "spec_digest": spec.digest, "analysis": {
        "method": "deterministic step split, policy-classified nodes, evidence extracted from the task text",
        "steps": steps, "evidence": evidence}}


# ---------------------------------------------------------------------- replanning
def _node(spec: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    return next(dict(n) for n in spec["nodes"] if n["id"] == node_id)


def _subjects(diagnosis: Mapping[str, Any]) -> set[str]:
    """Artifacts the failed checks are about: paths, and path-like command arguments."""
    found: set[str] = set()
    for check in diagnosis.get("failed_checks") or []:
        spec = check.get("spec") or {}
        if isinstance(spec.get("path"), str):
            found.add(spec["path"])
        for arg in spec.get("argv") or []:
            for token in re.findall(r"[\w./-]+\.[A-Za-z0-9]{1,8}", str(arg)):
                if "/" not in token or not token.startswith("/"):
                    found.add(token)
        for changed in check.get("changed") or []:
            found.add(str(changed))
    return {s for s in found if len(s) > 2}


def _repair_targets(data: Mapping[str, Any], failed: str, diagnosis: Mapping[str, Any], round_: int) -> list[str]:
    """Agent nodes upstream of a failed checkpoint that the failure is about.

    Round 1 reopens the upstream agent nodes whose text names an artifact of a failed check
    (falling back to the checkpoint's direct producers); later rounds reopen every agent node
    upstream of the checkpoint.
    """
    nodes = {n["id"]: n for n in data["nodes"]}
    ancestors: list[str] = []
    pending = list(nodes[failed]["depends_on"])
    while pending:
        current = pending.pop()
        if current in ancestors:
            continue
        ancestors.append(current)
        pending.extend(nodes[current]["depends_on"])
    agents = [n["id"] for n in data["nodes"] if n["id"] in ancestors and n["executor"] != "builtin"]
    if round_ > 1:
        return agents
    subjects = _subjects(diagnosis)
    relevant = [a for a in agents if any(s in " ".join(str(nodes[a].get(k) or "") for k in (
        "name", "purpose", "instructions")) for s in subjects)]
    return relevant or [d for d in nodes[failed]["depends_on"] if nodes[d]["executor"] != "builtin"]


def _short_id(base: str, suffix: str) -> str:
    node_id = "%s_%s" % (base[: 63 - len(suffix) - 1], suffix)
    if not NODE_ID_RE.match(node_id):
        node_id = "n_" + node_id[:60]
    return node_id


def replan(spec: Mapping[str, Any], failed: str, diagnosis: Mapping[str, Any], round_: int) -> tuple[dict[str, Any], str]:
    """Revise the subgraph around ``failed``. Returns ``(new raw spec, description)``.

    * a failed evidence checkpoint (builtin ``verify``) reopens the agent nodes it checks, with
      the diagnosis as an explicit input, so they are executed again and the checks re-run;
    * a failed agent node gets a new read-only diagnosis node in front of it whose analysis
      becomes an explicit input of the retried node.
    """
    data = json.loads(json.dumps(dict(spec)))
    nodes = {n["id"]: n for n in data["nodes"]}
    target = nodes[failed]
    key = "repair_%d" % round_
    data["inputs"][key] = json.loads(json.dumps(dict(diagnosis), default=str))
    note = ("\n\nRepair round %d: a previous attempt failed. The diagnosis is in input '%s' (failed checks, exit "
            "codes, output tails). Fix the cause; the same deterministic checks run again afterwards." % (round_, key))
    if target["executor"] == "builtin":
        producers = _repair_targets(data, failed, diagnosis, round_)
        if not producers:
            raise GraphEngineeringError(ErrorCode.NODE_FAILED, "no agent work upstream of %s can be repaired" % failed)
        for producer in producers:
            node = nodes[producer]
            node["inputs"][key] = {"from": "graph.inputs.%s" % key, "type": "object"}
            node["instructions"] = (node.get("instructions") or "") + note
        return data, "reopened %s with the diagnosis of %s" % (", ".join(producers), failed)
    diag_id = _short_id(failed, "diag%d" % round_)
    inputs = {name: dict(spec_in) for name, spec_in in target["inputs"].items() if name != "analysis"}
    inputs[key] = {"from": "graph.inputs.%s" % key, "type": "object"}
    data["inputs"][key]["failed_purpose"] = target["purpose"]
    diag = {
        # the text names no work itself (the failed node is identified in the input), so the policy
        # classifies this node by what it does: read-only reasoning
        "id": diag_id, "name": "failure analysis round %d" % round_,
        "purpose": "Explain why the previous attempt of the node named in the failure record did not succeed",
        "depends_on": list(target["depends_on"]), "executor": "agent", "requires": ["agent.reasoning"],
        "instructions": ("Read-only analysis. The failure record, including the failed node's purpose, is in input "
                         "'%s'. Explain the root cause and what the next attempt must do differently. Leave all "
                         "files as they are." % key),
        "inputs": inputs, "outputs": {"analysis": {"type": "string"}},
        "success_criteria": [{"output": "analysis", "check": "non_empty"}],
        "idempotent": True, "side_effects": False, "risk": "READ_ONLY", "on_failure": "stop",
    }
    target["depends_on"] = list(dict.fromkeys(list(target["depends_on"]) + [diag_id]))
    target["inputs"]["analysis"] = {"from": "%s.analysis" % diag_id, "type": "string"}
    target["inputs"][key] = {"from": "graph.inputs.%s" % key, "type": "object"}
    target["instructions"] = (target.get("instructions") or "") + note + \
        " A read-only diagnosis of the failure is in input 'analysis'."
    position = [n["id"] for n in data["nodes"]].index(failed)
    data["nodes"].insert(position, diag)
    return data, "inserted diagnosis node %s before %s" % (diag_id, failed)
