"""Plugin configuration, read from ``plugins.entries.<plugin>.settings`` in Hermes config.

Settings are optional; defaults are safe. Invalid values fail closed with
``PLUGIN_CONFIGURATION_INVALID`` instead of being coerced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ge_runtime.autopilot import AutopilotBudget
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.executors import ExecutorCatalog
from ge_runtime.policy import RISK_CLASSES, PolicyConfig, RiskClass

DEFAULTS = {
    "require_plan_approval": True,
    "disabled_executors": [],
    "max_steps": 500,
    "autonomous_agent_execution": False,
    "autonomous_node_timeout_seconds": 3600,
    "autonomous_call_budget_seconds": 300,
    "autopilot_auto_approve_max_risk": "LOCAL_EXEC",
    "autopilot_deny_risks": ["DESTRUCTIVE"],
    "autopilot_max_attempts": 30,
    "autopilot_max_repairs": 6,
    "autopilot_max_replans": 2,
    "autopilot_max_runtime_seconds": 14400,
    "autopilot_max_cost": None,
    "autopilot_continue_turns": True,
    "workspace_root": None,
}

CONFIG_SCHEMA = {
    "require_plan_approval": {
        "type": "bool",
        "default": True,
        "description": "Require an operator approval of the plan before a run executes.",
    },
    "disabled_executors": {
        "type": "list",
        "default": [],
        "description": "Executor names (builtin, agent) that must not run.",
    },
    "max_steps": {
        "type": "int",
        "default": 500,
        "description": "Maximum node attempts started per run/resume call.",
    },
    "autonomous_agent_execution": {
        "type": "bool",
        "default": False,
        "description": ("Let the running Hermes host execute waiting agent nodes and submit their results "
                        "(sequentially). Off: agent nodes wait for a manual submit."),
    },
    "autonomous_node_timeout_seconds": {
        "type": "int",
        "default": 3600,
        "description": "Upper bound for one autonomous agent node before the host worker is cancelled.",
    },
    "autonomous_call_budget_seconds": {
        "type": "int",
        "default": 300,
        "description": ("Stop starting further agent nodes in one run/resume/auto call after this many seconds, so "
                        "the call returns before the host's own tool-call deadline; the autopilot continues."),
    },
    "autopilot_auto_approve_max_risk": {
        "type": "str",
        "default": "LOCAL_EXEC",
        "description": "Highest risk class the autopilot approves without an operator (READ_ONLY..NETWORK).",
    },
    "autopilot_deny_risks": {
        "type": "list",
        "default": ["DESTRUCTIVE"],
        "description": "Risk classes the autopilot never executes (not even after an operator approval).",
    },
    "autopilot_max_attempts": {"type": "int", "default": 30, "description": "Node attempts per autopilot run."},
    "autopilot_max_repairs": {"type": "int", "default": 6, "description": "Repairs per autopilot run."},
    "autopilot_max_replans": {"type": "int", "default": 2, "description": "Plan revisions per autopilot run."},
    "autopilot_max_runtime_seconds": {"type": "int", "default": 14400,
                                      "description": "Wall-clock limit of an autopilot run since its start."},
    "autopilot_max_cost": {"type": "number", "default": None,
                           "description": "Limit on the host-reported cost of an autopilot run (none: no limit)."},
    "autopilot_continue_turns": {
        "type": "bool",
        "default": True,
        "description": ("Continue an unfinished autopilot run in a new agent turn automatically (public "
                        "inject_message API) instead of waiting for the user."),
    },
    "workspace_root": {
        "type": "str",
        "default": None,
        "description": "Directory autopilot runs work in and check evidence against (default: Hermes' cwd).",
    },
}


@dataclass(frozen=True)
class PluginConfig:
    require_plan_approval: bool = True
    disabled_executors: tuple[str, ...] = ()
    max_steps: int = 500
    autonomous_agent_execution: bool = False
    autonomous_node_timeout_seconds: int = 3600
    autonomous_call_budget_seconds: int = 300
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    budget: AutopilotBudget = field(default_factory=AutopilotBudget)
    autopilot_continue_turns: bool = True
    workspace_root: str | None = None


def _invalid(message: str) -> GraphEngineeringError:
    return GraphEngineeringError(ErrorCode.PLUGIN_CONFIGURATION_INVALID, message,
                                 {"settings_path": "plugins.entries.hermes-graph-engineering.settings"})


def parse_config(get: Callable[[str, Any], Any]) -> PluginConfig:
    """Build a PluginConfig from a ``get(key, default)`` accessor (``ctx.get_config``)."""
    try:
        require = get("require_plan_approval", DEFAULTS["require_plan_approval"])
        disabled = get("disabled_executors", DEFAULTS["disabled_executors"])
        max_steps = get("max_steps", DEFAULTS["max_steps"])
        autonomous = get("autonomous_agent_execution", DEFAULTS["autonomous_agent_execution"])
        node_timeout = get("autonomous_node_timeout_seconds", DEFAULTS["autonomous_node_timeout_seconds"])
        call_budget = get("autonomous_call_budget_seconds", DEFAULTS["autonomous_call_budget_seconds"])
        auto = {key: get(key, DEFAULTS[key]) for key in DEFAULTS if key.startswith("autopilot_")}
        workspace = get("workspace_root", DEFAULTS["workspace_root"])
    except Exception as exc:
        raise _invalid("plugin settings could not be read (%s)" % type(exc).__name__) from exc
    if not isinstance(require, bool):
        raise _invalid("require_plan_approval must be true or false")
    if disabled is None:
        disabled = []
    if not isinstance(disabled, list) or not all(isinstance(name, str) for name in disabled):
        raise _invalid("disabled_executors must be a list of executor names")
    known = set(ExecutorCatalog().names())
    unknown = sorted(set(disabled) - known)
    if unknown:
        raise _invalid("disabled_executors names unknown executor(s) %s (known: %s)" % (
            ", ".join(unknown), ", ".join(sorted(known))))
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 10_000:
        raise _invalid("max_steps must be an integer between 1 and 10000")
    if not isinstance(autonomous, bool):
        raise _invalid("autonomous_agent_execution must be true or false")
    if not isinstance(node_timeout, int) or isinstance(node_timeout, bool) or not 10 <= node_timeout <= 86_400:
        raise _invalid("autonomous_node_timeout_seconds must be an integer between 10 and 86400")
    if not isinstance(call_budget, int) or isinstance(call_budget, bool) or not 10 <= call_budget <= 86_400:
        raise _invalid("autonomous_call_budget_seconds must be an integer between 10 and 86400")
    ceiling = auto["autopilot_auto_approve_max_risk"]
    if ceiling not in RISK_CLASSES or RiskClass(ceiling).rank > RiskClass.NETWORK.rank:
        raise _invalid("autopilot_auto_approve_max_risk must be one of READ_ONLY, WORKSPACE_WRITE, LOCAL_EXEC, NETWORK")
    deny = auto["autopilot_deny_risks"]
    if deny is None:
        deny = []
    if not isinstance(deny, list) or not all(r in RISK_CLASSES for r in deny):
        raise _invalid("autopilot_deny_risks must be a list of risk classes (%s)" % ", ".join(RISK_CLASSES))
    limits = {}
    for key, low, high in (("autopilot_max_attempts", 1, 1000), ("autopilot_max_repairs", 0, 100),
                           ("autopilot_max_replans", 0, 20), ("autopilot_max_runtime_seconds", 60, 7 * 86_400)):
        value = auto[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise _invalid("%s must be an integer between %d and %d" % (key, low, high))
        limits[key] = value
    max_cost = auto["autopilot_max_cost"]
    if max_cost is not None and (isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or max_cost < 0):
        raise _invalid("autopilot_max_cost must be a non-negative number or null")
    if not isinstance(auto["autopilot_continue_turns"], bool):
        raise _invalid("autopilot_continue_turns must be true or false")
    if workspace is not None and (not isinstance(workspace, str) or not workspace.strip()):
        raise _invalid("workspace_root must be a directory path or null")
    policy = PolicyConfig(auto_approve_max=RiskClass(ceiling), deny=frozenset(RiskClass(r) for r in deny))
    budget = AutopilotBudget(max_attempts_total=limits["autopilot_max_attempts"],
                             max_repairs=limits["autopilot_max_repairs"], max_replans=limits["autopilot_max_replans"],
                             max_runtime_seconds=limits["autopilot_max_runtime_seconds"],
                             max_cost=float(max_cost) if max_cost is not None else None)
    return PluginConfig(require_plan_approval=require, disabled_executors=tuple(disabled), max_steps=max_steps,
                        autonomous_agent_execution=autonomous, autonomous_node_timeout_seconds=node_timeout,
                        autonomous_call_budget_seconds=call_budget, policy=policy, budget=budget,
                        autopilot_continue_turns=auto["autopilot_continue_turns"], workspace_root=workspace)
