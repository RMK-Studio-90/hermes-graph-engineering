"""Graph spec format, admission validation and topology.

A spec is plain data (JSON or YAML). Admission is strict and fails closed:
unknown fields, dangling references, cycles, unknown executors and unmet
capabilities are all rejected before a run can be created.

Error precedence: structural problems (``GRAPH_INVALID``) first, then
``DEPENDENCY_CYCLE``, then ``EXECUTOR_UNAVAILABLE``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

import yaml

from .contracts import VALUE_TYPES, criterion_errors
from .errors import ErrorCode, GraphEngineeringError
from .executors import BUILTIN_OPERATIONS, CAPABILITIES, ExecutorCatalog, operation_errors
from .version import SPEC_SCHEMA_VERSION

GRAPH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
MAX_NODES = 200
MAX_SPEC_BYTES = 1_000_000
MAX_ATTEMPTS = 5
GATE_TYPES = ("approval", "review")
FAILURE_POLICIES = ("stop", "continue")

_GRAPH_FIELDS = {"schema_version", "graph_id", "title", "description", "inputs", "nodes", "acceptance",
                 "require_evidence"}
_NODE_FIELDS = {
    "id", "name", "purpose", "depends_on", "executor", "requires", "operation", "instructions", "inputs",
    "outputs", "success_criteria", "on_failure", "retry", "idempotent", "side_effects", "gates", "owner",
    "rollback_boundary", "risk", "evidence",
}
# Risk classes a node may declare (see ge_runtime.policy); a declaration can only raise the inferred risk.
RISK_CLASS_NAMES = ("READ_ONLY", "WORKSPACE_WRITE", "LOCAL_EXEC", "NETWORK", "EXTERNAL_SIDE_EFFECT", "PAID",
                    "DESTRUCTIVE")
_DEFAULT_REQUIRES = {"builtin": ["compute.pure"], "agent": ["agent.reasoning"]}


@dataclass(frozen=True)
class GraphSpec:
    """An admitted spec: normalized data plus derived topology and digest."""

    data: Mapping[str, Any]
    order: tuple[str, ...]
    digest: str

    @property
    def graph_id(self) -> str:
        return self.data["graph_id"]

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self.data["nodes"]

    def node(self, node_id: str) -> dict[str, Any]:
        for node in self.data["nodes"]:
            if node["id"] == node_id:
                return node
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "unknown node %r" % node_id)

    def dependents(self, node_id: str) -> list[str]:
        return [node["id"] for node in self.data["nodes"] if node_id in node["depends_on"]]


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def load_spec_text(text: str) -> Any:
    """Parse JSON or YAML spec text. Failures raise GRAPH_INVALID."""
    if not isinstance(text, str) or not text.strip():
        raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "spec text is empty")
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "spec exceeds %d bytes" % MAX_SPEC_BYTES)
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "spec is neither valid JSON nor YAML: %s" % exc) from exc


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _normalize_outputs(raw: Any, where: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        errors.append("%s: outputs must be a mapping of name -> {type, required}" % where)
        return outputs
    for name, spec in raw.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append("%s: invalid output name %r" % (where, name))
            continue
        if isinstance(spec, str):
            spec = {"type": spec}
        if not isinstance(spec, Mapping) or set(spec) - {"type", "required", "description"}:
            errors.append("%s: output %r must be a type string or {type, required, description}" % (where, name))
            continue
        type_name = spec.get("type", "any")
        if type_name not in VALUE_TYPES:
            errors.append("%s: output %r has unknown type %r" % (where, name, type_name))
        required = spec.get("required", True)
        if not isinstance(required, bool):
            errors.append("%s: output %r required must be a boolean" % (where, name))
        entry = {"type": type_name, "required": required}
        if isinstance(spec.get("description"), str):
            entry["description"] = spec["description"]
        outputs[name] = entry
    return outputs


def _normalize_inputs(raw: Any, where: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        errors.append("%s: inputs must be a mapping of name -> {from, type, required}" % where)
        return inputs
    for name, spec in raw.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append("%s: invalid input name %r" % (where, name))
            continue
        if isinstance(spec, str):
            spec = {"from": spec}
        if not isinstance(spec, Mapping) or set(spec) - {"from", "type", "required", "description", "view"}:
            errors.append("%s: input %r must be a source string or {from, type, required, description, view}" % (
                where, name))
            continue
        source = spec.get("from")
        if not isinstance(source, str) or source.count(".") < 1:
            errors.append("%s: input %r needs 'from' as 'graph.inputs.<name>' or '<node>.<output>'" % (where, name))
            continue
        type_name = spec.get("type", "any")
        if type_name not in VALUE_TYPES:
            errors.append("%s: input %r has unknown type %r" % (where, name, type_name))
        required = spec.get("required", True)
        if not isinstance(required, bool):
            errors.append("%s: input %r required must be a boolean" % (where, name))
        entry = {"from": source, "type": type_name, "required": required}
        if isinstance(spec.get("description"), str):
            entry["description"] = spec["description"]
        if "view" in spec:
            from .views import view_errors

            problems = view_errors(spec["view"], "%s: input %r" % (where, name))
            errors.extend(problems)
            if not problems:
                entry["view"] = json.loads(canonical_json(dict(spec["view"])))
        inputs[name] = entry
    return inputs


def _normalize_gates(raw: Any, where: str, errors: list[str]) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    if raw is None:
        return gates
    if not isinstance(raw, list):
        errors.append("%s: gates must be a list" % where)
        return gates
    seen = set()
    for index, gate in enumerate(raw):
        gwhere = "%s.gates[%d]" % (where, index)
        if not isinstance(gate, Mapping) or set(gate) - {"id", "type", "owner", "description"}:
            errors.append("%s: gate must be {id, type, owner, description}" % gwhere)
            continue
        gate_id = gate.get("id")
        if not isinstance(gate_id, str) or not NODE_ID_RE.match(gate_id):
            errors.append("%s: invalid gate id %r" % (gwhere, gate_id))
            continue
        if gate_id in seen:
            errors.append("%s: duplicate gate id %r" % (gwhere, gate_id))
        seen.add(gate_id)
        if gate.get("type") not in GATE_TYPES:
            errors.append("%s: gate type must be one of %s" % (gwhere, ", ".join(GATE_TYPES)))
        owner = gate.get("owner", "operator")
        if not isinstance(owner, str) or not NAME_RE.match(owner):
            errors.append("%s: invalid gate owner %r" % (gwhere, owner))
        gates.append({
            "id": gate_id,
            "type": gate.get("type"),
            "owner": owner,
            "description": gate.get("description", "") if isinstance(gate.get("description"), str) else "",
        })
    return gates


def _normalize_node(raw: Any, index: int, errors: list[str]) -> dict[str, Any] | None:
    where = "nodes[%d]" % index
    if not isinstance(raw, Mapping):
        errors.append("%s: node must be a mapping" % where)
        return None
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
        errors.append("%s: id must match %s" % (where, NODE_ID_RE.pattern))
        return None
    where = "node %s" % node_id
    unknown = sorted(set(raw) - _NODE_FIELDS)
    if unknown:
        errors.append("%s: unknown field(s) %s" % (where, ", ".join(unknown)))

    executor = raw.get("executor")
    if not isinstance(executor, str) or not executor:
        errors.append("%s: executor is required" % where)
        executor = ""
    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        errors.append("%s: purpose is required" % where)
    name = raw.get("name", node_id)
    if not isinstance(name, str) or not name.strip():
        errors.append("%s: name must be a non-empty string" % where)

    depends_on = raw.get("depends_on", [])
    if not _string_list(depends_on):
        errors.append("%s: depends_on must be a list of node ids" % where)
        depends_on = []
    elif len(set(depends_on)) != len(depends_on):
        errors.append("%s: depends_on contains duplicates" % where)

    requires = raw.get("requires", list(_DEFAULT_REQUIRES.get(executor, [])))
    if not _string_list(requires):
        errors.append("%s: requires must be a list of capability ids" % where)
        requires = []
    for capability in requires:
        if capability not in CAPABILITIES:
            errors.append("%s: unknown capability %r (known: %s)" % (where, capability, ", ".join(sorted(CAPABILITIES))))

    outputs = _normalize_outputs(raw.get("outputs"), where, errors)
    inputs = _normalize_inputs(raw.get("inputs"), where, errors)

    criteria = raw.get("success_criteria", [])
    if not isinstance(criteria, list):
        errors.append("%s: success_criteria must be a list" % where)
        criteria = []
    for cindex, criterion in enumerate(criteria):
        errors.extend(criterion_errors(criterion, outputs, "%s.success_criteria[%d]" % (where, cindex)))

    on_failure = raw.get("on_failure", "stop")
    if on_failure not in FAILURE_POLICIES:
        errors.append("%s: on_failure must be one of %s" % (where, ", ".join(FAILURE_POLICIES)))

    retry = raw.get("retry", {})
    max_attempts = 1
    if not isinstance(retry, Mapping) or set(retry) - {"max_attempts"}:
        errors.append("%s: retry must be {max_attempts}" % where)
    else:
        max_attempts = retry.get("max_attempts", 1)
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= MAX_ATTEMPTS:
            errors.append("%s: retry.max_attempts must be an integer 1..%d" % (where, MAX_ATTEMPTS))
            max_attempts = 1

    side_effects = raw.get("side_effects", False)
    idempotent = raw.get("idempotent", executor == "builtin")
    for flag_name, flag in (("side_effects", side_effects), ("idempotent", idempotent)):
        if not isinstance(flag, bool):
            errors.append("%s: %s must be a boolean" % (where, flag_name))

    gates = _normalize_gates(raw.get("gates"), where, errors)
    owner = raw.get("owner", "runtime" if executor == "builtin" else "agent")
    if not isinstance(owner, str) or not NAME_RE.match(owner):
        errors.append("%s: invalid owner %r" % (where, owner))
    boundary = raw.get("rollback_boundary")
    if boundary is not None and (not isinstance(boundary, str) or not NAME_RE.match(boundary)):
        errors.append("%s: invalid rollback_boundary %r" % (where, boundary))

    risk = raw.get("risk")
    if risk is not None and risk not in RISK_CLASS_NAMES:
        errors.append("%s: risk must be one of %s" % (where, ", ".join(RISK_CLASS_NAMES)))
    evidence = raw.get("evidence")
    if evidence is not None:
        from .evidence import evidence_errors

        errors.extend(evidence_errors(evidence, "%s.evidence" % where, outputs))

    operation = raw.get("operation")
    instructions = raw.get("instructions", "")
    if not isinstance(instructions, str):
        errors.append("%s: instructions must be a string" % where)
        instructions = ""
    if executor == "builtin":
        errors.extend(operation_errors(operation, where))
        if isinstance(operation, Mapping) and operation.get("op") in BUILTIN_OPERATIONS:
            produced = BUILTIN_OPERATIONS[operation["op"]][2]
            for out_name in outputs:
                if out_name not in produced:
                    errors.append("%s: operation %s does not produce output %r" % (where, operation["op"], out_name))
        if "value" not in inputs and not (isinstance(operation, Mapping) and operation.get("op") == "verify"):
            errors.append("%s: builtin nodes need an input named 'value'" % where)
        if side_effects is True:
            errors.append("%s: builtin operations are pure and cannot declare side_effects" % where)
    elif operation is not None:
        errors.append("%s: operation is only valid for the builtin executor" % where)

    # governance invariants
    if max_attempts > 1 and (idempotent is not True or side_effects is True):
        errors.append("%s: retries require idempotent: true and side_effects: false" % where)
    if side_effects is True:
        if not any(gate.get("type") == "approval" for gate in gates):
            errors.append("%s: nodes with side_effects need an approval gate" % where)
        if boundary is None:
            errors.append("%s: nodes with side_effects need a rollback_boundary" % where)

    normalized = {
        "id": node_id,
        "name": name if isinstance(name, str) else node_id,
        "purpose": purpose if isinstance(purpose, str) else "",
        "depends_on": list(depends_on),
        "executor": executor,
        "requires": list(requires),
        "operation": dict(operation) if isinstance(operation, Mapping) else None,
        "instructions": instructions,
        "inputs": inputs,
        "outputs": outputs,
        "success_criteria": [dict(c) for c in criteria if isinstance(c, Mapping)],
        "on_failure": on_failure,
        "retry": {"max_attempts": max_attempts},
        "idempotent": idempotent,
        "side_effects": side_effects,
        "gates": gates,
        "owner": owner,
        "rollback_boundary": boundary,
    }
    # optional fields enter the normalized spec (and its digest) only when declared, so specs written
    # for 1.x keep their digest
    if risk is not None:
        normalized["risk"] = risk
    if evidence:
        normalized["evidence"] = json.loads(canonical_json(list(evidence))) if _is_json_value(evidence) else []
    return normalized


def find_cycle(nodes: list[Mapping[str, Any]]) -> list[str] | None:
    """Return one dependency cycle as ``[a, b, ..., a]`` or ``None``. Deterministic."""
    deps = {node["id"]: list(node["depends_on"]) for node in nodes}
    white, grey, black = 0, 1, 2
    color = {node_id: white for node_id in deps}
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        color[node_id] = grey
        stack.append(node_id)
        for dep in deps[node_id]:
            if dep not in color:
                continue
            if color[dep] == grey:
                return stack[stack.index(dep):] + [dep]
            if color[dep] == white:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node_id] = black
        return None

    for node_id in deps:
        if color[node_id] == white:
            found = visit(node_id)
            if found:
                return found
    return None


def topological_order(nodes: list[Mapping[str, Any]]) -> tuple[str, ...]:
    """Kahn's algorithm; ties broken by declaration order. Assumes no cycle."""
    position = {node["id"]: index for index, node in enumerate(nodes)}
    remaining = {node["id"]: set(node["depends_on"]) for node in nodes}
    order: list[str] = []
    while remaining:
        ready = sorted((nid for nid, deps in remaining.items() if not deps), key=position.__getitem__)
        if not ready:
            raise GraphEngineeringError(ErrorCode.DEPENDENCY_CYCLE, "dependency cycle detected")
        current = ready[0]
        order.append(current)
        del remaining[current]
        for deps in remaining.values():
            deps.discard(current)
    return tuple(order)


