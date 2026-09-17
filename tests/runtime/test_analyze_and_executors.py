"""Task analysis, builtin operations, contracts and success criteria."""
import pytest

from ge_runtime.adapters.types import ExecutionRequest, ExecutorOutcome
from ge_runtime.analyze import analyze_task, classify, split_steps
from ge_runtime.contracts import criterion_errors, evaluate_criterion, output_contract_errors
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.executors import AgentExecutor, BuiltinExecutor, ExecutorCatalog, operation_errors

SMOKE = "Create an execution graph for: 1. read input 2. validate input 3. transform input 4. verify result"


def test_numbered_inline_steps_become_a_linear_graph():
    result = analyze_task(SMOKE)
    nodes = result["spec"]["nodes"]
    assert [n["id"] for n in nodes] == ["read_input", "validate_input", "transform_input", "verify_result"]
    assert [n["depends_on"] for n in nodes] == [[], ["read_input"], ["validate_input"], ["transform_input"]]
    assert result["spec"]["title"] == "Create an execution graph for"
    assert all(n["executor"] == "agent" for n in nodes)
    assert nodes[1]["inputs"]["previous"]["from"] == "read_input.result"
    verify = nodes[3]
    assert verify["requires"] == ["agent.review"]
    assert {"output": "passed", "check": "true"} in verify["success_criteria"]


def test_analysis_is_deterministic():
    assert analyze_task(SMOKE)["spec_digest"] == analyze_task(SMOKE)["spec_digest"]


@pytest.mark.parametrize("text, steps", [
    ("- fetch data\n- summarize data", ["fetch data", "summarize data"]),
    ("research options; then write report", ["research options", "write report"]),
    ("lies die Datei und dann fasse sie zusammen", ["lies die Datei", "fasse sie zusammen"]),
    ("single step only", ["single step only"]),
])
def test_step_splitting(text, steps):
    assert split_steps(text)[1] == steps


def test_side_effect_steps_get_gate_boundary_and_no_retry():
    spec = analyze_task("1. build the release 2. deploy to production")["spec"]
    deploy = spec["nodes"][1]
    assert deploy["side_effects"] is True and deploy["idempotent"] is False
    assert deploy["gates"][0]["type"] == "approval" and deploy["rollback_boundary"]
    assert spec["nodes"][0]["side_effects"] is False


def test_keyword_signals_match_word_starts_only():
    assert classify("run the tests")["verification"] is True
    assert classify("use the latest version")["verification"] is False
    assert classify("add a prefix")["code"] is False


def test_duplicate_step_names_get_unique_ids():
    ids = [n["id"] for n in analyze_task("1. check data 2. check data")["spec"]["nodes"]]
    assert ids == ["check_data", "check_data_2"]


def test_empty_task_rejected():
    with pytest.raises(GraphEngineeringError) as info:
        analyze_task("  ")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def _request(op, value, outputs, **args):
    return ExecutionRequest(run_id="r", node_id="n", attempt_id="a", execution_id="e", context_id="c",
                            node_type="builtin", identity="runtime",
                            contract={"operation": {"op": op, "args": args}, "outputs": outputs},
                            inputs={"value": value})


def test_builtin_operations():
    executor = BuiltinExecutor()
    result = executor.execute(_request("text_transform", "  a  b ", {"value": {}}, steps=["collapse_whitespace", "title"]))
    assert result.outcome is ExecutorOutcome.SUCCESS and result.outputs == {"value": "A B"}
    result = executor.execute(_request("require", "", {"valid": {}}, non_empty=True))
    assert result.outcome is ExecutorOutcome.NON_RETRYABLE_FAILURE and "empty" in result.detail
    result = executor.execute(_request("compare", 3, {"matches": {}}, expected=3.0))
    assert result.outputs == {"matches": False}  # no silent int/float coercion
    result = executor.execute(_request("measure", "one two", {"length": {}, "words": {}}))
    assert result.outputs == {"length": 7, "words": 2}
    assert executor.cancel("e") is False


def test_builtin_rejects_invalid_operation_at_runtime_too():
    result = BuiltinExecutor().execute(_request("shell", "x", {}))
    assert result.outcome is ExecutorOutcome.NON_RETRYABLE_FAILURE


def test_operation_validation_messages():
    assert operation_errors({"op": "text_transform", "args": {"steps": ["explode"]}}, "n")
    assert operation_errors({"op": "require", "args": {"pattern": "("}}, "n")
    assert operation_errors({"op": "identity", "args": {"x": 1}}, "n")
    assert operation_errors({"op": "identity"}, "n") == []


def test_agent_executor_is_deferred_and_never_executes():
    executor = AgentExecutor()
    assert executor.deferred is True and "agent.code" in executor.capabilities
    assert executor.execute(_request("identity", 1, {})).outcome is ExecutorOutcome.GOVERNANCE_BLOCKED


def test_catalog_fails_closed():
    catalog = ExecutorCatalog(disabled=["agent"])
    with pytest.raises(GraphEngineeringError):
        catalog.get("agent")
    with pytest.raises(GraphEngineeringError):
        catalog.get("nope")
    assert [e["enabled"] for e in catalog.describe()] == [False, True]


@pytest.mark.parametrize("criterion, outputs, passed", [
    ({"output": "x", "check": "non_empty"}, {"x": "  "}, False),
    ({"output": "x", "check": "non_empty"}, {"x": [1]}, True),
    ({"output": "x", "check": "equals", "value": 1}, {"x": True}, False),
    ({"output": "x", "check": "matches", "value": "[A-Z]+"}, {"x": "ABC"}, True),
    ({"output": "x", "check": "min_length", "value": 3}, {"x": "ab"}, False),
    ({"output": "x", "check": "max", "value": 10}, {"x": 10}, True),
    ({"output": "x", "check": "in", "value": ["a", "b"]}, {"x": "b"}, True),
    ({"output": "x", "check": "true"}, {"x": 1}, False),
    ({"output": "x", "check": "type", "value": "integer"}, {"x": False}, False),
    ({"output": "x", "check": "non_empty"}, {}, False),
])
def test_criteria_evaluation(criterion, outputs, passed):
    assert criterion_errors(criterion, ["x"], "t") == []
    assert evaluate_criterion(criterion, outputs)[0] is passed


def test_output_contract_is_strict():
    declared = {"a": {"type": "string", "required": True}, "b": {"type": "integer", "required": False}}
    assert output_contract_errors(declared, {"a": "x"}) == []
    assert output_contract_errors(declared, {"a": 1}) == ["output 'a' is not of type string"]
    assert output_contract_errors(declared, {"b": 1}) == ["required output 'a' missing"]
    assert output_contract_errors(declared, {"a": "x", "c": 1}) == ["undeclared output 'c'"]
    assert output_contract_errors(declared, ["a"]) == ["outputs must be an object"]
