"""BUILTIN_ADAPTER_DEFAULTS_IMPORT and minimal behaviour of the built-in defaults."""
import json

import pytest

from ge_runtime.adapters import protocols
from ge_runtime.adapters.registry import AdapterKind, AdapterRegistry, UnknownAdapterError, default_registry
from ge_runtime.adapters.types import (
    ApprovalBinding,
    ApprovalStatus,
    CostSource,
    ExecutionRequest,
    ExecutorOutcome,
    NotifyCategory,
    TraceWriteError,
)
from ge_runtime.states import HOLD_STATES, TERMINAL_OUTCOMES, HoldState, TerminalOutcome, is_success

PROTOCOL_FOR = {
    AdapterKind.KNOWLEDGE_GRAPH: protocols.KnowledgeGraphAdapter,
    AdapterKind.TRACE_SINK: protocols.TraceSink,
    AdapterKind.APPROVAL_PROVIDER: protocols.ApprovalProvider,
    AdapterKind.NOTIFIER: protocols.Notifier,
    AdapterKind.EXECUTOR: protocols.ExecutorAdapter,
}


def _factory_kwargs(kind, tmp_path):
    if kind is AdapterKind.TRACE_SINK:
        return {"path": tmp_path / "trace.jsonl"}
    if kind is AdapterKind.APPROVAL_PROVIDER:
        return {"directory": tmp_path / "approvals"}
    return {}


@pytest.mark.parametrize("kind", list(AdapterKind))
def test_every_kind_has_a_builtin_default_satisfying_its_protocol(kind, tmp_path):
    registry = default_registry()
    adapter = registry.create(kind, **_factory_kwargs(kind, tmp_path))
    assert isinstance(adapter, PROTOCOL_FOR[kind])
    assert registry.default_name(kind) == adapter.name


def test_registry_unknown_name_fails_closed():
    with pytest.raises(UnknownAdapterError):
        default_registry().create(AdapterKind.EXECUTOR, name="does-not-exist")


def test_registry_without_default_fails_closed():
    with pytest.raises(UnknownAdapterError):
        AdapterRegistry().create(AdapterKind.NOTIFIER)


def test_registry_rejects_duplicate_names():
    registry = default_registry()
    with pytest.raises(ValueError):
        registry.register(AdapterKind.NOTIFIER, "log", lambda: None)


# -- canonical states (O2) --------------------------------------------------------

def test_only_succeeded_counts_as_success():
    assert TERMINAL_OUTCOMES == frozenset(TerminalOutcome)
    assert {o.value for o in TerminalOutcome} == {
        "SUCCEEDED", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED", "HARD_STOPPED", "DEADLOCKED", "POLICY_DENIED",
    }
    assert {h.value for h in HoldState} >= {"WAITING_FOR_APPROVAL", "NEEDS_ATTENTION"}
    assert not ({o.value for o in TERMINAL_OUTCOMES} & {h.value for h in HOLD_STATES})
    assert [o for o in TerminalOutcome if is_success(o)] == [TerminalOutcome.SUCCEEDED]
    for hold in HoldState:
        assert not is_success(hold)


def test_executor_outcome_taxonomy():
    assert {o.value for o in ExecutorOutcome} == {
        "SUCCESS", "RETRYABLE_FAILURE", "NON_RETRYABLE_FAILURE", "TIMEOUT",
        "CANCELLED", "GOVERNANCE_BLOCKED", "EXECUTOR_UNAVAILABLE",
    }


# -- dry-run executor ---------------------------------------------------------------

def _request(**overrides):
    base = dict(
        run_id="run-1", node_id="summarize", attempt_id="a-1", execution_id="e-1",
        context_id="c-1", node_type="agentic", identity="writer",
        contract={"outputs": ["summary", "score"]}, inputs={"text": "hello"},
    )
    base.update(overrides)
    return ExecutionRequest(**base)


def test_dry_run_executor_is_deterministic_and_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor = default_registry().create(AdapterKind.EXECUTOR)
    first = executor.execute(_request())
    second = executor.execute(_request(execution_id="e-2"))
    assert first.outcome is ExecutorOutcome.SUCCESS
    assert first.outputs == second.outputs
    assert set(first.outputs) == {"summary", "score"}
    assert first.usage.cost == 0.0 and first.usage.cost_source is CostSource.ADAPTER_ATTESTED
    assert first.iterations == 0
    assert list(tmp_path.iterdir()) == []


