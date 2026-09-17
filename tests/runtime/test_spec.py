"""Spec admission: validation, cycles, executors/capabilities, ordering, normalization."""
import copy
import json

import pytest

from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.executors import ExecutorCatalog
from ge_runtime.spec import admit, find_cycle, load_spec_text, topological_order


def builtin_node(node_id, depends_on=(), source="graph.inputs.text", **extra):
    node = {
        "id": node_id,
        "purpose": "step %s" % node_id,
        "executor": "builtin",
        "depends_on": list(depends_on),
        "operation": {"op": "identity"},
        "inputs": {"value": {"from": source, "type": "any"}},
        "outputs": {"value": "any"},
    }
    node.update(extra)
    return node


def spec(*nodes, **extra):
    data = {"schema_version": 1, "graph_id": "demo", "inputs": {"text": "hi"}, "nodes": list(nodes)}
    data.update(extra)
    return data


def chain():
    return spec(
        builtin_node("a"),
        builtin_node("b", ["a"], "a.value"),
        builtin_node("c", ["b"], "b.value"),
    )


def _error(raw, catalog=None):
    with pytest.raises(GraphEngineeringError) as info:
        admit(raw, catalog)
    return info.value


def test_valid_chain_is_admitted_with_order_and_digest():
    admitted = admit(chain())
    assert admitted.order == ("a", "b", "c")
    assert admitted.digest.startswith("sha256:")
    assert admitted.node("b")["retry"] == {"max_attempts": 1}
    assert admitted.node("a")["idempotent"] is True and admitted.node("a")["requires"] == ["compute.pure"]


def test_normalized_spec_readmits_to_the_same_digest():
    admitted = admit(chain())
    again = admit(copy.deepcopy(admitted.data))
    assert again.digest == admitted.digest and again.data == admitted.data


def test_json_and_yaml_text_are_accepted():
    as_json = json.dumps(chain())
    assert admit(as_json).order == ("a", "b", "c")
    assert load_spec_text("schema_version: 1\ngraph_id: x\n") == {"schema_version": 1, "graph_id": "x"}


@pytest.mark.parametrize("mutate, fragment", [
    (lambda s: s.update(schema_version=2), "schema_version"),
    (lambda s: s.update(graph_id="Bad Id"), "graph_id"),
    (lambda s: s.update(nodes=[]), "non-empty"),
    (lambda s: s.update(extra=1), "unknown top-level"),
    (lambda s: s["nodes"][0].update(id="1bad"), "id must match"),
    (lambda s: s["nodes"][0].update(purpose=""), "purpose is required"),
    (lambda s: s["nodes"][0].update(colour="red"), "unknown field"),
    (lambda s: s["nodes"][1].update(depends_on=["zzz"]), "unknown node"),
    (lambda s: s["nodes"].append(builtin_node("a")), "duplicate node id"),
    (lambda s: s["nodes"][0].update(outputs={"value": "text"}), "unknown type"),
    (lambda s: s["nodes"][0].update(operation={"op": "shell"}), "unknown builtin operation"),
    (lambda s: s["nodes"][0].update(success_criteria=[{"output": "nope", "check": "non_empty"}]), "undeclared output"),
    (lambda s: s["nodes"][0].update(success_criteria=[{"output": "value", "check": "equals"}]), "requires 'value'"),
    (lambda s: s["nodes"][0].update(requires=["compute.magic"]), "unknown capability"),
    (lambda s: s["nodes"][1].update(inputs={"value": {"from": "c.value"}}), "not an upstream dependency"),
    (lambda s: s["nodes"][1].update(inputs={"value": {"from": "a.missing"}}), "undeclared output"),
    (lambda s: s["nodes"][0].update(inputs={"value": {"from": "graph.inputs.none"}}), "missing graph input"),
    (lambda s: s["nodes"][0].update(side_effects=True), "cannot declare side_effects"),
    (lambda s: s["nodes"][0].update(retry={"max_attempts": 9}), "max_attempts"),
    (lambda s: s["nodes"][0].update(on_failure="ignore"), "on_failure"),
    (lambda s: s["nodes"][0].update(gates=[{"id": "g", "type": "vote"}]), "gate type"),
])
def test_invalid_specs_are_rejected_with_graph_invalid(mutate, fragment):
    raw = chain()
    mutate(raw)
    error = _error(raw)
    assert error.code is ErrorCode.GRAPH_INVALID
    assert any(fragment in item for item in error.details["errors"]), error.details["errors"]


