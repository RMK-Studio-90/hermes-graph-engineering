# hermes-graph-engineering

> Independent community project. Not affiliated with, endorsed by, or sponsored by
> Nous Research.

Graph Engineering is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that turns multi-step work into an explicit **execution graph** that you can inspect,
approve, run, resume and verify.

[What it is](#what-it-is) · [Features](#features) · [Requirements](#requirements) ·
[Installation](#installation) · [Configuration](#configuration) · [Commands](#commands) ·
[Workflow example](#workflow-example) · [Spec](#spec) · [Architecture](#architecture) ·
[Safety and execution model](#safety-and-execution-model) ·
[Troubleshooting](#troubleshooting) · [Development](#development)

## What it is

An **execution graph** describes a piece of work as **nodes** connected by
dependencies. Each node states *why* it exists (purpose), *who* performs it (an
executor and the capabilities it needs), *what* it consumes and produces (input and
output contracts), *when it counts as done* (success criteria), and *what happens on
failure* (stop or continue, retries, approval gates, rollback boundary).

Graph Engineering:

- **validates** a graph before anything runs (references, contracts, cycles,
  executors, governance rules);
- **executes deterministically** in dependency order: pure `builtin` operations run
  in-process; `agent` nodes become work orders for the Hermes agent, whose own model
  routing does the work;
- **persists state** for every run (checksummed state file, hash-chained trace), so a
  run survives restarts and can be resumed safely;
- **verifies** a finished run against its contracts, success criteria, approvals and
  trace, and writes a receipt;
- **explains** why every node, dependency and gate exists;
- keeps each Hermes **profile isolated**: runs live in the profile's own data directory.

## Features

- Graph specs in JSON or YAML with strict, fail-closed admission.
- Deterministic topological execution order and parallel layers.
- Executors: `builtin` (`identity`, `require`, `text_transform`, `compare`, `measure`)
  and `agent` (deferred work orders with output submission).
- Plan approval, approval gates before a node, review gates after a node; approvals are
  bound to the exact plan and action.
- Output contracts and declarative success criteria (`non_empty`, `true`, `false`,
  `equals`, `not_equals`, `matches`, `min_length`, `max_length`, `min`, `max`, `type`,
  `in`).
- Failure policies, retries for idempotent nodes, conservative resume, cancellation.
- Deterministic task analysis (`/ge-analyze`) that turns a numbered task description
  into a draft graph.
- 14 slash commands, an agent tool (`ge_graph`) and a skill for the agent workflow.
- Profile-local persistence and safe loading in multi-profile gateways.
- Staged, hash-verified installer with reinstall, verify, rollback and uninstall.

## Requirements

- **Hermes Agent** 0.21 or newer (the plugin manifest declares `requires_hermes: ">=0.21"`;
  tested with 0.21.0 to 0.21.3). Optional autonomous agent execution additionally needs a
  host with the public subagent lifecycle API and is verified from 0.21.1 (see
  [Autonomous agent execution](#autonomous-agent-execution-optional)).
- **Python** 3.11 or newer; the test suite is run on **3.11** and **3.14**.
- **PyYAML** 6.0 or newer (Hermes already ships it).

## Installation

Clone the repository and install the plugin into your Hermes home with the Python
interpreter that runs Hermes (the installer uses it for its import check):

```bash
git clone <repository-url> hermes-graph-engineering
cd hermes-graph-engineering
python scripts/install_plugin.py install --hermes-home <hermes-home> --python <hermes-python>
```

This creates `<hermes-home>/plugins/hermes-graph-engineering`. The installer copies
only the runtime files, verifies their hashes, the manifest and an import of the
plugin, and never touches other plugins.

For **each additional Hermes profile** that should offer the commands, install into
that profile's home as well:

```bash
python scripts/install_plugin.py install --hermes-home <profile-home> --python <hermes-python>
```

Install the same release into every profile (see [Troubleshooting](#troubleshooting)).

Maintenance:

```bash
python scripts/install_plugin.py verify    --hermes-home <hermes-home>
python scripts/install_plugin.py reinstall --hermes-home <hermes-home>   # keeps a backup
python scripts/install_plugin.py rollback  --hermes-home <hermes-home>   # restores the newest backup
python scripts/install_plugin.py uninstall --hermes-home <hermes-home>   # moves the plugin to a backup
python scripts/install_plugin.py uninstall --hermes-home <hermes-home> --purge
```

Backups are kept in `<hermes-home>/plugin-install/hermes-graph-engineering/backups/`.
After uninstalling, also remove the plugin from `plugins.enabled`.

The wheel (`pip install hermes_graph_engineering-1.0.0-py3-none-any.whl`) provides the
`ge_runtime` and `ge_hermes` Python packages for programmatic use; Hermes itself loads
the plugin from the plugin directory created by the installer.

## Configuration

Hermes loads user plugins only when they are enabled. In `<hermes-home>/config.yaml`
(and in `<profile-home>/config.yaml` for every profile that should have it):

```yaml
plugins:
  enabled:
    - hermes-graph-engineering
```

or run `hermes plugins enable hermes-graph-engineering`. Start a new Hermes session or
restart the gateway afterwards (`hermes gateway restart`).

Optional settings:

```yaml
plugins:
  entries:
    hermes-graph-engineering:
      settings:
        require_plan_approval: true   # default; false creates runs already APPROVED
        disabled_executors: []        # e.g. [agent] to allow only builtin nodes
        max_steps: 500                # node attempts started per /ge-run or /ge-resume
        autonomous_agent_execution: false        # see "Autonomous agent execution" below
        autonomous_node_timeout_seconds: 3600
        autonomous_call_budget_seconds: 300
```

Invalid values make every command return `PLUGIN_CONFIGURATION_INVALID` until fixed.

### Autonomous agent execution (optional)

By default an `agent` node stops at `WAITING_FOR_SUBMISSION`: someone else (you, an
external worker, a test) does the work and submits the result.

```text
manual mode       Graph Engineering -> external or manual agent work   -> /ge-submit or ge_graph submit
autonomous mode   Graph Engineering -> Hermes host agent execution     -> automatic submit
```

Autonomous mode is off unless you enable it (per profile):

```yaml
plugins:
  entries:
    hermes-graph-engineering:
      settings:
        autonomous_agent_execution: true
        autonomous_node_timeout_seconds: 3600   # a worker is cancelled after this long
        autonomous_call_budget_seconds: 300     # stop starting further nodes in one call after this long
```

> **Graph Engineering does not select or configure LLM providers.** Autonomous `agent`
> nodes are executed through the capabilities provided by the running Hermes installation:
> its model, routing, tools and permission policy, whatever they are. Graph Engineering
> only says *what* must be done; Hermes decides *how*.

How it works:

- When the agent calls `ge_graph` with `run` or `resume` (during a normal agent turn),
  waiting agent nodes are handed to Hermes **one at a time**, in dependency order. Each
  node becomes a bounded task: purpose, instructions, resolved inputs, required
  capabilities, output contract and success criteria. Hermes runs it in a fresh
  worker session through its public subagent lifecycle API, and the reply's JSON outputs
  are submitted with the same `submit` that manual mode uses.
- **The engine stays the authority.** Outputs are checked against the output contract and
  every success criterion exactly as for a manual submit; a worker that reports success
  but returns invalid outputs fails the node under the node's normal retry policy.
- **The worker is not the operator.** Dispatch stops at an unapproved plan, at approval
  gates, at review gates and at `NEEDS_ATTENTION`. It never approves, denies, retries or
  cancels, and it does not bypass side-effect gates.
- **Fallback.** If the host offers no agent execution API, or no agent turn is active
  (for example a `/ge-run` typed outside a turn), the response carries
  `HOST_EXECUTION_UNAVAILABLE`, nothing is started, and the node stays
  `WAITING_FOR_SUBMISSION` for a manual submit. Registration never fails because of it.
- **Recovery.** A dispatch is recorded in the run trace before the worker starts. After a
  crash, an interrupted idempotent node without side effects is dispatched again; any
  other node is held (`DISPATCH_INTERRUPTED`) so the operator decides. Manual
  `/ge-submit` remains available at all times.
- **No recursion.** While a node is being worked on, the worker cannot run, resume,
  submit or verify graphs (`DISPATCH_IN_PROGRESS`), nested dispatch is refused, and where
  the host copies its context into worker threads the worker also cannot create graphs
  (`WORKER_CONTEXT_RESTRICTED`).
- **No extra permissions.** The worker runs under the Hermes session's normal tool and
  permission policy. Graph Engineering adds no filesystem, network, shell, secret or
  credential access, and needs no API key.
- **Time.** Hermes bounds each tool call (420 seconds by default). After
  `autonomous_call_budget_seconds` no further node is started in the same call and the
  response says `time_budget_exhausted`; call `resume` to continue. A single node that runs
  longer than the host's tool-call deadline is still cut off at the agent (the worker keeps
  going and is submitted when it finishes; use `status` to follow it), so raise the host's
  tool-call timeout for long nodes.
- **Limits.** Sequential only; one dispatch per profile at a time. Failures reported by
  the host (`HOST_EXECUTION_FAILED`, invalid replies as `HOST_RESULT_INVALID`) fail the
  attempt. Replies are read from the worker's final message (the host caps it at about
  32,000 characters), so declared outputs should be small.

Compatibility: autonomous execution feature-detects `PluginContext.subagent_lifecycle`.
It was verified end to end (through Hermes' own plugin loader, subagent lifecycle and
thread pool, with only the worker's model turn replaced by a deterministic stand-in) on
unmodified Hermes 0.21.1 and 0.21.3 and on a 0.21.2 installation. On 0.21.0 it runs, but
that release does not copy context into worker threads, so a worker is not prevented from
creating (never approving or running) new draft graphs; use 0.21.1 or newer for
autonomous mode. Manual mode is unchanged and was also verified on 0.21.0.

## Commands

| Command | Purpose |
| --- | --- |
| `/ge [runs]` | help, executors, templates, recent runs |
| `/ge-analyze [--preview] <task>` | task text → draft graph (DRAFT run) |
| `/ge-create --template text-pipeline [--text <text>]` or `/ge-create <spec>` | create a run from a template, inline JSON/YAML or spec file |
| `/ge-validate <spec>` | validate a spec without creating a run |
| `/ge-plan <run>` | nodes, dependencies, order, layers, gates, ownership, rollback boundaries |
| `/ge-approve <run> [plan\|<node>:<gate>] [--deny]` | operator decision on the plan or a gate |
| `/ge-run <run>` | execute an approved run until it finishes or waits |
| `/ge-status [<run>]` | status, current/completed/blocked/failed nodes, gates, holds; lists runs without an id |
| `/ge-verify <run>` | verify contracts, criteria, gates, approvals and trace; writes a receipt |
| `/ge-resume <run>` | recover an interrupted run conservatively and continue |
| `/ge-explain <run>` | why each node, dependency and gate exists |
| `/ge-submit <run> <node> <json>` or `--failed <reason>` | deliver outputs for an agent node |
| `/ge-retry <run> <node>` | authorize re-running a node held in NEEDS_ATTENTION |
| `/ge-cancel <run>` | cancel a run |

`<run>` may be `last`. Add `--json` for machine-readable output. **Telegram** does not
allow `-` in bot commands: use `/ge_create`, `/ge_approve`, ... — the Hermes gateway
maps `_` to `-` when it looks up plugin commands.

The full reference with syntax, arguments, examples and state requirements is in
[docs/COMMANDS.md](docs/COMMANDS.md). The agent tool `ge_graph` (toolset
`graph_engineering`) covers analysis, creation, runs, work orders, submissions,
verification and explanations, but not approvals, retries or cancellation.

## Workflow example

```
/ge-create --template text-pipeline --text "  hello   graph  "
    → Graph plan: Deterministic text pipeline, run text-pipeline-…, status DRAFT
/ge-plan last                 inspect nodes, dependencies and gates
/ge-approve last plan         → APPROVED
/ge-run last                  → SUCCEEDED (read_input, validate_input, transform_input, verify_result)
/ge-status last               → all four nodes SUCCEEDED, plan=APPROVED
/ge-explain last              → why each node and dependency exists
/ge-verify last               → VERIFIED (state integrity, spec digest, trace chain, approvals,
                                 output contracts and success criteria all pass)
```

With the agent: `/ge-analyze <task with numbered steps>`, approve the plan, `/ge-run last`;
the run waits for work orders that the agent completes through the `ge_graph` tool, then
`/ge-verify last`. See [examples/text-pipeline](examples/text-pipeline).

## Spec

```yaml
schema_version: 1
graph_id: release-notes
title: Release notes
inputs: {version: "1.2.0"}
nodes:
  - id: collect
    purpose: Collect merged changes
    executor: agent
    requires: [agent.tools]
    inputs: {version: graph.inputs.version}
    outputs: {changes: {type: string}}
    success_criteria: [{output: changes, check: non_empty}]
  - id: publish
    purpose: Publish the notes
    depends_on: [collect]
    executor: agent
    requires: [agent.tools]
    inputs: {changes: collect.changes}
    outputs: {url: string}
    success_criteria: [{output: url, check: matches, value: "https://.+"}]
    side_effects: true
    gates: [{id: approve, type: approval}]
    rollback_boundary: publication
```

Node fields: `id`, `name`, `purpose`, `depends_on`, `executor` (`builtin` | `agent`),
`requires` (capabilities: `compute.pure`; `agent.reasoning`, `agent.generation`,
`agent.tools`, `agent.code`, `agent.research`, `agent.review`), `operation` (builtin
only), `instructions` (agent), `inputs` (`graph.inputs.<name>` or `<node>.<output>`),
`outputs`, `success_criteria`, `on_failure`, `retry.max_attempts`, `idempotent`,
`side_effects`, `gates` (`approval` | `review`), `owner`, `rollback_boundary`.
Nodes with side effects need an approval gate and a rollback boundary; retries require
idempotent nodes without side effects.

## Architecture

```
Hermes Agent ──▶ plugin entry (<hermes-home>/plugins/hermes-graph-engineering/__init__.py)
                  └─▶ ge_hermes   commands, tool, skill, settings (PluginContext only)
                        └─▶ ge_runtime   spec admission · executors · engine · store · analysis
```

- `ge_runtime` is host-independent (standard library + PyYAML) and never imports Hermes.
- Run files per profile: `<profile-home>/plugin-data/<plugin namespace>/runs/<run_id>/`
  with `state.json` (checksummed), `trace.jsonl` (hash-chained) and `receipt.json`.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Safety and execution model

- **Approval first.** A run executes only after its plan is approved. Approval gates
  pause before a node; review gates pause after outputs are produced. Approvals are bound
  to a digest of the plan and action and are re-checked during verification.
- **Operator-only decisions.** Approving, denying, retrying and cancelling are slash
  commands; the agent tool cannot perform them.
- **Deterministic operations.** `builtin` nodes are pure in-process transformations;
  identical inputs give identical results.
- **Fail closed.** Invalid specs, cycles, unknown or disabled executors, missing
  capabilities, invalid settings and corrupt state are rejected with stable error codes
  (`GRAPH_INVALID`, `DEPENDENCY_CYCLE`, `EXECUTOR_UNAVAILABLE`, `NODE_FAILED`,
  `GATE_FAILED`, `STATE_CORRUPT`, `PLUGIN_CONFIGURATION_INVALID`, `RUN_NOT_FOUND`,
  `RUN_LOCKED`, `INVALID_TRANSITION`, `INVALID_ARGUMENT`, `TRACE_WRITE_FAILED`,
  `STATE_WRITE_FAILED`; autonomous execution adds `HOST_EXECUTION_UNAVAILABLE`,
  `HOST_EXECUTION_FAILED`, `HOST_RESULT_INVALID`, `WORKER_CONTEXT_RESTRICTED`,
  `DISPATCH_IN_PROGRESS`, `DISPATCH_INTERRUPTED`).
- **Success means verified outputs.** A node succeeds only when its outputs satisfy the
  output contract and every success criterion, never just because an executor reported
  success.
- **Safe recovery.** A node is recorded as RUNNING before it executes. On `/ge-resume`,
  interrupted idempotent nodes are re-queued; non-idempotent nodes are held until an
  operator runs `/ge-retry`.
- **Integrity.** State files carry a checksum and the trace is hash-chained; corrupt state
  is reported as `STATE_CORRUPT` and never modified. This detects corruption and naive
  tampering; it is not a cryptographic signature.
- **Isolation.** Each profile's handlers are bound to that profile's data directory.

See also [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Unknown command` or commands missing | The plugin is not installed or not enabled in this profile, or the session started earlier. Check `hermes plugins list`, enable the plugin, start a new session or run `hermes gateway restart`. |
| Commands work in the CLI but not in chat | The gateway was started before the plugin was enabled; restart the gateway. |
| Telegram rejects `/ge-create` | Use the underscore form `/ge_create`; Hermes maps it to `/ge-create`. |
| A command is missing after enabling | All commands use the `ge` namespace to avoid Hermes built-ins such as `/graph` and `/plan`. If Hermes refuses a name it logs a collision warning (the plugin logs which commands were refused). |
| A run from another profile is not listed | Expected: runs are stored per profile. Use the profile that created the run. |
| `/ge-run` returns `GATE_FAILED ... plan ... not approved` | The run is still DRAFT. Review it with `/ge-plan`, then `/ge-approve <run> plan`. |
| `/ge-verify` says `NOT VERIFIED` | Verification requires a SUCCEEDED run. For DRAFT or unfinished runs the failing checks show what is missing. |
| Run is `WAITING` | `/ge-status <run>` shows each hold: an approval gate, a work order waiting for the agent or `/ge-submit`, a review gate, or a node needing `/ge-retry`. |
| In a multi-profile gateway one profile reports `PLUGIN_CONFIGURATION_INVALID: a different hermes-graph-engineering installation is already loaded` | Profiles served by one gateway process share the loaded packages, so every profile must have the same release installed. Reinstall the same version into all profiles and restart the gateway. |
| `STATE_CORRUPT` | The run's state failed its checksum, schema or digest check. Nothing is repaired automatically; inspect the run directory. |
| `RUN_LOCKED` | Another process is operating on the run; retry shortly. |
| `STATE_WRITE_FAILED` (on Windows often with "Windows limits paths to 260") | The run state could not be written. Check that the Hermes home is writable. On Windows, very deep Hermes homes can exceed the 260-character path limit (run files live under `<hermes-home>/plugin-data/<plugin namespace>/runs/<run_id>/`); use a shorter Hermes home or enable Windows long paths. |

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/boundary_check.py imports
python scripts/boundary_check.py sanitize
python -m build
```

Tests against a real Hermes installation run when `GE_HERMES_PYTHON` points to an
interpreter that can import `hermes_cli`. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
