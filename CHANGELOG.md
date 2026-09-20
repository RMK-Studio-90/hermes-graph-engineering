# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-09-20

Backward-compatible feature release. Autonomous execution is optional and off by default;
manual submission is unchanged.

### Added

- **Optional autonomous execution of `agent` nodes** (`autonomous_agent_execution`,
  default off). Graph Engineering decides *what* work exists; the running Hermes host
  decides *how* it is executed. Waiting `agent` nodes are handed to the host one at a
  time, in dependency order, and the worker's structured reply is submitted through the
  existing `submit`, which still enforces output contracts and success criteria.
- **Generic Hermes host integration** through the public `PluginContext.subagent_lifecycle`
  API, with feature detection. No provider, model, router or credential coupling: model
  and provider selection stay under the control of the user's Hermes installation.
- **Manual submission remains supported** at all times. When the host offers no agent
  execution API, or no agent turn is active, the response carries
  `HOST_EXECUTION_UNAVAILABLE`, nothing is started and the node stays
  `WAITING_FOR_SUBMISSION`.
- **Governance preserved.** Dispatch stops at an unapproved plan, at approval gates, at
  review gates and at `NEEDS_ATTENTION`. The worker never approves, denies, retries or
  cancels; retry policy applies to failed or invalid worker results as for manual submits.
- **Conservative interruption and recovery.** A dispatch claim is written to the run
  trace before the worker starts. After a crash, an interrupted idempotent node without
  side effects is dispatched again; any other node is held (`DISPATCH_INTERRUPTED`) for
  the operator.
- **Recursion and dispatch guards.** While a node is being worked on, the worker cannot
  run, resume, submit or verify graphs (`DISPATCH_IN_PROGRESS`); nested dispatch is
  refused; where the host copies its context into worker threads, the worker also cannot
  create graphs (`WORKER_CONTEXT_RESTRICTED`).
- **Configuration:** `autonomous_agent_execution`, `autonomous_node_timeout_seconds` and
  `autonomous_call_budget_seconds`; new error codes `HOST_EXECUTION_UNAVAILABLE`,
  `HOST_EXECUTION_FAILED`, `HOST_RESULT_INVALID`, `WORKER_CONTEXT_RESTRICTED`,
  `DISPATCH_IN_PROGRESS`, `DISPATCH_INTERRUPTED`.
- `scripts/hermes_probe.py --autonomous-smoke` for the real Hermes plugin loader.
- Boundary policy: one documented, file-scoped host API allowlist entry
  (`agent.subagent_lifecycle` in `ge_hermes/host.py`).

### Verification

- Verified end to end through Hermes' own plugin loader, subagent lifecycle and thread
  pool on unmodified Hermes 0.21.1 and 0.21.3 and on a 0.21.2 installation (deterministic
  stand-in for the worker's model turn).
- Verified with a live model through the host: a graph with an `agent` node was executed
  by Hermes, the reply `{"result":"HELLO GRAPH"}` was submitted automatically without a
  manual submit, the output contract validated, the run ended `SUCCEEDED` and `verify`
  returned `VERIFIED`.
- Compatibility: manual mode is verified on Hermes 0.21.0. Autonomous execution
  is verified on the tested 0.21.x releases from 0.21.1 and relies on feature detection
  elsewhere; on 0.21.0 the host does not copy context into worker threads, so 0.21.1 or
  newer is recommended.

## [1.0.0] - 2026-09-17

First public release.

### Added

- **Execution graph runtime (`ge_runtime`)**, independent of Hermes:
  - JSON/YAML graph specs with strict admission: unknown fields, dangling or
    non-upstream input references, invalid success criteria, cycles (with the cycle
    path), unknown executors and missing capabilities are rejected.
  - Deterministic topological order and parallel layers; SHA-256 spec digests.
  - Node model with purpose, dependencies, executor, required capabilities, input and
    output contracts, success criteria, failure policy (`stop`/`continue`), retry
    policy (idempotent nodes only), approval and review gates, owner and rollback
    boundary.
  - `builtin` executor with pure deterministic operations (`identity`, `require`,
    `text_transform`, `compare`, `measure`) and a deferred `agent` executor that turns
    nodes into work orders for the host agent.
  - Engine with plan approval, digest-bound gate approvals, write-ahead node records,
    output-contract and success-criteria checks, conservative resume (no automatic
    replay of non-idempotent work), cancellation and verification receipts.
  - Checksummed atomic run state, hash-chained trace, per-run OS file locks and
    `STATE_CORRUPT` detection.
  - Deterministic task analysis that splits a task description into chained nodes.
  - Stable error codes (`GRAPH_INVALID`, `DEPENDENCY_CYCLE`, `EXECUTOR_UNAVAILABLE`,
    `NODE_FAILED`, `GATE_FAILED`, `STATE_CORRUPT`, `STATE_WRITE_FAILED`,
    `PLUGIN_CONFIGURATION_INVALID`, ...); filesystem write failures carry an actionable
    hint (including the Windows path-length limit) instead of an internal error.
- **Hermes plugin (`ge_hermes`)**:
  - 14 slash commands: `/ge`, `/ge-analyze`, `/ge-create`, `/ge-validate`, `/ge-plan`,
    `/ge-approve`, `/ge-run`, `/ge-status`, `/ge-verify`, `/ge-resume`, `/ge-explain`,
    `/ge-submit`, `/ge-retry`, `/ge-cancel`, each with `--json` output. Names are
    namespaced to avoid Hermes built-ins such as `/graph` and `/plan`.
  - Agent tool `ge_graph` (toolset `graph_engineering`) for analysis, creation, runs,
    work orders, submissions, verification and explanations; approvals, retries and
    cancellation remain operator-only commands.
  - Skill `graph-engineering` describing the agent workflow.
  - Optional settings `require_plan_approval`, `disabled_executors`, `max_steps`, with
    fail-closed validation.
  - Built-in `text-pipeline` template.
  - Works from the Hermes CLI, Desktop and messaging gateway; in Telegram the
    underscore form (`/ge_create`) reaches the hyphenated command.
- **Profiles and multi-profile gateways**: run state is stored in each profile's own
  plugin data directory; a gateway serving several profiles loads identical plugin
  copies safely and refuses mismatched copies with `PLUGIN_CONFIGURATION_INVALID`.
- **Installer** (`scripts/install_plugin.py`): staged, hash-verified install,
  reinstall with backup, verify, rollback and uninstall that touch only this plugin's
  directory.
- **Quality gates**: unit, engine, binding, installer, packaging, import-boundary and
  sanitizer tests; optional tests against a real Hermes installation
  (`GE_HERMES_PYTHON`); GitHub Actions workflow for Python 3.11 and 3.14.
- Documentation: README, command reference, architecture, example graph.
