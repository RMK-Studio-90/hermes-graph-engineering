"""Built-in workflow graphs.

``audit_workflow`` is an isolated multi-agent review::

    context_map -> hunt_<specialty> (one per specialty) -> collect (builtin)
        -> blind_review        sees the findings only: no code, no hunter reasoning
        -> verify_code         sees each finding plus the exact source lines the runtime read for its
                               location; no hunter reasoning, no reviewer notes
        -> adjudicate (builtin) deterministic statuses: a finding is CONFIRMED only if the independent
                               verifier confirmed it AND its quotes exist in the runtime-read source
        -> synthesize          writes the report from the adjudicated ledger only

Every node is a separate worker with a fresh context; artifacts move only through declared,
projected inputs. All nodes are read-only, so the policy approves the plan automatically and the
runtime's read-only guard fails any node that changes the workspace.
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT_SPECIALTIES = ("correctness", "security", "concurrency")
_FINDING_SCHEMA = ('a JSON array of findings, each {"title": str, "claim": str, "location": "path:start-end", '
                   '"severity": "low|medium|high|critical", "category": str, "reasoning": str}; locations are paths '
                   'relative to the workspace with 1-based line ranges; an empty array if nothing was found')


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30] or "general"


def audit_workflow(target: str, *, specialties: tuple[str, ...] | list[str] = DEFAULT_SPECIALTIES,
                   graph_id: str = "audit") -> dict[str, Any]:
    if not isinstance(target, str) or not target.strip():
        target = "the workspace"
    specialties = [s for s in dict.fromkeys(_slug(s) for s in specialties) if s][:6] or ["general"]
    task_input = {"task": {"from": "graph.inputs.target", "type": "string"}}
    nodes: list[dict[str, Any]] = [{
        "id": "context_map", "name": "context mapper",
        "purpose": "Map the audit target: modules, entry points, trust boundaries and data flows",
        "executor": "agent", "requires": ["agent.reasoning"], "risk": "READ_ONLY",
        "instructions": "Read the audit target and describe its structure for the specialists. Leave every file "
                        "as it is.",
        "inputs": task_input, "outputs": {"context": {"type": "string"}},
        "success_criteria": [{"output": "context", "check": "non_empty"}],
        "idempotent": True, "side_effects": False, "retry": {"max_attempts": 2},
    }]
    hunters = []
    for specialty in specialties:
        node_id = "hunt_%s" % specialty
        hunters.append(node_id)
        nodes.append({
            "id": node_id, "name": "%s hunter" % specialty,
            "purpose": "Look for %s problems in the audit target" % specialty.replace("_", " "),
            "depends_on": ["context_map"], "executor": "agent", "requires": ["agent.reasoning"],
            "risk": "READ_ONLY",
            "instructions": "Specialist review for %s. Report %s. Report only what you can point to in the "
                            "source. Leave every file as it is." % (specialty.replace("_", " "), _FINDING_SCHEMA),
            "inputs": {"target": {"from": "graph.inputs.target", "type": "string"},
                       "context": {"from": "context_map.context", "type": "string"}},
            "outputs": {"findings": {"type": "array"}},
            "idempotent": True, "side_effects": False, "retry": {"max_attempts": 2},
        })
    nodes.append({
        "id": "collect", "name": "collect findings", "purpose": "Merge the specialists' findings with stable ids",
        "depends_on": hunters, "executor": "builtin", "operation": {"op": "collect_findings"},
        "inputs": {h: {"from": "%s.findings" % h, "type": "array"} for h in hunters},
        "outputs": {"findings": {"type": "array"}, "invalid": {"type": "integer"}},
    })
    public = ["id", "title", "claim", "location", "severity", "category"]
    nodes.append({
        "id": "blind_review", "name": "blind report reviewer",
        "purpose": "Assess each finding as written, without seeing code or how it was found",
        "depends_on": ["collect"], "executor": "agent", "requires": ["agent.review"], "risk": "READ_ONLY",
        "instructions": 'For each finding return {"id", "assessment": "plausible|implausible|duplicate", "notes"} '
                        "as a JSON array in output 'reviews'. You see the findings only; judge clarity, "
                        "plausibility and duplicates.",
        "inputs": {"findings": {"from": "collect.findings", "type": "array", "view": {"pick": public}}},
        "outputs": {"reviews": {"type": "array"}},
        "idempotent": True, "side_effects": False, "retry": {"max_attempts": 2},
    })
    nodes.append({
        "id": "verify_code", "name": "independent code verifier",
        "purpose": "Verify each finding against the cited source lines only",
        "depends_on": ["collect"], "executor": "agent", "requires": ["agent.review"], "risk": "READ_ONLY",
        "instructions": 'For each finding return {"id", "verdict": "CONFIRMED|PARTIALLY_CONFIRMED|REJECTED|'
                        'NEEDS_MORE_EVIDENCE", "quotes": [{"line": int, "text": str}], "explanation"} as a JSON '
                        "array in output 'verifications'. Each finding carries the exact source lines under "
                        "'source'. A confirmation counts only if every quote appears verbatim on the cited line.",
        "inputs": {"findings": {"from": "collect.findings", "type": "array",
                                "view": {"pick": public, "attach_source": {"context_lines": 3}}}},
        "outputs": {"verifications": {"type": "array"}},
        "idempotent": True, "side_effects": False, "retry": {"max_attempts": 2},
    })
    nodes.append({
        "id": "adjudicate", "name": "adjudicate findings",
        "purpose": "Deterministic finding statuses from the independent verification",
        "depends_on": ["blind_review", "verify_code"], "executor": "builtin",
        "operation": {"op": "adjudicate_findings"},
        "inputs": {"findings": {"from": "collect.findings", "type": "array",
                                "view": {"attach_source": {"context_lines": 3}}},
                   "verifications": {"from": "verify_code.verifications", "type": "array"},
                   "reviews": {"from": "blind_review.reviews", "type": "array"}},
        "outputs": {"ledger": {"type": "array"}, "counts": {"type": "object"}},
    })
    nodes.append({
        "id": "synthesize", "name": "final synthesizer",
        "purpose": "Compose the audit report from the adjudicated ledger",
        "depends_on": ["adjudicate"], "executor": "agent", "requires": ["agent.generation"], "risk": "READ_ONLY",
        "instructions": "Summarize the audit from the ledger. Report every finding with its status exactly as "
                        "given; do not upgrade UNVERIFIED or NEEDS_MORE_EVIDENCE findings.",
        "inputs": {"ledger": {"from": "adjudicate.ledger", "type": "array"},
                   "counts": {"from": "adjudicate.counts", "type": "object"}},
        "outputs": {"report": {"type": "string"}},
        "success_criteria": [{"output": "report", "check": "non_empty"}],
        "idempotent": True, "side_effects": False, "retry": {"max_attempts": 2},
    })
    return {"schema_version": 1, "graph_id": graph_id, "title": "Audit: %s" % target.strip()[:100],
            "description": "Isolated multi-agent audit with deterministic finding adjudication",
            "inputs": {"target": target.strip()}, "nodes": nodes}
