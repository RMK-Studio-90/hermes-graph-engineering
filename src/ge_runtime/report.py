"""Plain-text renderings of plans, status, explanations and verification.

Output is compact Markdown-friendly text that reads well in chat platforms and
terminals alike. Machine consumers should use the dict views instead.
"""
from __future__ import annotations

from typing import Any, Mapping

_STATUS_MARK = {
    "PENDING": "[ ]",
    "READY": "[>]",
    "RUNNING": "[~]",
    "SUCCEEDED": "[x]",
    "FAILED": "[!]",
    "BLOCKED": "[#]",
    "SKIPPED": "[-]",
}


def render_error(error: Mapping[str, Any]) -> str:
    lines = ["GE_ERROR %s: %s" % (error.get("error"), error.get("message"))]
    for item in (error.get("details") or {}).get("errors", [])[:20]:
        lines.append("  - %s" % item)
    cycle = (error.get("details") or {}).get("cycle")
    if cycle:
        lines.append("  cycle: %s" % " -> ".join(cycle))
    return "\n".join(lines)


def render_plan(plan: Mapping[str, Any], run_id: str | None = None) -> str:
    lines = ["Graph plan: %s (%s)" % (plan["title"], plan["graph_id"])]
    if run_id:
        lines.append("run: %s" % run_id)
    lines.append("spec: %s" % plan["spec_digest"])
    lines.append("execution order: %s" % " -> ".join(plan["execution_order"]))
    for level, layer in enumerate(plan["layers"]):
        lines.append("  layer %d: %s" % (level, ", ".join(layer)))
    lines.append("nodes:")
    for node in plan["nodes"]:
        flags = []
        if node["side_effects"]:
            flags.append("side-effects")
        if not node["idempotent"]:
            flags.append("non-idempotent")
        lines.append("  - %s [%s, owner=%s%s]" % (
            node["id"], node["executor"], node["owner"], (", " + ", ".join(flags)) if flags else ""))
        lines.append("      purpose: %s" % node["purpose"])
        lines.append("      depends on: %s" % (", ".join(node["depends_on"]) or "-"))
        lines.append("      requires: %s" % ", ".join(node["requires"]))
        lines.append("      outputs: %s" % (", ".join("%s:%s" % (k, v["type"]) for k, v in node["outputs"].items()) or "-"))
        if node["success_criteria"]:
            lines.append("      success: %s" % "; ".join(
                "%s %s%s" % (c["output"], c["check"], (" %r" % c["value"]) if "value" in c else "")
                for c in node["success_criteria"]))
        lines.append("      on failure: %s, attempts: %d" % (node["on_failure"], node["retry"]["max_attempts"]))
        if node["gates"]:
            lines.append("      gates: %s" % ", ".join("%s(%s)" % (g["id"], g["type"]) for g in node["gates"]))
        if node["rollback_boundary"]:
            lines.append("      rollback boundary: %s" % node["rollback_boundary"])
    lines.append("gates: %s" % ", ".join("%s(%s)" % (g["target"], g["type"]) for g in plan["gates"]))
    if plan["rollback_boundaries"]:
        lines.append("rollback boundaries: %s" % "; ".join(
            "%s=%s" % (name, ",".join(nodes)) for name, nodes in plan["rollback_boundaries"].items()))
    return "\n".join(lines)


def render_status(summary: Mapping[str, Any], work: list[Mapping[str, Any]] | None = None) -> str:
    lines = ["Run %s: %s" % (summary["run_id"], summary["status"])]
    lines.append("graph: %s (%s)" % (summary["title"], summary["graph_id"]))
    for node_id, node in summary["nodes"].items():
        wait = " (waiting: %s)" % node["wait"] if node["wait"] else ""
        lines.append("  %s %s %s attempts=%d%s" % (
            _STATUS_MARK.get(node["status"], "[?]"), node_id, node["status"], node["attempts"], wait))
    lines.append("current: %s" % (", ".join(summary["current_nodes"]) or "-"))
    lines.append("completed: %s" % (", ".join(summary["completed_nodes"]) or "-"))
    if summary["blocked_nodes"]:
        lines.append("blocked: %s" % "; ".join("%s (%s)" % (b["node"], b["reason"]) for b in summary["blocked_nodes"]))
    if summary["failed_nodes"]:
        lines.append("failed: %s" % "; ".join(
            "%s %s: %s" % (f["node"], (f["error"] or {}).get("code"), (f["error"] or {}).get("message"))
            for f in summary["failed_nodes"]))
    lines.append("gates: %s" % ", ".join("%s=%s" % (g["target"], g["status"]) for g in summary["gates"]))
    for hold in summary["holds"]:
        target = hold.get("node") or "run"
        if hold.get("gate"):
            target += ":" + hold["gate"]
        lines.append("hold: %s %s%s" % (hold["kind"], target, (" - " + hold["detail"]) if hold.get("detail") else ""))
    if summary.get("verification"):
        lines.append("verification: %s" % ("VERIFIED" if summary["verification"]["verified"] else "NOT VERIFIED"))
    for order in work or []:
        lines.append("work order: node %s attempt %d/%d - %s" % (
            order["node"], order["attempt"], order["max_attempts"], order["purpose"]))
    return "\n".join(lines)


def render_explain(view: Mapping[str, Any]) -> str:
    lines = ["Graph explanation: %s (%s)" % (view["title"], view["graph_id"])]
    for node in view["nodes"]:
        lines.append("* %s - %s" % (node["id"], node["name"]))
        lines.append("    why: %s" % node["why"])
        lines.append("    how: %s; requires %s" % (node["execution"], ", ".join(node["requires"])))
        for dep in node["dependencies"]:
            lines.append("    after %s: %s" % (dep["node"], dep["reason"]))
        for gate in node["gates"]:
            lines.append("    gate %s (%s, owner %s): %s" % (gate["gate"], gate["type"], gate["owner"], gate["reason"]))
        if node["success_criteria"]:
            lines.append("    succeeds when: %s" % "; ".join(
                "%s %s%s" % (c["output"], c["check"], (" %r" % c["value"]) if "value" in c else "")
                for c in node["success_criteria"]))
        else:
            lines.append("    succeeds when: outputs match the declared contract")
        lines.append("    on failure: %s; max attempts %d" % (node["failure"], node["retry"]))
        if node["rollback_boundary"]:
            lines.append("    rollback boundary %s: failures here need operator compensation" % node["rollback_boundary"])
    return "\n".join(lines)


def render_verification(receipt: Mapping[str, Any]) -> str:
    lines = ["Verification of %s: %s" % (receipt["run_id"], "VERIFIED" if receipt["verified"] else "NOT VERIFIED")]
    lines.append("run status: %s, spec: %s" % (receipt["run_status"], receipt["spec_digest"]))
    for check in receipt["checks"]:
        lines.append("  %s %s - %s" % ("PASS" if check["passed"] else "FAIL", check["check"], check["detail"]))
    return "\n".join(lines)
