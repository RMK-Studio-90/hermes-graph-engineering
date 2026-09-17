"""Plugin configuration, read from ``plugins.entries.<plugin>.settings`` in Hermes config.

Settings are optional; defaults are safe. Invalid values fail closed with
``PLUGIN_CONFIGURATION_INVALID`` instead of being coerced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.executors import ExecutorCatalog

DEFAULTS = {
    "require_plan_approval": True,
    "disabled_executors": [],
    "max_steps": 500,
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
}


@dataclass(frozen=True)
class PluginConfig:
    require_plan_approval: bool = True
    disabled_executors: tuple[str, ...] = ()
    max_steps: int = 500


def _invalid(message: str) -> GraphEngineeringError:
    return GraphEngineeringError(ErrorCode.PLUGIN_CONFIGURATION_INVALID, message,
                                 {"settings_path": "plugins.entries.hermes-graph-engineering.settings"})


def parse_config(get: Callable[[str, Any], Any]) -> PluginConfig:
    """Build a PluginConfig from a ``get(key, default)`` accessor (``ctx.get_config``)."""
    try:
        require = get("require_plan_approval", DEFAULTS["require_plan_approval"])
        disabled = get("disabled_executors", DEFAULTS["disabled_executors"])
        max_steps = get("max_steps", DEFAULTS["max_steps"])
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
    return PluginConfig(require_plan_approval=require, disabled_executors=tuple(disabled), max_steps=max_steps)
