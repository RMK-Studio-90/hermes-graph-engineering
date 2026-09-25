"""``hermes ge ...``: the headless runner as a Hermes CLI subcommand (public ``register_cli_command``).

It drives autopilot runs of the current profile without an interactive session: every agent
node runs as its own one-shot Hermes process (``hermes -z``) restricted to the toolsets of the
node's risk class, so it works from cron, CI and containers. Exit codes follow
``ge_runtime.headless``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ge_runtime import headless

from .host import TOOLSETS_FOR_LEVEL


def default_worker_command() -> str:
    hermes = sys.argv[0] if Path(sys.argv[0]).name.startswith("hermes") else (shutil.which("hermes") or "hermes")
    return "%s -z {prompt} -t {toolsets}" % hermes


def toolsets_by_risk() -> str:
    return json.dumps({level.value: list(toolsets) for level, toolsets in TOOLSETS_FOR_LEVEL.items()})


def setup(parser: Any) -> None:
    parser.add_argument("action", choices=["run", "drive", "status", "recover"],
                        help="run: plan and execute a task; drive/status/recover: an existing run")
    parser.add_argument("target", help="the task text (run) or a run id")
    parser.add_argument("--workspace", default=None, help="working directory (default: current directory)")
    parser.add_argument("--worker-command", default=None, help="command per agent node (default: hermes -z)")


def make_handler(service: Any):
    def handle(args: Any) -> int:
        root = str(service.data_dir)
        argv = ["--root", root, "--worker-command", args.worker_command or default_worker_command(),
                "--toolsets-by-risk", toolsets_by_risk(),
                "--auto-approve-max-risk", service.config().policy.auto_approve_max.value]
        if args.action == "run":
            argv += ["start", "--workspace", args.workspace or service.config().workspace_root or os.getcwd(),
                     "--task", args.target]
        else:
            engine = service.engine()
            argv += [args.action, service.resolve_run(engine, args.target)]
        code = headless.main(argv)
        sys.exit(code)

    return handle
