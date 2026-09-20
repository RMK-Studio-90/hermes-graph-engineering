# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Optional autonomous agent execution** (`autonomous_agent_execution`, default off): the
  running Hermes host executes waiting `agent` nodes one at a time and their outputs are
  submitted through the existing `submit`, which still enforces output contracts and
  success criteria. Uses the public `PluginContext.subagent_lifecycle` API with feature
  detection; graph engineering selects no model or provider and needs no credentials.
  Stops at plan, approval and review gates and at `NEEDS_ATTENTION`; falls back to manual
  submission with `HOST_EXECUTION_UNAVAILABLE`; write-ahead dispatch claims in the run
  trace with conservative crash recovery; worker recursion protection; new settings
  `autonomous_agent_execution` and `autonomous_node_timeout_seconds`; new error codes
  `HOST_EXECUTION_UNAVAILABLE`, `HOST_EXECUTION_FAILED`, `HOST_RESULT_INVALID`,
  `WORKER_CONTEXT_RESTRICTED`, `DISPATCH_IN_PROGRESS`, `DISPATCH_INTERRUPTED`.
- `scripts/hermes_probe.py --autonomous-smoke` for the real Hermes plugin loader.
- Boundary policy: one documented, file-scoped host API allowlist entry
  (`agent.subagent_lifecycle` in `ge_hermes/host.py`).

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
