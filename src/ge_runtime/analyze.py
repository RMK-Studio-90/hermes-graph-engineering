"""Deterministic task analysis: task text -> draft graph spec.

The analyzer is intentionally simple and predictable. It splits a task into
steps (numbered or bulleted items, lines, or "then"/";" separators), chains
them sequentially and classifies each step with keyword signals:

* side-effect verbs (write, delete, deploy, send, ...) -> ``side_effects``,
  non-idempotent, approval gate, own rollback boundary
* verification verbs (verify, test, confirm, ...) -> review capability and a
  boolean ``passed`` output that must be ``true``
* research / code / tool / generation verbs -> matching capability ids

Every generated node runs on the ``agent`` executor, so the host decides which
model or tooling performs it. The result is a *draft*: it is admitted through
the normal validator and still requires plan approval before execution. A
smarter host (for example an agent) may instead submit its own spec.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .spec import GRAPH_ID_RE, admit

MAX_STEPS = 50
MAX_TASK_CHARS = 20_000

_ITEM = re.compile(r"(?:^|\s)(?:\d{1,2}[.)]|[-*•])\s+")
_SEPARATORS = re.compile(r"\s*(?:;|\bthen\b|\bund dann\b|\bdanach\b|\banschließend\b)\s*", re.IGNORECASE)

SIGNALS = {
    "side_effects": (
        "write", "save", "delete", "remove", "deploy", "publish", "send", "email", "post ", "commit", "push",
        "install", "uninstall", "migrate", "drop ", "overwrite", "upload", "purchase", "pay ", "rename", "move ",
        "schreib", "speicher", "lösch", "veröffentlich", "sende", "installier", "hochlad", "verschieb",
    ),
    "verification": ("verify", "verification", "test", "assert", "confirm", "prüfe ergebnis", "verifizier", "teste"),
    "research": ("research", "search", "look up", "investigate", "find out", "recherch", "such"),
    "code": ("code", "implement", "refactor", "fix", "bug", "function", "class ", "programm", "implementier"),
    "tools": ("read", "load", "fetch", "open", "parse", "import", "download", "list", "lies", "lese", "lade"),
    "reasoning": ("validate", "valid", "check", "ensure", "analy", "decide", "plan", "prüf", "bewert", "entscheid"),
    "generation": ("transform", "convert", "generate", "create", "summar", "draft", "format", "compose", "translate",
                   "erstell", "generier", "zusammenfass", "umwandel", "übersetz"),
}
_CAPABILITY_FOR = {
    "research": "agent.research",
    "code": "agent.code",
    "tools": "agent.tools",
    "reasoning": "agent.reasoning",
    "generation": "agent.generation",
    "verification": "agent.review",
}


def split_steps(task: str) -> tuple[str, list[str]]:
    """Return ``(preamble, steps)`` for a task description."""
    text = task.strip()
    matches = list(_ITEM.finditer(text))
    if len(matches) >= 2:
        preamble = text[:matches[0].start()].strip()
        steps = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            steps.append(text[match.end():end])
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2:
            preamble, steps = "", lines
        else:
            parts = [part for part in _SEPARATORS.split(text) if part.strip()]
            preamble, steps = "", parts if len(parts) >= 2 else [text]
    cleaned = [re.sub(r"\s+", " ", step).strip().rstrip(".,;:") for step in steps]
    return preamble.rstrip(":").strip(), [step for step in cleaned if step][:MAX_STEPS]


def _slug(text: str, separator: str, limit: int) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", separator, ascii_text).strip(separator)
    return slug[:limit].strip(separator)


_SIGNAL_PATTERNS = {
    signal: re.compile(r"\b(?:%s)" % "|".join(re.escape(word.strip()) for word in words), re.IGNORECASE)
    for signal, words in SIGNALS.items()
}


def classify(step: str) -> dict[str, bool]:
    """Keyword signals matched at word starts (``test`` matches ``tests``, not ``latest``)."""
    return {signal: bool(pattern.search(step)) for signal, pattern in _SIGNAL_PATTERNS.items()}


def analyze_task(task: str, *, graph_id: str | None = None, title: str | None = None) -> dict[str, Any]:
    """Build and admit a draft spec for ``task``. Raises GraphEngineeringError if it cannot be admitted."""
    from .errors import ErrorCode, GraphEngineeringError

    if not isinstance(task, str) or not task.strip():
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "task text is required")
    if len(task) > MAX_TASK_CHARS:
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "task exceeds %d characters" % MAX_TASK_CHARS)
    preamble, steps = split_steps(task)
    title = (title or preamble or steps[0])[:120]
    graph_id = graph_id or _slug(title, "-", 48) or "task"
    if not GRAPH_ID_RE.match(graph_id):
        graph_id = "task-" + graph_id.strip("-_")[:40] if graph_id.strip("-_") else "task"

    nodes: list[dict[str, Any]] = []
    signals_report = []
    used: set[str] = set()
    for index, step in enumerate(steps, start=1):
        base = _slug(step, "_", 40) or "step"
        if not base[0].isalpha():
            base = "step_" + base
        node_id, suffix = base[:40], 2
        while node_id in used:
            node_id = "%s_%d" % (base[:36], suffix)
            suffix += 1
        used.add(node_id)
        signals = classify(step)
        requires = sorted({cap for signal, cap in _CAPABILITY_FOR.items() if signals[signal]}) or ["agent.reasoning"]
        previous = nodes[-1]["id"] if nodes else None
        inputs: dict[str, Any] = {"task": {"from": "graph.inputs.task", "type": "string"}}
        if previous:
            inputs["previous"] = {"from": "%s.result" % previous, "type": "string"}
        outputs: dict[str, Any] = {"result": {"type": "string", "description": "what this step produced or found"}}
        criteria: list[dict[str, Any]] = [{"output": "result", "check": "non_empty"}]
        if signals["verification"]:
            outputs["passed"] = {"type": "boolean", "description": "true only if the verification succeeded"}
            criteria.append({"output": "passed", "check": "true"})
        node: dict[str, Any] = {
            "id": node_id,
            "name": step[:80],
            "purpose": step,
            "depends_on": [previous] if previous else [],
            "executor": "agent",
            "requires": requires,
            "instructions": "Step %d of %d: %s. Use the task and the previous step's result as context; "
                            "report what was produced in 'result'." % (index, len(steps), step),
            "inputs": inputs,
            "outputs": outputs,
            "success_criteria": criteria,
            "on_failure": "stop",
        }
        if signals["side_effects"]:
            node.update({
                "side_effects": True,
                "idempotent": False,
                "gates": [{"id": "approval", "type": "approval", "owner": "operator",
                           "description": "step changes external state"}],
                "rollback_boundary": "boundary_%s" % node_id[:50],
            })
        nodes.append(node)
        signals_report.append({"node": node_id, "signals": sorted(k for k, v in signals.items() if v)})

    raw = {
        "schema_version": 1,
        "graph_id": graph_id,
        "title": title,
        "description": task.strip()[:2000],
        "inputs": {"task": task.strip()},
        "nodes": nodes,
    }
    spec = admit(raw)
    return {
        "spec": spec.data,
        "spec_digest": spec.digest,
        "analysis": {
            "method": "deterministic step split + keyword signals; review before approving",
            "steps": steps,
            "signals": signals_report,
        },
    }
