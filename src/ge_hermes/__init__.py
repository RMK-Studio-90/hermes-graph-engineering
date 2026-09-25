"""ge_hermes - thin Hermes Agent binding for ge_runtime.

The binding is the only place that talks to Hermes, and it does so exclusively
through the ``PluginContext`` passed to ``register(ctx)``; it never imports
Hermes internals.

Registered surface:

* slash commands ``/ge`` (autopilot entry point) and ``/ge-*`` (operator entry
  points, including all approvals and other human decisions)
* tool ``ge_graph`` in toolset ``graph_engineering`` (agent entry point)
* skill ``hermes-graph-engineering:graph-engineering`` (usage guide for the agent)
* hooks ``subagent_start``, ``pre_tool_call`` (worker isolation and risk-class tool
  guard) and ``post_llm_call`` (autopilot continuation across agent turns)

Run state lives in the plugin's profile-scoped data directory provided by
Hermes (``ctx.state.data_dir``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ge_runtime import __version__

from .commands import CommandSet
from .host import REGISTRY, HermesHostExecutor
from .service import GraphService
from .tool import TOOL_NAME, TOOL_SCHEMA, TOOLSET, make_tool_handler

PLUGIN_NAME = "hermes-graph-engineering"
SKILL_NAME = "graph-engineering"
RESOURCES = Path(__file__).resolve().parent / "resources"

DISCLAIMER = (
    "This is an independent community project. "
    "It is not affiliated with, endorsed by, or sponsored by Nous Research."
)

# Hyphenated because the Hermes gateway looks up plugin commands with "_" replaced by "-".
COMMANDS = (
    "ge",
    "ge-auto",
    "ge-analyze",
    "ge-create",
    "ge-validate",
    "ge-plan",
    "ge-approve",
    "ge-run",
    "ge-status",
    "ge-verify",
    "ge-resume",
    "ge-explain",
    "ge-submit",
    "ge-retry",
    "ge-reclaim",
    "ge-cancel",
)

logger = logging.getLogger("ge_hermes")

# What the most recent register() call achieved; inspected by diagnostics and tests.
LAST_REGISTRATION: dict[str, Any] = {}

__all__ = [
    "COMMANDS",
    "DISCLAIMER",
    "LAST_REGISTRATION",
    "PLUGIN_NAME",
    "TOOL_NAME",
    "TOOLSET",
    "__version__",
    "build_service",
    "register",
]


def build_service(ctx: Any) -> GraphService:
    """Bind state to the plugin's profile-scoped data directory, resolved once at registration."""
    data_dir = Path(ctx.state.data_dir)
    injector = getattr(ctx, "inject_message", None)
    return GraphService(data_dir, ctx.get_config, host_executor=HermesHostExecutor(ctx),
                        injector=injector if callable(injector) else None)


def _autonomous_snapshot(service: GraphService) -> dict[str, Any]:
    try:
        return service.autonomous_status()
    except Exception:  # invalid configuration is reported by the commands, never by registration
        return {"enabled": None, "host_supported": service.host_supported()}


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    service = build_service(ctx)
    try:
        service.config()
    except Exception as exc:  # commands keep reporting PLUGIN_CONFIGURATION_INVALID until fixed
        logger.warning("hermes-graph-engineering configuration invalid: %s", exc)

    registered, refused = [], []
    for name, handler, description, hint in CommandSet(service).handlers():
        handle = ctx.register_command(name, handler, description=description, args_hint=hint)
        # Hermes returns None when a name collides with a built-in command; never assume success.
        (registered if handle is not None else refused).append(name)
    if refused:
        logger.warning("hermes-graph-engineering: commands refused by Hermes (name collision): %s", ", ".join(refused))

    tool_handle = ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=TOOL_SCHEMA,
        handler=make_tool_handler(service),
        description="Build, run and verify Graph Engineering execution graphs.",
    )

    skill_registered = False
    skill_path = RESOURCES / "skills" / SKILL_NAME / "SKILL.md"
    try:
        ctx.register_skill(SKILL_NAME, skill_path,
                           description="How to use Graph Engineering graphs, work orders and gates from Hermes.")
        skill_registered = True
    except Exception as exc:
        logger.warning("hermes-graph-engineering: skill not registered: %s", exc)

    hooks = []
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        # public hook API: link worker sessions to nodes and enforce each node's risk class at call time
        for hook, callback in (("subagent_start", REGISTRY.on_subagent_start),
                               ("pre_tool_call", REGISTRY.on_pre_tool_call),
                               ("post_llm_call", service.on_turn_finished)):
            try:
                register_hook(hook, callback)
                hooks.append(hook)
            except Exception as exc:
                logger.warning("hermes-graph-engineering: hook %s not registered: %s", hook, exc)

    cli_command = None
    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        from . import cli

        try:
            register_cli("ge", "Graph Engineering headless autopilot (plan, execute, verify without a chat)",
                         cli.setup, cli.make_handler(service),
                         description="Run tasks as Graph Engineering autopilot runs from cron, CI or containers.")
            cli_command = "ge"
        except Exception as exc:
            logger.warning("hermes-graph-engineering: CLI command not registered: %s", exc)

    LAST_REGISTRATION.clear()
    LAST_REGISTRATION.update({
        "version": __version__,
        "commands": registered,
        "refused_commands": refused,
        "tool": TOOL_NAME if tool_handle is not None else None,
        "skill": SKILL_NAME if skill_registered else None,
        "data_dir": str(service.data_dir),
        "hooks": hooks,
        "cli_command": cli_command,
        "autonomous_agent_execution": _autonomous_snapshot(service),
    })
