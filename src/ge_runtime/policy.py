"""Risk policy: classify every node, approve safe work automatically, stop risky work.

Risk classes (ascending)::

    READ_ONLY < WORKSPACE_WRITE < LOCAL_EXEC < NETWORK < EXTERNAL_SIDE_EFFECT < PAID < DESTRUCTIVE

A node's risk is the maximum of what it *declares* (``risk``, ``side_effects``) and what the
runtime *infers* from its executor, required capabilities, evidence commands and the text
of its name, purpose and instructions. A declaration can raise the risk but never lower it:
``side_effects: false`` on a node whose instructions say "deploy" is still
``EXTERNAL_SIDE_EFFECT``, and the contradiction is recorded as a reason.

Decisions (default configuration)::

    READ_ONLY, WORKSPACE_WRITE, LOCAL_EXEC       AUTO_APPROVE
    NETWORK, EXTERNAL_SIDE_EFFECT, PAID          REQUIRE_APPROVAL  (a digest-bound operator gate)
    DESTRUCTIVE                                  DENY              (never executed)

The evaluation is persisted in the run (``state["policy"]``) with the configuration, the
spec digest and a digest over all decisions. A plan approved by policy stores that digest as
its basis; verification re-classifies the plan with the recorded configuration, so a policy
approval cannot be transferred to a changed plan or a tampered record.

Worker rights follow the risk class: ``worker_level`` tells the host binding which tool
categories a worker for the node may use (the Hermes binding narrows the worker's toolsets
and blocks other tools at call time).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from .errors import ErrorCode, GraphEngineeringError
from .spec import RISK_CLASS_NAMES, GraphSpec, digest_of

POLICY_VERSION = 1


class RiskClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    WORKSPACE_WRITE = "WORKSPACE_WRITE"
    LOCAL_EXEC = "LOCAL_EXEC"
    NETWORK = "NETWORK"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"
    PAID = "PAID"
    DESTRUCTIVE = "DESTRUCTIVE"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)


_ORDER = [RiskClass.READ_ONLY, RiskClass.WORKSPACE_WRITE, RiskClass.LOCAL_EXEC, RiskClass.NETWORK,
          RiskClass.EXTERNAL_SIDE_EFFECT, RiskClass.PAID, RiskClass.DESTRUCTIVE]
RISK_CLASSES = tuple(r.value for r in _ORDER)
assert RISK_CLASSES == RISK_CLASS_NAMES


class Decision(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


def max_risk(risks: Iterable[RiskClass]) -> RiskClass:
    return max(risks, key=lambda r: r.rank, default=RiskClass.READ_ONLY)


# Text signals, strongest first. Matched case-insensitively against name, purpose and instructions.
_SIGNALS: list[tuple[RiskClass, str, re.Pattern[str]]] = [
    (RiskClass.DESTRUCTIVE, "destructive operation", re.compile(
        r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|\bdrop\s+(table|database|schema|index)\b|\btruncate\s+table\b"
        r"|\bwipe\b|\bdestroy\b|\bpurge\b|\bforce[- ]push\b|push\s+--force|reset\s+--hard|\bmkfs\b|\bshred\b"
        r"|\bdelete\s+(all|every|the\s+)?\s*(files?|folders?|director(y|ies)|branch(es)?|repo(sitor(y|ies))?|"
        r"database|tables?|bucket|account|users?|records?|rows?|backups?|data)\b"
        r"|\bl(ö|oe)sch(e|en|t)?\b|\bvernicht", re.I)),
    (RiskClass.PAID, "payment or paid service", re.compile(
        r"\b(purchase|buy|pay(ment)?|checkout|subscribe|subscription|billing|invoice|credit\s*card|"
        r"paid\s+(api|plan|tier|service)|kauf(e|en)?|bezahl|zahlung|abonn)", re.I)),
    (RiskClass.EXTERNAL_SIDE_EFFECT, "external side effect", re.compile(
        r"\b(deploy|publish|release\s+to|ship\s+to|send|e-?mail|sms|tweet|notify|webhook|post\s+(to|on|a)|"
        r"git\s+push|push\s+(to|the)|merge\s+(to|into)|open\s+a\s+pull\s+request|create\s+(a\s+)?(pr|issue|ticket)|"
        r"upload|ver(ö|oe)ffentlich|sende|versende|hochlad|benachrichtig)", re.I)),
    (RiskClass.NETWORK, "network access", re.compile(
        r"https?://|\b(download|curl|wget|internet|web\s*search|online|api\s+(call|request)|rest\s+api|"
        r"pip\s+install|npm\s+install|git\s+clone|fetch\s+(from|the)\s+(web|url|internet|remote|site)|"
        r"research|recherch|herunterlad)", re.I)),
    (RiskClass.LOCAL_EXEC, "local command execution", re.compile(
        r"\b(run|execute|pytest|unittest|npm\s+(run|test)|make|compile|build|shell|terminal|command|script|"
        r"tests?|ausf(ü|ue)hr|teste)\b", re.I)),
    (RiskClass.WORKSPACE_WRITE, "workspace change", re.compile(
        r"\b(write|create|edit|modify|change|update|implement|refactor|fix|save|patch|rename|move|remove|"
        r"schreib|erstell|(ä|ae)nder|implementier|speicher|korrigier)", re.I)),
]

_CAPABILITY_RISK = {
    "compute.pure": RiskClass.READ_ONLY,
    "agent.reasoning": RiskClass.READ_ONLY,
    "agent.generation": RiskClass.READ_ONLY,
    "agent.review": RiskClass.READ_ONLY,
    "agent.tools": RiskClass.WORKSPACE_WRITE,
    "agent.code": RiskClass.WORKSPACE_WRITE,
    "agent.research": RiskClass.NETWORK,
}


def classify_command(argv: Any) -> tuple[RiskClass, str]:
    """Risk of a command the runtime itself runs as evidence."""
    text = " ".join(str(part) for part in argv) if isinstance(argv, (list, tuple)) else str(argv)
    for risk, label, pattern in _SIGNALS[:4]:
        if pattern.search(text):
            return risk, "evidence command %r: %s" % (text[:80], label)
    return RiskClass.LOCAL_EXEC, "evidence command %r runs locally" % text[:80]


def classify_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one admitted node. Pure and deterministic."""
    reasons: list[str] = []
    risks: list[RiskClass] = []
    if node.get("executor") == "builtin":
        risks.append(RiskClass.READ_ONLY)
        reasons.append("builtin operation is pure")
    for capability in node.get("requires") or []:
        risk = _CAPABILITY_RISK.get(capability, RiskClass.WORKSPACE_WRITE)
        if risk.rank > RiskClass.READ_ONLY.rank:
            reasons.append("capability %s implies %s" % (capability, risk.value))
        risks.append(risk)
    if node.get("executor") != "builtin":
        text = " ".join(str(node.get(key) or "") for key in ("name", "purpose", "instructions"))
        for risk, label, pattern in _SIGNALS:
            match = pattern.search(text)
            if match:
                risks.append(risk)
                reasons.append("%s (%r)" % (label, match.group(0).strip()[:40]))
    for check in node.get("evidence") or []:
        if isinstance(check, Mapping) and check.get("type") == "command":
            risk, why = classify_command(check.get("argv") or [])
            risks.append(risk)
            reasons.append(why)
    inferred = max_risk(risks)
    declared_risk = node.get("risk")
    declared = RiskClass(declared_risk) if declared_risk in RISK_CLASSES else None
    effective = inferred
    if declared is not None:
        if declared.rank > inferred.rank:
            effective = declared
            reasons.append("declared risk %s" % declared.value)
        elif declared.rank < inferred.rank:
            reasons.append("declared risk %s ignored: inferred %s" % (declared.value, inferred.value))
    if node.get("side_effects") is True and effective.rank < RiskClass.EXTERNAL_SIDE_EFFECT.rank:
        effective = RiskClass.EXTERNAL_SIDE_EFFECT
        reasons.append("declares side effects")
    if node.get("side_effects") is False and effective.rank >= RiskClass.EXTERNAL_SIDE_EFFECT.rank:
        reasons.append("declared side_effects: false is contradicted; not trusted")
    return {"risk": effective.value, "inferred": inferred.value,
            "declared": declared.value if declared else None, "reasons": reasons}