def ancestors(nodes: list[Mapping[str, Any]], node_id: str) -> set[str]:
    deps = {node["id"]: node["depends_on"] for node in nodes}
    seen: set[str] = set()
    pending = list(deps.get(node_id, []))
    while pending:
        current = pending.pop()
        if current in seen or current not in deps:
            continue
        seen.add(current)
        pending.extend(deps[current])
    return seen


def admit(raw: Any, catalog: ExecutorCatalog | None = None) -> GraphSpec:
    """Validate and normalize a raw spec. Raises GraphEngineeringError on any problem."""
    catalog = catalog or ExecutorCatalog()
    if isinstance(raw, str):
        raw = load_spec_text(raw)
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "spec must be a mapping", {"errors": ["spec must be a mapping"]})
    unknown = sorted(set(raw) - _GRAPH_FIELDS)
    if unknown:
        errors.append("unknown top-level field(s) %s" % ", ".join(unknown))
    if raw.get("schema_version") != SPEC_SCHEMA_VERSION:
        errors.append("schema_version must be %d" % SPEC_SCHEMA_VERSION)
    graph_id = raw.get("graph_id")
    if not isinstance(graph_id, str) or not GRAPH_ID_RE.match(graph_id):
        errors.append("graph_id must match %s" % GRAPH_ID_RE.pattern)
    title = raw.get("title", graph_id if isinstance(graph_id, str) else "")
    if not isinstance(title, str):
        errors.append("title must be a string")
        title = ""
    description = raw.get("description", "")
    if not isinstance(description, str):
        errors.append("description must be a string")
        description = ""
    graph_inputs = raw.get("inputs", {})
    if not isinstance(graph_inputs, Mapping) or not all(isinstance(k, str) for k in graph_inputs):
        errors.append("inputs must be a mapping of name -> JSON value")
        graph_inputs = {}
    elif not _is_json_value(dict(graph_inputs)):
        errors.append("inputs must contain JSON values only")

    acceptance = raw.get("acceptance")
    if acceptance is not None:
        from .evidence import evidence_errors

        errors.extend(evidence_errors(acceptance, "acceptance"))
    require_evidence = raw.get("require_evidence", False)
    if not isinstance(require_evidence, bool):
        errors.append("require_evidence must be a boolean")

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("nodes must be a non-empty list")
        raw_nodes = []
    if len(raw_nodes) > MAX_NODES:
        errors.append("at most %d nodes are allowed" % MAX_NODES)
    nodes = []
    for index, raw_node in enumerate(raw_nodes):
        node = _normalize_node(raw_node, index, errors)
        if node is not None:
            nodes.append(node)

    ids = [node["id"] for node in nodes]
    duplicates = sorted({nid for nid in ids if ids.count(nid) > 1})
    if duplicates:
        errors.append("duplicate node id(s) %s" % ", ".join(duplicates))
    known = set(ids)
    for node in nodes:
        for dep in node["depends_on"]:
            if dep not in known:
                errors.append("node %s: depends_on references unknown node %r" % (node["id"], dep))
    if errors:
        raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "graph spec rejected (%d problem(s))" % len(errors),
                                    {"errors": errors})

    cycle = find_cycle(nodes)
    if cycle:
        raise GraphEngineeringError(ErrorCode.DEPENDENCY_CYCLE, "dependency cycle: %s" % " -> ".join(cycle),
                                    {"cycle": cycle})

    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        upstream = ancestors(nodes, node["id"])
        for input_name, spec in node["inputs"].items():
            source = spec["from"]
            if source.startswith("graph.inputs."):
                key = source[len("graph.inputs."):]
                if key not in graph_inputs and spec["required"]:
                    errors.append("node %s: input %r needs missing graph input %r" % (node["id"], input_name, key))
                continue
            src_node, _, src_output = source.partition(".")
            if src_node not in by_id:
                errors.append("node %s: input %r references unknown node %r" % (node["id"], input_name, src_node))
            elif src_node not in upstream:
                errors.append("node %s: input %r reads %r which is not an upstream dependency" % (
                    node["id"], input_name, src_node))
            elif src_output not in by_id[src_node]["outputs"]:
                errors.append("node %s: input %r references undeclared output %r" % (node["id"], input_name, source))
    if errors:
        raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "graph spec rejected (%d problem(s))" % len(errors),
                                    {"errors": errors})

    unavailable = []
    for node in nodes:
        try:
            executor = catalog.get(node["executor"])
        except GraphEngineeringError as exc:
            unavailable.append("node %s: %s" % (node["id"], exc.message))
            continue
        missing = sorted(set(node["requires"]) - set(executor.capabilities))
        if missing:
            unavailable.append("node %s: executor %s lacks required capability %s" % (
                node["id"], node["executor"], ", ".join(missing)))
    if unavailable:
        raise GraphEngineeringError(ErrorCode.EXECUTOR_UNAVAILABLE, "graph needs unavailable executors",
                                    {"errors": unavailable})

    data = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "graph_id": graph_id,
        "title": title,
        "description": description,
        "inputs": json.loads(canonical_json(dict(graph_inputs))),
        "nodes": nodes,
    }
    if acceptance:
        data["acceptance"] = json.loads(canonical_json(list(acceptance)))
    if require_evidence is True:
        data["require_evidence"] = True
    return GraphSpec(data=data, order=topological_order(nodes), digest=digest_of(data))