def test_non_mapping_spec_rejected():
    assert _error(["not", "a", "mapping"]).code is ErrorCode.GRAPH_INVALID
    assert _error("   ").code is ErrorCode.GRAPH_INVALID


def test_agent_side_effects_require_gate_and_boundary():
    node = {"id": "deploy", "purpose": "deploy", "executor": "agent", "side_effects": True,
            "outputs": {"result": "string"}}
    errors = _error(spec(node)).details["errors"]
    assert any("approval gate" in e for e in errors) and any("rollback_boundary" in e for e in errors)
    node.update(gates=[{"id": "approve", "type": "approval"}], rollback_boundary="deploy_boundary")
    assert admit(spec(node)).node("deploy")["side_effects"] is True


def test_retries_require_idempotent_nodes():
    node = {"id": "work", "purpose": "work", "executor": "agent", "retry": {"max_attempts": 3},
            "outputs": {"result": "string"}}
    assert any("idempotent" in e for e in _error(spec(node)).details["errors"])
    node["idempotent"] = True
    assert admit(spec(node)).node("work")["retry"]["max_attempts"] == 3


def test_cycle_is_rejected_with_path():
    raw = chain()
    raw["nodes"][0]["depends_on"] = ["c"]
    raw["nodes"][0]["inputs"] = {"value": {"from": "graph.inputs.text"}}
    error = _error(raw)
    assert error.code is ErrorCode.DEPENDENCY_CYCLE
    cycle = error.details["cycle"]
    assert cycle[0] == cycle[-1] and set(cycle) == {"a", "b", "c"}


def test_self_dependency_is_a_cycle():
    raw = spec(builtin_node("a", ["a"]))
    assert _error(raw).code is ErrorCode.DEPENDENCY_CYCLE


def test_find_cycle_none_for_dag():
    nodes = [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}, {"id": "c", "depends_on": ["a"]}]
    assert find_cycle(nodes) is None


def test_topological_order_is_deterministic_and_respects_dependencies():
    nodes = [
        {"id": "d", "depends_on": ["b", "c"]},
        {"id": "c", "depends_on": ["a"]},
        {"id": "b", "depends_on": ["a"]},
        {"id": "a", "depends_on": []},
    ]
    order = topological_order(nodes)
    assert order == ("a", "c", "b", "d")
    for node in nodes:
        for dep in node["depends_on"]:
            assert order.index(dep) < order.index(node["id"])


def test_unknown_executor_is_rejected_as_unavailable():
    raw = spec({"id": "x", "purpose": "x", "executor": "quantum", "requires": [], "outputs": {}})
    error = _error(raw)
    assert error.code is ErrorCode.EXECUTOR_UNAVAILABLE


def test_disabled_executor_is_rejected():
    error = _error(chain(), ExecutorCatalog(disabled=["builtin"]))
    assert error.code is ErrorCode.EXECUTOR_UNAVAILABLE and "disabled" in error.details["errors"][0]


def test_missing_capability_is_rejected():
    raw = chain()
    raw["nodes"][0]["requires"] = ["agent.code"]
    error = _error(raw)
    assert error.code is ErrorCode.EXECUTOR_UNAVAILABLE
    assert "lacks required capability agent.code" in error.details["errors"][0]


def test_structural_errors_take_precedence_over_cycles():
    raw = chain()
    raw["nodes"][0]["depends_on"] = ["c"]
    raw["nodes"][2]["purpose"] = ""
    assert _error(raw).code is ErrorCode.GRAPH_INVALID


def test_readme_spec_example_is_admitted(repo_root):
    text = (repo_root / "README.md").read_text(encoding="utf-8")
    block = text.split("## Spec", 1)[1].split("```yaml", 1)[1].split("```", 1)[0]
    admitted = admit(block)
    assert admitted.order == ("collect", "publish")
    assert admitted.node("publish")["side_effects"] is True