@dataclass(frozen=True)
class PolicyConfig:
    auto_approve_max: RiskClass = RiskClass.LOCAL_EXEC
    deny: frozenset[RiskClass] = field(default_factory=lambda: frozenset({RiskClass.DESTRUCTIVE}))

    def to_dict(self) -> dict[str, Any]:
        return {"version": POLICY_VERSION, "auto_approve_max": self.auto_approve_max.value,
                "deny": sorted(r.value for r in self.deny)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicyConfig":
        try:
            return cls(auto_approve_max=RiskClass(data["auto_approve_max"]),
                       deny=frozenset(RiskClass(r) for r in data.get("deny") or []))
        except (KeyError, ValueError, TypeError) as exc:
            raise GraphEngineeringError(ErrorCode.PLUGIN_CONFIGURATION_INVALID, "invalid policy configuration") from exc

    def decide(self, risk: RiskClass) -> Decision:
        if risk in self.deny:
            return Decision.DENY
        if risk.rank <= self.auto_approve_max.rank:
            return Decision.AUTO_APPROVE
        return Decision.REQUIRE_APPROVAL


def evaluate_plan(spec: GraphSpec, config: PolicyConfig) -> dict[str, Any]:
    """The persisted policy record for one plan (see module docstring)."""
    decisions: dict[str, Any] = {}
    for node_id in spec.order:
        node = spec.node(node_id)
        classified = classify_node(node)
        decision = config.decide(RiskClass(classified["risk"]))
        entry = dict(classified, decision=decision.value)
        if decision is Decision.REQUIRE_APPROVAL and any(g["type"] == "approval" for g in node["gates"]):
            entry["gated_by"] = "declared approval gate"
        decisions[node_id] = entry
    values = {d["decision"] for d in decisions.values()}
    plan = (Decision.DENY if Decision.DENY.value in values else
            Decision.REQUIRE_APPROVAL if Decision.REQUIRE_APPROVAL.value in values else Decision.AUTO_APPROVE)
    record = {"version": POLICY_VERSION, "config": config.to_dict(), "config_digest": digest_of(config.to_dict()),
              "spec_digest": spec.digest, "decisions": decisions, "plan_decision": plan.value,
              "max_risk": max_risk(RiskClass(d["risk"]) for d in decisions.values()).value}
    record["digest"] = digest_of(record)
    return record


def record_digest(record: Mapping[str, Any]) -> str:
    return digest_of({k: v for k, v in record.items() if k != "digest"})


class PolicyVerifier:
    """Engine collaborator: re-derives the persisted policy record during verification."""

    def verify_record(self, state: Mapping[str, Any], spec: GraphSpec, engine: Any) -> tuple[bool, str]:
        record = state.get("policy")
        if not isinstance(record, Mapping):
            return True, "no policy record"
        if record.get("digest") != record_digest(record):
            return False, "policy record digest mismatch (tampered)"
        if record.get("spec_digest") != spec.digest:
            return False, "policy record was made for another plan"
        expected = evaluate_plan(spec, PolicyConfig.from_dict(record["config"]))
        if expected["decisions"] != record.get("decisions"):
            return False, "policy decisions do not match a re-classification of this plan"
        plan = (state.get("approvals") or {}).get("plan") or {}
        if str(plan.get("decided_by", "")).startswith("policy:"):
            if (plan.get("basis") or {}).get("policy_digest") != record.get("digest"):
                return False, "plan approved by policy without a matching policy record"
            if record.get("plan_decision") == Decision.DENY.value:
                return False, "plan approved by policy although the policy denies it"
        risks = sorted({d["risk"] for d in record["decisions"].values()}, key=lambda r: RiskClass(r).rank)
        return True, "policy %s re-derived: max risk %s, decisions %s" % (
            record["digest"][:19], record.get("max_risk"), ", ".join(risks))


def worker_level(risk: str | None) -> RiskClass | None:
    """Tool level a worker for a node of this risk may use (None: the host's own permissions,
    which only happens after an operator approved the node)."""
    if risk not in RISK_CLASSES:
        return RiskClass.READ_ONLY
    value = RiskClass(risk)
    return value if value.rank <= RiskClass.NETWORK.rank else None


POLICY_ACTOR = "policy:auto-approval"


def apply_policy(engine: Any, run_id: str, config: PolicyConfig, *, decided_by: str = POLICY_ACTOR) -> dict[str, Any]:
    """Evaluate the run's current plan, persist the record and decide the plan by policy.

    * no DENY: the plan is approved by policy, bound to the plan digest, with the policy digest as
      its basis; nodes that need an operator carry a policy gate and hold there;
    * any DENY: the plan is rejected by policy (terminal ``POLICY_DENIED``); nothing runs.

    Idempotent: re-applying to a plan that is no longer a draft only refreshes nothing.
    """
    with engine.mutate(run_id) as (state, spec):
        record = evaluate_plan(spec, config)
        if state.get("policy") != record:
            state["policy"] = record
            engine.store.append_trace(run_id, "policy.evaluated", policy_digest=record["digest"],
                                      plan_decision=record["plan_decision"], max_risk=record["max_risk"],
                                      decisions={k: {"risk": v["risk"], "decision": v["decision"]}
                                                 for k, v in record["decisions"].items()})
            engine._save(state)
        status = state["status"]
    if status == "DRAFT":
        approve = record["plan_decision"] != Decision.DENY.value
        denied = [k for k, v in record["decisions"].items() if v["decision"] == Decision.DENY.value]
        basis = {"policy_digest": record["digest"], "plan_decision": record["plan_decision"],
                 "reason": "denied node(s): %s" % ", ".join(denied) if denied else "all risks within policy"}
        engine.decide(run_id, "plan", approve=approve, decided_by=decided_by, basis=basis)
    return record
