"""Agent-facing tool ``ge_graph``.

The agent can analyze tasks, create/validate graphs, execute approved runs,
read status, fetch work orders, submit node outputs, verify and explain. It
cannot approve or deny gates, authorize retries or cancel runs: those are
operator decisions available only as slash commands.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from ge_runtime.analyze import analyze_task
from ge_runtime.engine import explain_view, pending_work, plan_view, status_summary
from ge_runtime.errors import ErrorCode, GraphEngineeringError

from ge_runtime.guard import enforce_not_dispatching, enforce_worker_restrictions
from .service import GraphService

logger = logging.getLogger("ge_hermes")

TOOL_NAME = "ge_graph"
TOOLSET = "graph_engineering"
AGENT = "agent:ge_graph-tool"

ACTIONS = ("analyze", "validate", "create", "plan", "run", "status", "work", "submit", "verify", "explain",
           "resume", "list")

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Graph Engineering: turn a task into an execution graph (nodes, dependencies, gates, contracts), "
        "run approved graphs and verify them. Actions: analyze (task -> draft run), validate/create (spec), "
        "plan, run (execute an operator-approved run; when autonomous agent execution is enabled the host also "
        "executes waiting agent nodes and submits their results), status, work (work orders for nodes waiting on you), "
        "submit (outputs for a waiting node; they are checked against the node's output contract and "
        "success criteria), verify, explain, resume, list. Plan and gate approvals are operator-only "
        "(/ge-approve); ask the user to approve instead of trying to bypass it."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "task": {"type": "string", "description": "analyze: task description; numbered steps become nodes"},
            "spec": {"type": "string", "description": "validate/create: graph spec as JSON or YAML text"},
            "template": {"type": "string", "description": "create: template name instead of spec (text-pipeline)"},
            "text": {"type": "string", "description": "create with template: input text"},
            "run_id": {"type": "string", "description": "run id or 'last'"},
            "node": {"type": "string", "description": "submit: node id"},
            "outputs": {"type": "object", "description": "submit: outputs matching the node's output contract"},
            "failed": {"type": "boolean", "description": "submit: report that the node's work failed"},
            "detail": {"type": "string", "description": "submit: failure reason"},
        },
        "required": ["action"],
    },
}


def make_tool_handler(service: GraphService):
    def handler(args: Any = None, **kwargs: Any) -> str:
        try:
            params = args
            if isinstance(params, str):
                params = json.loads(params) if params.strip() else {}
            if not isinstance(params, Mapping):
                raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "arguments must be an object")
            result = _dispatch(service, params)
        except GraphEngineeringError as exc:
            result = exc.to_dict()
        except ValueError:
            result = {"ok": False, "error": ErrorCode.INVALID_ARGUMENT.value, "message": "arguments are not valid JSON"}
        except Exception as exc:  # never propagate into the agent loop
            logger.exception("ge_graph tool failed")
            result = {"ok": False, "error": "INTERNAL", "message": type(exc).__name__}
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    return handler


def _state_payload(engine: Any, state: Mapping[str, Any], autonomous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _loaded, spec = engine.load(state["run_id"])
    payload = {"ok": True, "status": status_summary(state, spec), "work_orders": pending_work(state, spec)}
    if autonomous is not None:
        payload["autonomous"] = dict(autonomous)
    return payload


def _dispatch(service: GraphService, params: Mapping[str, Any]) -> dict[str, Any]:
    action = params.get("action")
    if action not in ACTIONS:
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "unknown action %r (known: %s)" % (action, ", ".join(ACTIONS)))
    enforce_worker_restrictions(action)
    engine = service.engine()
    if action == "analyze":
        result = analyze_task(params.get("task", ""))
        state = engine.create(result["spec"], created_by=AGENT)
        spec = engine.validate(state["spec"])
        return {"ok": True, "run_id": state["run_id"], "status": state["status"], "analysis": result["analysis"],
                "plan": plan_view(spec), "next": "ask the operator to review and run /ge-approve %s plan" % state["run_id"]}
    if action in ("validate", "create"):
        if params.get("template"):
            raw = service.spec_from_template(params["template"], params.get("text"))
        else:
            raw = service.spec_from_source(params.get("spec"))
        spec = engine.validate(raw)
        if action == "validate":
            return {"ok": True, "valid": True, "spec_digest": spec.digest, "plan": plan_view(spec)}
        state = engine.create(spec.data, created_by=AGENT)
        return {"ok": True, "run_id": state["run_id"], "status": state["status"], "plan": plan_view(spec)}
    if action == "list":
        return {"ok": True, "runs": engine.list_runs(limit=20)}

    run_id = service.resolve_run(engine, params.get("run_id"))
    enforce_not_dispatching(str(service.data_dir.resolve()), run_id, action, engine.store)
    if action == "plan":
        state, spec = engine.load(run_id)
        return {"ok": True, "run_id": run_id, "status": state["status"], "plan": plan_view(spec)}
    if action == "explain":
        return {"ok": True, "run_id": run_id, "explanation": explain_view(engine.load(run_id)[1])}
    if action in ("status", "work"):
        state, spec = engine.load(run_id)
        if action == "work":
            return {"ok": True, "run_id": run_id, "work_orders": pending_work(state, spec)}
        return {"ok": True, "status": status_summary(state, spec), "work_orders": pending_work(state, spec)}
    if action == "run":
        state, autonomous = service.drive(engine, engine.execute(run_id))
        return _state_payload(engine, state, autonomous)
    if action == "resume":
        state = engine.resume(run_id)
        recovered = state.get("recovered", [])
        state, autonomous = service.drive(engine, state)
        payload = _state_payload(engine, state, autonomous)
        payload["recovered"] = recovered
        return payload
    if action == "verify":
        return {"ok": True, "receipt": engine.verify(run_id)}
    # submit
    node = params.get("node")
    if not isinstance(node, str) or not node:
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "submit requires 'node'")
    if params.get("failed"):
        state = engine.submit(run_id, node, failed=True, detail=str(params.get("detail") or "failed"), submitted_by=AGENT)
    else:
        state = engine.submit(run_id, node, outputs=params.get("outputs"), submitted_by=AGENT)
    return _state_payload(engine, state)
