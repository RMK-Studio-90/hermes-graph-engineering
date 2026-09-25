# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.0] - 2026-09-25

GE2: autonomous, crash-resistant, policy-governed and evidence-verified. `/ge <task>` (or
`ge_graph action=auto`) now drives a task to a verified terminal result without manual
plan/approve/run/resume/verify steps.

### Added

- **Autopilot** (`ge_runtime.autopilot`): ANALYZE → PLAN → POLICY → EXECUTE → VERIFY →
  DIAGNOSE → REPAIR/REPLAN → RETEST → TERMINAL. State lives in the run, so any later call,
  turn or process continues. Failure classification (TRANSIENT, CONTRACT, EVIDENCE,
  STRUCTURAL, POLICY), repair with a diagnosis artifact in a fresh worker context, backoff,
  subgraph revision (reopen the nodes a failed check is about, or insert a read-only
  diagnosis node) with policy re-approval and retest, hard budgets (attempts, repairs,
  replans, runtime, cost, drive calls; `BUDGET_EXHAUSTED`), automatic final report.
- **Planner** (`ge_runtime.planner`): task → graph with evidence extracted from the task text
  (backticked commands, created files, file contents) and a final acceptance checkpoint.
- **Risk policy** (`ge_runtime.policy`): `READ_ONLY`, `WORKSPACE_WRITE`, `LOCAL_EXEC`,
  `NETWORK`, `EXTERNAL_SIDE_EFFECT`, `PAID`, `DESTRUCTIVE`, inferred from executor,
  capabilities, evidence commands and node text (declarations raise, never lower). Safe plans
  are approved automatically by a digest-bound policy record; risky nodes hold behind
  `<node>:policy` operator gates; destructive plans are rejected (`POLICY_DENIED`).
- **Worker rights by risk class**: toolsets narrowed at launch, `pre_tool_call` allowlist
  guard, read-only workspace guard (`POLICY_VIOLATION`).
- **Deterministic evidence** (`ge_runtime.evidence`): command exit codes, files, hashes (also
  of agent-claimed hashes), git diff scope, output/file equality, graph acceptance re-checked
  at verification; checksummed evidence artifacts; verification levels
  (`DETERMINISTIC`, `EVIDENCE`, `SELF_REPORTED`) and `require_evidence`.
- **Durable kernel**: explicit run/node transition tables; state bound to the journal head
  with a `state.saved` event per revision; torn journal tails repaired and quarantined,
  damaged journals quarantined, truncation/divergence recorded as violations, rolled-back
  state refused (`STATE_DIVERGED`), lost updates reconciled; crash-safe finalization with
  automatic checksummed receipts (schema 2) and receipt recovery; directory fsync; schema
  1 → 2 migration; `StateStore` protocol with the local store as default.
- **Durable dispatch**: persistent lease (owner, pid, heartbeat, ttl, epoch) released in
  `finally`, takeover of expired/dead leases, no duplicate execution across processes,
  automatic reclaim of abandoned claims of replay-safe nodes, persisted
  `DISPATCH_INTERRUPTED` for unsafe ones; results submitted only while the lease is held.
- **Isolation and audit workflow**: input views (`pick`, `omit`, `attach_source`), the
  `audit` template (context mapper → hunters → blind reviewer → code verifier →
  adjudication → synthesizer), finding statuses `UNVERIFIED | CONFIRMED |
  PARTIALLY_CONFIRMED | REJECTED | NEEDS_MORE_EVIDENCE` decided by quotes checked against
  runtime-read source; worker context digests and child sessions journaled.
- **Headless runner** (`python -m ge_runtime.headless`, `hermes ge run|drive|status|recover`)
  with one worker process per node attempt (`hermes -z`), exit codes for CI/cron.
- Commands `/ge <task>` (autopilot) and `/ge-auto`; tool action `auto`; hooks
  `subagent_start`, `pre_tool_call`, `post_llm_call`; autopilot settings.
- Real Hermes end-to-end tests (`tests/e2e`) through the interactive CLI in a pseudo terminal.

### Changed

- The dispatcher and guards moved to `ge_runtime` (`ge_hermes.dispatch`/`guard` re-export).
- The run lock is reentrant per thread.
- A non-idempotent node whose dispatch was interrupted is held as NEEDS_ATTENTION with
  `DISPATCH_INTERRUPTED` (was: reported but left WAITING_FOR_SUBMISSION); only operator
  commands can submit for it.
- State schema 2 and receipt schema 2 (1.x runs are migrated on load).

### Removed

- The non-standard desktop handoff (`handler.agent_turn_handler` returning
  `{"type": "send"}`): commands follow the standard `fn(raw_args) -> str` contract and agent
  turns are requested through the public `inject_message` API.

### Fixed

- A torn final journal line made every later append fail (run bricked).
- A second process could dispatch the same replay-safe node again (lease was in-process only).
- `/ge-reclaim` could reclaim a dispatch whose worker was still alive.
- Receipts existed only after an explicit verify and carried no checksum.
- Replaced files were not made durable with a directory fsync.
- Docs did not list `/ge-reclaim`.

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
