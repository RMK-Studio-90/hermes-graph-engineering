"""Graph-agent isolation: an audit where every role sees only the artifacts it is meant to see,
and no finding counts as verified because an agent says so."""
import json
from pathlib import Path

import pytest

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.autopilot import TERMINAL, Autopilot
from ge_runtime.engine import GraphEngine
from ge_runtime.findings import FindingStatus, adjudicate, check_quotes, collect_findings
from ge_runtime.store import RunStore
from ge_runtime.views import apply_view, read_source_range
from ge_runtime.workflows import audit_workflow

APP = "def add(a, b):\n    return a - b  # wrong operator\n\n\ndef run(expr):\n    return eval(expr)\n"
REASONING = "SECRET-HUNTER-REASONING"


def finding(title, location, hunter):
    return {"title": title, "claim": "%s at %s" % (title, location), "location": location, "severity": "high",
            "category": hunter, "reasoning": "%s of %s: long chain of thought" % (REASONING, hunter)}


HUNTS = {
    "hunt_correctness": [finding("add subtracts", "app.py:2", "correctness"),
                         finding("phantom bug", "app.py:90-95", "correctness")],
    "hunt_security": [finding("eval on input", "app.py:6", "security"),
                      finding("made-up quote", "app.py:1-2", "security")],
    "hunt_concurrency": [finding("race in add", "app.py:1-2", "concurrency"),
                         finding("partial issue", "app.py:5-6", "concurrency"),
                         finding("nobody checked this", "app.py:1", "concurrency")],
}
VERDICTS = {  # by title
    "add subtracts": {"verdict": "CONFIRMED", "quotes": [{"line": 2, "text": "return a - b"}]},
    "phantom bug": {"verdict": "CONFIRMED", "quotes": [{"line": 92, "text": "anything"}]},  # location does not exist
    "eval on input": {"verdict": "CONFIRMED", "quotes": [{"line": 6, "text": "return eval(expr)"}]},
    "made-up quote": {"verdict": "CONFIRMED", "quotes": [{"line": 2, "text": "os.system(cmd)"}]},  # fabricated
    "race in add": {"verdict": "REJECTED", "quotes": [], "explanation": "no shared state"},
    "partial issue": {"verdict": "PARTIALLY_CONFIRMED", "quotes": [{"line": 6, "text": "eval(expr)"}]},
    # "nobody checked this" is never verified
}


class AuditWorker:
    def __init__(self):
        self.seen = {}

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        node = order["node"]
        self.seen[node] = context
        if node == "context_map":
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"context": "one module app.py"})
        if node in HUNTS:
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"findings": HUNTS[node]})
        if node == "blind_review":
            reviews = [{"id": f["id"], "assessment": "plausible", "notes": "clear"} for f in order["inputs"]["findings"]]
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"reviews": reviews})
        if node == "verify_code":
            verifications = [dict(VERDICTS[f["title"]], id=f["id"]) for f in order["inputs"]["findings"]
                             if f["title"] in VERDICTS]
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"verifications": verifications})
        if node == "synthesize":
            counts = order["inputs"]["counts"]
            return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"report": "confirmed: %d" % counts["CONFIRMED"]})
        raise AssertionError(node)


