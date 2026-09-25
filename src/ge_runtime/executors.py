"""Executor catalog: which executors exist, what they can do, and how they run.

Two executor families ship with the runtime:

``builtin``
    Deterministic, side-effect-free data operations evaluated in-process
    (no filesystem, network, subprocess or model access). Always idempotent.

``agent``
    Work performed by the host's agent. The runtime never selects a model or
    provider: it publishes the node's requirements (capabilities, contracts,
    instructions) and waits for the host to submit outputs, which are then
    validated like any other result. The host's own routing decides who does
    the work.

The catalog is explicit and fails closed: unknown or disabled executors and
unmet capabilities are rejected before anything runs.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .adapters.types import CostSource, ExecutionRequest, ExecutorOutcome, ExecutorResult, Usage
from .contracts import VALUE_TYPES, compile_pattern, is_type
from .errors import ErrorCode, GraphEngineeringError

MAX_TEXT_CHARS = 100_000

CAPABILITIES = {
    "compute.pure": "deterministic in-process data operation without side effects",
    "agent.reasoning": "analysis, judgement or planning by the host agent",
    "agent.generation": "producing text or structured content",
    "agent.tools": "using host tools (files, web, terminal) under host permissions",
    "agent.code": "reading or changing source code",
    "agent.research": "gathering external information",
    "agent.review": "reviewing or verifying produced work",
}

TEXT_STEPS = ("strip", "lower", "upper", "title", "collapse_whitespace", "reverse")


def _fail(detail: str) -> ExecutorResult:
    return ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail=detail)


def _ok(outputs: Mapping[str, Any]) -> ExecutorResult:
    return ExecutorResult(
        ExecutorOutcome.SUCCESS, outputs=dict(outputs), usage=Usage(cost=0.0, cost_source=CostSource.ADAPTER_ATTESTED)
    )


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, (list, dict)) and not value


def _op_identity(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    return _ok({"value": value})


def _op_require(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    problems = []
    type_name = args.get("type")
    if type_name is not None and not is_type(value, type_name):
        problems.append("value is not of type %s" % type_name)
    if args.get("non_empty") and _is_empty(value):
        problems.append("value is empty")
    sized = isinstance(value, (str, list, dict))
    if "min_length" in args and not (sized and len(value) >= args["min_length"]):
        problems.append("value shorter than %s" % args["min_length"])
    if "max_length" in args and not (sized and len(value) <= args["max_length"]):
        problems.append("value longer than %s" % args["max_length"])
    if "pattern" in args and not (
        isinstance(value, str) and len(value) <= MAX_TEXT_CHARS and compile_pattern(args["pattern"]).fullmatch(value)
    ):
        problems.append("value does not match pattern")
    if problems:
        return _fail("input rejected: " + "; ".join(problems))
    return _ok({"value": value, "valid": True})


def _op_text_transform(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
        return _fail("text_transform needs a string value of at most %d characters" % MAX_TEXT_CHARS)
    for step in args["steps"]:
        if step == "strip":
            value = value.strip()
        elif step == "lower":
            value = value.lower()
        elif step == "upper":
            value = value.upper()
        elif step == "title":
            value = value.title()
        elif step == "collapse_whitespace":
            value = " ".join(value.split())
        elif step == "reverse":
            value = value[::-1]
    return _ok({"value": value})


def _op_compare(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    expected = inputs["expected"] if "expected" in inputs else args["expected"]
    matches = type(value) is type(expected) and value == expected
    return _ok({"matches": matches, "value": value, "expected": expected})


def _op_measure(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    if not isinstance(value, (str, list, dict)):
        return _fail("measure needs a string, array or object value")
    words = len(value.split()) if isinstance(value, str) else 0
    return _ok({"length": len(value), "words": words})


def _op_verify(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    # a checkpoint: it computes nothing; the node's declared evidence decides whether it succeeds
    return _ok({"passed": True})


def _op_collect_findings(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    from .findings import collect_findings

    return _ok(collect_findings(dict(inputs)))


def _op_adjudicate_findings(value: Any, args: Mapping[str, Any], inputs: Mapping[str, Any]) -> ExecutorResult:
    from .findings import adjudicate

    findings = inputs.get("findings")
    if not isinstance(findings, list):
        return _fail("adjudicate_findings needs input 'findings' (array)")
    return _ok(adjudicate(findings, inputs.get("verifications") or [], inputs.get("reviews") or []))


# name -> (handler, accepted args, produced outputs {name: type})
BUILTIN_OPERATIONS = {
    "identity": (_op_identity, (), {"value": "any"}),
    "require": (_op_require, ("type", "non_empty", "min_length", "max_length", "pattern"),
                {"value": "any", "valid": "boolean"}),
    "text_transform": (_op_text_transform, ("steps",), {"value": "string"}),
    "compare": (_op_compare, ("expected",), {"matches": "boolean", "value": "any", "expected": "any"}),
    "measure": (_op_measure, (), {"length": "integer", "words": "integer"}),
    "verify": (_op_verify, (), {"passed": "boolean"}),
    "collect_findings": (_op_collect_findings, (), {"findings": "array", "invalid": "integer"}),
    "adjudicate_findings": (_op_adjudicate_findings, (), {"ledger": "array", "counts": "object"}),
}
# operations that read named inputs instead of the single 'value' input
NO_VALUE_OPERATIONS = frozenset({"verify", "collect_findings", "adjudicate_findings"})


def operation_errors(operation: Any, where: str) -> list[str]:
    """Validate a builtin operation declaration."""
    if not isinstance(operation, Mapping):
        return ["%s: builtin executor requires an 'operation' mapping" % where]
    errors = []
    unknown = sorted(set(operation) - {"op", "args"})
    if unknown:
        errors.append("%s: unknown operation field(s) %s" % (where, ", ".join(unknown)))
    op = operation.get("op")
    if op not in BUILTIN_OPERATIONS:
        known = ", ".join(sorted(BUILTIN_OPERATIONS))
        return errors + ["%s: unknown builtin operation %r (known: %s)" % (where, op, known)]
    args = operation.get("args", {})
    if not isinstance(args, Mapping):
        return errors + ["%s: operation args must be a mapping" % where]
    extra = sorted(set(args) - set(BUILTIN_OPERATIONS[op][1]))
    if extra:
        errors.append("%s: operation %s does not accept arg(s) %s" % (where, op, ", ".join(extra)))
    if op == "require":
        if "type" in args and args["type"] not in VALUE_TYPES:
            errors.append("%s: require.type must be one of %s" % (where, ", ".join(VALUE_TYPES)))
        for key in ("min_length", "max_length"):
            if key in args and not (isinstance(args[key], int) and not isinstance(args[key], bool) and args[key] >= 0):
                errors.append("%s: require.%s must be a non-negative integer" % (where, key))
        if "non_empty" in args and not isinstance(args["non_empty"], bool):
            errors.append("%s: require.non_empty must be a boolean" % where)
        if "pattern" in args:
            try:
                compile_pattern(args["pattern"])
            except ValueError as exc:
                errors.append("%s: require.%s" % (where, exc))
    elif op == "text_transform":
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps or any(step not in TEXT_STEPS for step in steps):
            errors.append("%s: text_transform.steps must be a non-empty list of %s" % (where, ", ".join(TEXT_STEPS)))
    return errors


class BuiltinExecutor:
    """Deterministic in-process executor for pure data operations."""

    name = "builtin"
    deferred = False
    capabilities = frozenset({"compute.pure"})

    def execute(self, request: ExecutionRequest) -> ExecutorResult:
        operation = request.contract.get("operation")
        errors = operation_errors(operation, request.node_id)
        if errors:
            return _fail("; ".join(errors))
        if "value" not in request.inputs and operation["op"] not in NO_VALUE_OPERATIONS:
            return _fail("builtin operation %s requires input 'value'" % operation["op"])
        args = operation.get("args", {})
        if operation["op"] == "compare" and "expected" not in request.inputs and "expected" not in args:
            return _fail("compare requires input or arg 'expected'")
        handler = BUILTIN_OPERATIONS[operation["op"]][0]
        try:
            result = handler(request.inputs.get("value"), args, request.inputs)
        except Exception as exc:  # a pure operation must never crash the run
            return _fail("operation %s raised %s" % (operation["op"], type(exc).__name__))
        declared = request.contract.get("outputs")
        if result.outcome is ExecutorOutcome.SUCCESS and isinstance(declared, Mapping):
            # an operation may produce more than the node declares; only declared outputs leave the executor
            return _ok({name: value for name, value in result.outputs.items() if name in declared})
        return result

    def cancel(self, execution_id: str) -> bool:
        return False


class AgentExecutor:
    """Deferred executor: the host agent performs the work and submits outputs."""

    name = "agent"
    deferred = True
    capabilities = frozenset(name for name in CAPABILITIES if name.startswith("agent."))

    def execute(self, request: ExecutionRequest) -> ExecutorResult:
        # Deferred executors are never invoked synchronously; outputs arrive by submission.
        return ExecutorResult(ExecutorOutcome.GOVERNANCE_BLOCKED, detail="agent work is submitted, not executed")

    def cancel(self, execution_id: str) -> bool:
        return False


class ExecutorCatalog:
    def __init__(self, executors: Iterable[Any] | None = None, disabled: Iterable[str] = ()) -> None:
        executors = list(executors) if executors is not None else [BuiltinExecutor(), AgentExecutor()]
        self._executors = {executor.name: executor for executor in executors}
        self._disabled = frozenset(disabled)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

    def get(self, name: str) -> Any:
        if name not in self._executors:
            raise GraphEngineeringError(
                ErrorCode.EXECUTOR_UNAVAILABLE, "unknown executor %r" % name, {"known": list(self.names())}
            )
        if name in self._disabled:
            raise GraphEngineeringError(ErrorCode.EXECUTOR_UNAVAILABLE, "executor %r is disabled by configuration" % name)
        return self._executors[name]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "enabled": name not in self._disabled,
                "deferred": bool(getattr(executor, "deferred", False)),
                "capabilities": sorted(executor.capabilities),
            }
            for name, executor in sorted(self._executors.items())
        ]
