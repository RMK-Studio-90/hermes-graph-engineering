"""In-session slash commands (``/ge-*``).

Every handler takes the raw argument string and returns text. Handlers never
raise into Hermes: runtime errors are rendered as ``GE_ERROR <CODE>: ...`` and
unexpected exceptions as ``GE_ERROR INTERNAL``. Appending ``--json`` returns
machine-readable JSON instead of text.

Approvals, denials, retries and cancellation exist only as commands (operator
actions); the agent tool deliberately cannot perform them.

``/ge <task>`` is the autopilot entry point: it plans the task, applies the risk
policy, runs whatever it can right away and, when agent work is waiting, asks the
host for an agent turn through the public ``PluginContext.inject_message`` API.
Handlers follow the standard plugin command contract ``fn(raw_args) -> str``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ge_runtime.analyze import analyze_task
from ge_runtime.autopilot import CONTINUE, NEEDS_AGENT_TURN, render_step
from ge_runtime.engine import PLAN_TARGET, explain_view, pending_work, plan_view, status_summary
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.report import render_error, render_explain, render_plan, render_status, render_verification

from .service import GraphService
from .templates import TEMPLATES

logger = logging.getLogger("ge_hermes")

OPERATOR = "operator:slash-command"


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def _split_flag(raw: str, flag: str) -> tuple[str, bool]:
    """Remove a standalone flag without touching the spacing of the remaining text."""
    pattern = re.compile(r"(?:^|\s)%s(?=\s|$)" % re.escape(flag))
    if not pattern.search(raw):
        return raw.strip(), False
    return pattern.sub("", raw).strip(), True


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _render_autonomous(report: dict[str, Any]) -> str:
    dispatched = ", ".join("%s#%s %s" % (d["node"], d["attempt"], d.get("outcome", "?")) for d in report["dispatched"])
    line = "autonomous agent execution: %s; stopped: %s" % (dispatched or "nothing dispatched", report["stopped"])
    if report.get("error"):
        line += "\n  %s: %s" % (report["error"], report.get("message", ""))
    return line


class CommandSet:
    def __init__(self, service: GraphService) -> None:
        self.service = service

    # -- plumbing -----------------------------------------------------------------
    def _wrap(self, name: str, body: Callable[[str, bool], Any]) -> Callable[[str], str]:
        def handler(raw_args: str = "") -> Any:
            raw, as_json = _split_flag(raw_args or "", "--json")
            try:
                result = body(raw, as_json)
                return result if isinstance(result, str) else _dump(result)
            except GraphEngineeringError as exc:
                return _dump(exc.to_dict()) if as_json else render_error(exc.to_dict())
            except Exception as exc:  # never propagate into the host
                logger.exception("ge command /%s failed", name)
                error = {"ok": False, "error": "INTERNAL", "message": "%s (see Hermes logs)" % type(exc).__name__}
                return _dump(error) if as_json else render_error(error)

        handler.__name__ = "ge_command_" + name.replace("-", "_")
        return handler

    def handlers(self) -> list[tuple[str, Callable[[str], str], str, str]]:
        # standard plugin command contract: fn(raw_args) -> str
        return [(name, self._wrap(name, body), description, hint) for name, body, description, hint in self._table()]

    def _table(self) -> list[tuple[str, Callable[[str, bool], Any], str, str]]:
        return [
            ("ge", self.entry, "Graph Engineering autopilot: plan, run, verify and repair a task until it is done",
             "<task> | help | runs"),
            ("ge-auto", self.auto, "Continue an autopilot run (or put an existing run under the autopilot)",
             "<run|last>"),
            ("ge-analyze", self.analyze, "Analyze a task into a draft execution graph (creates a DRAFT run)",
             "[--preview] <task>"),
            ("ge-create", self.create, "Create a run from a spec file, inline JSON/YAML or a template",
             "--template text-pipeline [--text <text>] | <spec>"),
            ("ge-validate", self.validate, "Validate a graph spec without creating a run", "<spec>"),
            ("ge-plan", self.plan, "Show plan: nodes, dependencies, order, gates, ownership, rollback boundaries",
             "<run|last>"),
            ("ge-approve", self.approve, "Approve (or --deny) the plan or a node gate of a run",
             "<run|last> [plan|<node>:<gate>] [--deny]"),
            ("ge-run", self.run, "Execute an approved run until it finishes or waits", "<run|last>"),
            ("ge-status", self.status, "Run status: current, completed, blocked and failed nodes, gates",
             "[<run|last>]"),
            ("ge-verify", self.verify, "Verify a run against contracts, success criteria and gates", "<run|last>"),
            ("ge-resume", self.resume, "Safely resume an interrupted or waiting run", "<run|last>"),
            ("ge-explain", self.explain, "Explain why each node, dependency and gate exists", "<run|last>"),
            ("ge-submit", self.submit, "Submit JSON outputs for a node waiting on agent work",
             "<run|last> <node> <json> | <run|last> <node> --failed <reason>"),
            ("ge-retry", self.retry, "Authorize re-running a node held for operator attention", "<run|last> <node>"),
            ("ge-reclaim", self.reclaim, "Release a stale host dispatch of a node held for submission and hold it for operator attention", "<run|last> <node> [--force]"),
            ("ge-cancel", self.cancel, "Cancel a run", "<run|last>"),
        ]

    # -- commands -----------------------------------------------------------------
    def entry(self, raw: str, as_json: bool) -> Any:
        if raw.strip() in ("", "help", "runs"):
            return self.help(raw, as_json)
        run_id = self.service.start_autopilot(task=raw.strip(), created_by=OPERATOR, origin={"via": "/ge"})
        return self._autopilot_reply(run_id, as_json, started=True)

    def auto(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        engine.load(run_id)
        return self._autopilot_reply(run_id, as_json)

    def _autopilot_reply(self, run_id: str, as_json: bool, started: bool = False) -> Any:
        """Drive the run as far as possible here; agent work continues in an agent turn automatically."""
        report = self.service.drive_autopilot(run_id)
        turn = False
        if report["status"] in (NEEDS_AGENT_TURN, CONTINUE):
            turn = self.service.request_agent_turn(run_id)
        if as_json:
            return {"ok": True, "run_id": run_id, "autopilot": report, "agent_turn_requested": turn}
        lines = []
        if started:
            state, spec = self.service.engine().load(run_id)
            policy = state.get("policy") or {}
            lines.append("Graph Engineering autopilot started: %s (%d nodes, policy: %s, max risk %s)" % (
                run_id, len(spec.order), policy.get("plan_decision"), policy.get("max_risk")))
        lines.append(render_step(report))
        if report["status"] in (NEEDS_AGENT_TURN, CONTINUE):
            if turn:
                lines.append("Continuing automatically in an agent turn.")
            else:
                lines.append("Agent work is waiting. Continue with: ge_graph action=auto run_id=%s (or /ge-auto %s "
                             "inside an agent session)." % (run_id, run_id))
        return "\n".join(lines)

    def help(self, raw: str, as_json: bool) -> Any:
        engine = self.service.engine()
        runs = engine.list_runs(limit=10)
        payload = {
            "commands": [{"command": "/" + name, "usage": hint, "description": description}
                         for name, _body, description, hint in self._table()],
            "executors": engine.catalog.describe(),
            "templates": sorted(TEMPLATES),
            "require_plan_approval": engine.require_plan_approval,
            "autonomous_agent_execution": self.service.autonomous_status(),
            "recent_runs": runs,
        }
        if as_json:
            return payload
        lines = ["Graph Engineering - task graphs with dependencies, gates, contracts and verification."]
        if raw.strip() != "runs":
            for item in payload["commands"]:
                lines.append("  %s %s - %s" % (item["command"], item["usage"], item["description"]))
            lines.append("executors: %s" % ", ".join(
                "%s(%s)" % (e["name"], "enabled" if e["enabled"] else "disabled") for e in payload["executors"]))
            lines.append("templates: %s" % ", ".join(payload["templates"]))
            lines.append("plan approval required: %s" % ("yes" if payload["require_plan_approval"] else "no"))
            auto = payload["autonomous_agent_execution"]
            lines.append("autonomous agent execution: %s (host support: %s)" % (
                "on" if auto["enabled"] else "off", "yes" if auto["host_supported"] else "no"))
        lines.append("recent runs:" if runs else "recent runs: none")
        for row in runs:
            lines.append("  %s %s" % (row["run_id"], row["status"]))
        return "\n".join(lines)

    def analyze(self, raw: str, as_json: bool) -> Any:
        task, preview = _split_flag(raw, "--preview")
        engine = self.service.engine()
        result = analyze_task(task)
        spec = engine.validate(result["spec"])
        plan = plan_view(spec)
        run_id = None
        if not preview:
            run_id = engine.create(spec.data, created_by=OPERATOR)["run_id"]
        if as_json:
            return {"ok": True, "run_id": run_id, "analysis": result["analysis"], "plan": plan, "spec": spec.data}
        text = render_plan(plan, run_id)
        if run_id:
            text += "\nnext: /ge-approve %s plan, then /ge-run %s" % (run_id, run_id)
        return text

    def create(self, raw: str, as_json: bool) -> Any:
        engine = self.service.engine()
        text = raw.strip()
        if text.startswith("--template"):
            rest = text[len("--template"):].strip()
            name, _, remainder = rest.partition(" ")
            remainder = remainder.strip()
            template_text = None
            if remainder.startswith("--text"):
                template_text = _unquote(remainder[len("--text"):])
            elif remainder:
                raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "unexpected arguments after template name")
            spec_raw = self.service.spec_from_template(name, template_text)
        else:
            spec_raw = self.service.spec_from_source(text)
        state = engine.create(spec_raw, created_by=OPERATOR)
        spec = engine.validate(state["spec"])
        if as_json:
            return {"ok": True, "run_id": state["run_id"], "status": state["status"], "plan": plan_view(spec)}
        return render_plan(plan_view(spec), state["run_id"]) + "\nstatus: %s" % state["status"]

    def validate(self, raw: str, as_json: bool) -> Any:
        engine = self.service.engine()
        spec = engine.validate(self.service.spec_from_source(raw))
        plan = plan_view(spec)
        if as_json:
            return {"ok": True, "valid": True, "spec_digest": spec.digest, "plan": plan}
        return "VALID\n" + render_plan(plan)

    def _run_arg(self, raw: str) -> tuple[Any, str, list[str]]:
        tokens = raw.split()
        engine = self.service.engine()
        if not tokens:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "a run id (or 'last') is required")
        return engine, self.service.resolve_run(engine, tokens[0]), tokens[1:]

    def plan(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        state, spec = engine.load(run_id)
        plan = plan_view(spec)
        return {"ok": True, "run_id": run_id, "status": state["status"], "plan": plan} if as_json else \
            render_plan(plan, run_id) + "\nstatus: %s" % state["status"]

    def approve(self, raw: str, as_json: bool) -> Any:
        raw, deny = _split_flag(raw, "--deny")
        engine, run_id, rest = self._run_arg(raw)
        target = rest[0] if rest else PLAN_TARGET
        state = engine.decide(run_id, target, approve=not deny, decided_by=OPERATOR)
        if (state.get("autopilot") or {}).get("mode") and not as_json:
            # an autopilot run continues on its own once the operator decided
            return "%s %s\n" % ("denied" if deny else "approved", target) + self._autopilot_reply(run_id, False)
        return self._state_reply(engine, state, as_json, "%s %s" % ("denied" if deny else "approved", target))

    def run(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        state, autonomous = self.service.drive(engine, engine.execute(run_id))
        return self._state_reply(engine, state, as_json, autonomous=autonomous)

    def status(self, raw: str, as_json: bool) -> Any:
        if not raw.strip():
            engine = self.service.engine()
            runs = engine.list_runs(limit=20)
            if as_json:
                return {"ok": True, "runs": runs}
            return "\n".join(["runs:"] + ["  %s %s %s" % (r["run_id"], r["status"], r["updated_at"]) for r in runs]) \
                if runs else "no runs yet"
        engine, run_id, _ = self._run_arg(raw)
        state, spec = engine.load(run_id)
        if state["status"] in ("SUCCEEDED", "FAILED", "CANCELLED") and \
                (state.get("finalization") or {}).get("status") != "COMPLETE":
            engine.ensure_finalized(run_id)  # complete an interrupted finalization (receipt recovery)
            state, spec = engine.load(run_id)
        return self._state_reply(engine, state, as_json, spec=spec)

    def verify(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        receipt = engine.verify(run_id)
        return receipt if as_json else render_verification(receipt)

    def resume(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        state = engine.resume(run_id)
        recovered = state.get("recovered") or []
        note = "recovered: %s" % (", ".join("%s(%s)" % (r["node"], r["action"]) for r in recovered) or "nothing")
        state, autonomous = self.service.drive(engine, state)
        return self._state_reply(engine, state, as_json, note, autonomous=autonomous)

    def explain(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        _state, spec = engine.load(run_id)
        view = explain_view(spec)
        return view if as_json else render_explain(view)

    def submit(self, raw: str, as_json: bool) -> Any:
        parts = raw.strip().split(None, 2)
        if len(parts) < 3:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "usage: /ge-submit <run> <node> <json>|--failed <reason>")
        engine = self.service.engine()
        run_id = self.service.resolve_run(engine, parts[0])
        node_id, payload = parts[1], parts[2].strip()
        if payload.startswith("--failed"):
            state = engine.submit(run_id, node_id, failed=True, detail=_unquote(payload[len("--failed"):]) or "failed",
                                  submitted_by=OPERATOR, operator=True)
        else:
            try:
                outputs = json.loads(payload)
            except ValueError as exc:
                raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "outputs must be a JSON object") from exc
            state = engine.submit(run_id, node_id, outputs=outputs, submitted_by=OPERATOR, operator=True)
        return self._state_reply(engine, state, as_json, "submitted %s" % node_id)

    def retry(self, raw: str, as_json: bool) -> Any:
        engine, run_id, rest = self._run_arg(raw)
        if not rest:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "usage: /ge-retry <run> <node>")
        return self._state_reply(engine, engine.retry_node(run_id, rest[0], decided_by=OPERATOR), as_json)

    def reclaim(self, raw: str, as_json: bool) -> Any:
        raw, force = _split_flag(raw, "--force")
        engine, run_id, rest = self._run_arg(raw)
        if not rest:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "usage: /ge-reclaim <run> <node> [--force]")
        state = engine.reclaim_stale_dispatch(run_id, rest[0], decided_by=OPERATOR, force=force)
        note = "reclaimed %s (held for operator attention; use /ge-retry to re-run)" % rest[0]
        return self._state_reply(engine, state, as_json, note)

    def cancel(self, raw: str, as_json: bool) -> Any:
        engine, run_id, _ = self._run_arg(raw)
        return self._state_reply(engine, engine.cancel(run_id, decided_by=OPERATOR), as_json, "cancelled")

    def _state_reply(self, engine: Any, state: dict[str, Any], as_json: bool, note: str = "", spec: Any = None,
                     autonomous: dict[str, Any] | None = None) -> Any:
        if spec is None:
            _loaded, spec = engine.load(state["run_id"])
        summary = status_summary(state, spec)
        work = pending_work(state, spec)
        if as_json:
            payload = {"ok": True, "note": note, "status": summary, "work_orders": work}
            if autonomous is not None:
                payload["autonomous"] = autonomous
            return payload
        text = render_status(summary, work)
        if autonomous is not None:
            text += "\n" + _render_autonomous(autonomous)
        return (note + "\n" + text) if note else text