@pytest.fixture
def audited(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text(APP, encoding="utf-8")
    worker = AuditWorker()
    pilot = Autopilot(GraphEngine(RunStore(tmp_path / "store")), worker, sleep=lambda s: None)
    run_id = pilot.start(spec=audit_workflow("app.py"), workspace=str(workspace))
    report = pilot.drive(run_id)
    return pilot, run_id, worker, report


def test_the_audit_runs_autonomously_under_a_read_only_policy(audited):
    pilot, run_id, worker, report = audited
    assert report["status"] == TERMINAL and report["final_report"]["outcome"] == "SUCCEEDED"
    state, _spec = pilot.engine.load(run_id)
    assert state["policy"]["max_risk"] == "READ_ONLY" and state["approvals"]["plan"]["decided_by"].startswith("policy:")
    assert set(worker.seen) == {"context_map", "hunt_correctness", "hunt_security", "hunt_concurrency",
                                "blind_review", "verify_code", "synthesize"}


def test_every_role_gets_a_fresh_context(audited):
    _pilot, _run_id, worker, _report = audited
    correlations = {ctx.correlation_id for ctx in worker.seen.values()}
    digests = {ctx.context_digest for ctx in worker.seen.values()}
    assert len(correlations) == len(digests) == len(worker.seen) == 7


def test_the_blind_reviewer_sees_findings_but_no_code_and_no_reasoning(audited):
    _pilot, _run_id, worker, _report = audited
    text = worker.seen["blind_review"].goal + worker.seen["blind_review"].context
    assert "add subtracts" in text
    assert REASONING not in text and "return a - b" not in text and '"source"' not in text


def test_the_code_verifier_sees_source_ranges_but_no_reasoning(audited):
    _pilot, _run_id, worker, _report = audited
    text = worker.seen["verify_code"].context
    assert "return a - b" in text and "eval(expr)" in text  # the exact lines, read by the runtime
    assert REASONING not in text and "plausible" not in text  # neither hunter reasoning nor reviewer notes


def test_the_synthesizer_sees_the_ledger_only(audited):
    _pilot, _run_id, worker, _report = audited
    text = worker.seen["synthesize"].context
    assert REASONING not in text and "CONFIRMED" in text


def test_statuses_are_decided_by_verified_quotes_not_by_claims(audited):
    pilot, run_id, _worker, _report = audited
    state, _spec = pilot.engine.load(run_id)
    ledger = {e["title"]: e for e in state["nodes"]["adjudicate"]["outputs"]["ledger"]}
    assert {k: v["status"] for k, v in ledger.items()} == {
        "add subtracts": "CONFIRMED", "phantom bug": "NEEDS_MORE_EVIDENCE", "eval on input": "CONFIRMED",
        "made-up quote": "NEEDS_MORE_EVIDENCE", "race in add": "REJECTED", "partial issue": "PARTIALLY_CONFIRMED",
        "nobody checked this": "UNVERIFIED"}
    assert "not found" in ledger["made-up quote"]["reason"]
    assert "does not resolve" in ledger["phantom bug"]["reason"]
    assert all("reasoning" not in e for e in ledger.values())
    counts = state["nodes"]["adjudicate"]["outputs"]["counts"]
    assert counts["CONFIRMED"] == 2 and counts["UNVERIFIED"] == 1
    assert state["nodes"]["synthesize"]["outputs"]["report"] == "confirmed: 2"


def test_findings_start_unverified_and_quotes_are_checked_against_runtime_source(tmp_path):
    merged = collect_findings({"b": [{"claim": "x", "location": "a.py:1"}, {"claim": ""}], "a": "not a list"})
    assert merged["invalid"] == 1 and merged["findings"][0]["status"] == FindingStatus.UNVERIFIED.value
    (tmp_path / "a.py").write_text("alpha\nbeta\n", encoding="utf-8")
    source = read_source_range(str(tmp_path), "a.py:2", context_lines=0)
    assert source["text"] == "2: beta"
    assert check_quotes(source, [{"line": 2, "text": "beta"}])[:2] == (1, 1)
    assert check_quotes(source, [{"line": 1, "text": "beta"}])[:2] == (0, 1)  # right text, wrong line
    assert read_source_range(str(tmp_path), "../etc/passwd:1") is None  # never outside the workspace
    ledger = adjudicate([{"id": "F1", "location": "a.py:2", "source": source}],
                        [{"id": "F1", "verdict": "CONFIRMED", "quotes": []}])
    assert ledger["ledger"][0]["status"] == "NEEDS_MORE_EVIDENCE"  # a bare "confirmed" is not enough


def test_views_project_without_leaking(tmp_path):
    items = [{"id": "F1", "claim": "c", "location": "x.py:1", "reasoning": "secret"}]
    assert apply_view(items, {"pick": ["id", "claim"]}) == [{"id": "F1", "claim": "c"}]
    assert apply_view(items, {"omit": ["reasoning"]}) == [{"id": "F1", "claim": "c", "location": "x.py:1"}]
    (tmp_path / "x.py").write_text("line one\n", encoding="utf-8")
    projected = apply_view(items, {"pick": ["id"], "attach_source": True}, workspace=str(tmp_path))
    assert projected[0]["source"]["text"] == "1: line one" and "location" not in projected[0]
    assert items[0]["reasoning"] == "secret"  # the stored artifact is untouched