# -- file approval provider -----------------------------------------------------------

def _binding(**overrides):
    base = dict(graph_id="g", spec_version="sha256:abc", run_id="run-1",
                node_or_transition="publish", action_digest="sha256:def")
    base.update(overrides)
    return ApprovalBinding(**base)


def _decide(directory, request_id, payload):
    (directory / ("%s.decision.json" % request_id)).write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def test_file_approval_pending_then_approved(tmp_path):
    provider = default_registry().create(AdapterKind.APPROVAL_PROVIDER, directory=tmp_path)
    binding = _binding()
    request_id = provider.request(binding)
    assert provider.poll(request_id).status is ApprovalStatus.PENDING
    _decide(tmp_path, request_id, {"status": "APPROVED", "binding_digest": binding.digest(),
                                   "decided_by": "operator"})
    decision = provider.poll(request_id)
    assert decision.status is ApprovalStatus.APPROVED
    assert decision.binding == binding


@pytest.mark.parametrize("case", ["malformed", "missing-digest", "wrong-digest", "unknown-status"])
def test_file_approval_errors_never_approve(tmp_path, case):
    provider = default_registry().create(AdapterKind.APPROVAL_PROVIDER, directory=tmp_path)
    binding = _binding()
    request_id = provider.request(binding)
    payload = {
        "malformed": "{not json",
        "missing-digest": {"status": "APPROVED"},
        "wrong-digest": {"status": "APPROVED", "binding_digest": "sha256:wrong"},
        "unknown-status": {"status": "MAYBE", "binding_digest": binding.digest()},
    }[case]
    _decide(tmp_path, request_id, payload)
    assert provider.poll(request_id).status is ApprovalStatus.ERROR


def test_file_approval_denied(tmp_path):
    provider = default_registry().create(AdapterKind.APPROVAL_PROVIDER, directory=tmp_path)
    binding = _binding()
    request_id = provider.request(binding)
    _decide(tmp_path, request_id, {"status": "DENIED", "binding_digest": binding.digest()})
    assert provider.poll(request_id).status is ApprovalStatus.DENIED


def test_file_approval_unknown_or_unsafe_request_id_is_error(tmp_path):
    provider = default_registry().create(AdapterKind.APPROVAL_PROVIDER, directory=tmp_path)
    assert provider.poll("0" * 32).status is ApprovalStatus.ERROR
    assert provider.poll("../escape").status is ApprovalStatus.ERROR


def test_binding_digest_covers_every_field():
    base = _binding()
    for field in ("graph_id", "spec_version", "run_id", "node_or_transition", "action_digest"):
        assert _binding(**{field: "changed"}).digest() != base.digest()


# -- trace sink / notifier / knowledge graph ---------------------------------------------

def test_jsonl_trace_sink_appends_lines(tmp_path):
    path = tmp_path / "t" / "trace.jsonl"
    sink = default_registry().create(AdapterKind.TRACE_SINK, path=path)
    sink.emit({"event_id": "1", "outcome": "ok"})
    sink.emit({"event_id": "2", "outcome": "ok"})
    sink.flush()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event_id"] for line in lines] == ["1", "2"]


def test_jsonl_trace_sink_write_failure_raises(tmp_path):
    blocked = tmp_path / "is-a-directory"
    blocked.mkdir()
    sink = default_registry().create(AdapterKind.TRACE_SINK, path=blocked)
    assert sink.required is True
    with pytest.raises(TraceWriteError):
        sink.emit({"event_id": "1"})


def test_logging_notifier_records(caplog):
    notifier = default_registry().create(AdapterKind.NOTIFIER)
    with caplog.at_level("INFO", logger="ge_runtime.notify"):
        result = notifier.notify(NotifyCategory.LOG_ONLY, "executor selected")
    assert result.delivered is True
    assert "executor selected" in caplog.text


def test_noop_knowledge_graph_is_optional_and_unsupported():
    kg = default_registry().create(AdapterKind.KNOWLEDGE_GRAPH)
    assert kg.required is False
    kg.register_spec({"graph_id": "g"})
    kg.register_run({"run_id": "r"})
    assert kg.impact("node").supported is False
