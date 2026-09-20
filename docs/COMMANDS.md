# Command reference

All commands are Hermes in-session slash commands registered by the plugin
(`ge_hermes.COMMANDS`). They work wherever Hermes dispatches plugin slash
commands: the Hermes CLI, Hermes Desktop and the messaging gateway.

Conventions used below:

- `<run>` is a run id such as `text-pipeline-20260101t120000z-a1b2c3`, or `last`
  for the most recently updated run of the current profile.
- Every command accepts `--json` to return machine-readable JSON instead of text.
- Errors are returned as text, never raised into Hermes:
  `GE_ERROR <CODE>: <message>` (or `{"ok": false, "error": "<CODE>", ...}` with `--json`).
- Runs are stored per Hermes profile; a command only sees the runs of the profile it runs in.

**Telegram.** Telegram bot commands cannot contain `-`. Type the underscore form
(`/ge_create`, `/ge_approve`, ...). The Hermes gateway maps `_` to `-` when it
looks up plugin commands, so `/ge_create` runs `/ge-create`. Discord, the CLI and
Desktop use the hyphenated names directly.

**Operator-only commands.** `/ge-approve`, `/ge-retry` and `/ge-cancel` record
human decisions. The agent tool `ge_graph` deliberately has no equivalent.

---

## `/ge`

- **Purpose:** help, executors, templates and recent runs.
- **Syntax:** `/ge [runs]`
- **Arguments:** `runs` (optional) shows only the recent runs.
- **Example:** `/ge`
- **State requirements:** none.

## `/ge-analyze`

- **Purpose:** analyze a task into a draft execution graph (creates a DRAFT run).
- **Syntax:** `/ge-analyze [--preview] <task>`
- **Arguments:**
  - `<task>`: task text. Numbered (`1.`), bulleted (`-`) items, separate lines or
    `;`/`then` separators become nodes, chained in order.
  - `--preview`: show the plan without creating a run.
- **Example:** `/ge-analyze Create an execution graph for: 1. read input 2. validate input 3. transform input 4. verify result`
- **State requirements:** none. Generated nodes use the `agent` executor; steps with
  side-effect verbs (write, delete, deploy, send, ...) get an approval gate and a
  rollback boundary. The analysis is keyword-based and deterministic; review the plan
  before approving it.

## `/ge-create`

- **Purpose:** create a run from a template, inline JSON/YAML or a spec file.
- **Syntax:** `/ge-create --template text-pipeline [--text <text>]` or `/ge-create <spec>`
- **Arguments:**
  - `--template <name>`: built-in template (`text-pipeline`).
  - `--text <text>`: input text for the template (quotes allowed).
  - `<spec>`: inline JSON or YAML, or a path to a `.json`, `.yaml` or `.yml` file
    readable by the Hermes process.
- **Example:** `/ge-create --template text-pipeline --text "  hello   graph  "`
- **State requirements:** the spec must pass validation. The new run is `DRAFT`
  (or `APPROVED` when `require_plan_approval` is disabled).

## `/ge-validate`

- **Purpose:** validate a graph spec without creating a run.
- **Syntax:** `/ge-validate <spec>`
- **Arguments:** `<spec>`: inline JSON/YAML or a `.json`/`.yaml`/`.yml` file path.
- **Example:** `/ge-validate examples/text-pipeline/spec.yaml`
- **State requirements:** none. Returns `VALID` plus the plan, or
  `GRAPH_INVALID`, `DEPENDENCY_CYCLE` or `EXECUTOR_UNAVAILABLE` with details.

## `/ge-plan`

- **Purpose:** show the plan: nodes, dependencies, order, parallel layers, gates,
  ownership and rollback boundaries.
- **Syntax:** `/ge-plan <run|last>`
- **Arguments:** `<run>`
- **Example:** `/ge-plan last`
- **State requirements:** the run exists.

## `/ge-approve`

- **Purpose:** approve (or deny) the plan or one node gate.
- **Syntax:** `/ge-approve <run|last> [plan|<node>:<gate>] [--deny]`
- **Arguments:**
  - target `plan` (default) or `<node>:<gate>`.
  - `--deny` records a denial instead of an approval.
- **Example:** `/ge-approve last plan`, `/ge-approve last deploy:approval`
- **State requirements:**
  - `plan`: the run is `DRAFT`. Approval moves it to `APPROVED`; denial cancels the run.
  - `<node>:<gate>`: the node is currently waiting on that gate (approval gate before
    execution, review gate after outputs were submitted). Denial fails the node with
    `GATE_FAILED`. Approving a gate of a running run continues execution.

## `/ge-run`

- **Purpose:** execute an approved run until it finishes or waits.
- **Syntax:** `/ge-run <run|last>`
- **Arguments:** `<run>`
- **Example:** `/ge-run last`
- **State requirements:** `APPROVED`, `RUNNING` or `WAITING`. A `DRAFT` run returns
  `GATE_FAILED` (plan not approved); a finished run returns `INVALID_TRANSITION`;
  disabled or incapable executors return `EXECUTOR_UNAVAILABLE` without changing state.
  `builtin` nodes execute immediately; `agent` nodes become work orders
  (`WAITING_FOR_SUBMISSION`).

## `/ge-status`

- **Purpose:** run status: current, completed, blocked and failed nodes, gates, holds
  and work orders. Without a run id, lists recent runs.
- **Syntax:** `/ge-status [<run|last>]`
- **Arguments:** `<run>` (optional)
- **Example:** `/ge-status last`
- **State requirements:** none (with a run id: the run exists).

## `/ge-verify`

- **Purpose:** verify a run against its contracts, success criteria and gates.
- **Syntax:** `/ge-verify <run|last>`
- **Arguments:** `<run>`
- **Example:** `/ge-verify last`
- **State requirements:** any state. Checks state integrity, spec digest, trace hash
  chain, plan approval binding, node status, output contracts, success criteria and
  gate bindings, and writes a receipt. Only a `SUCCEEDED` run whose checks all pass is
  `VERIFIED`; a draft or unfinished run is reported as `NOT VERIFIED`.

## `/ge-resume`

- **Purpose:** safely resume an interrupted or waiting run.
- **Syntax:** `/ge-resume <run|last>`
- **Arguments:** `<run>`
- **Example:** `/ge-resume last`
- **State requirements:** the run is not finished. Interrupted idempotent nodes are
  re-queued; interrupted non-idempotent nodes are held in `NEEDS_ATTENTION` and never
  replayed automatically. Unless the run is `DRAFT`, execution continues.

## `/ge-explain`

- **Purpose:** explain why each node, dependency and gate exists.
- **Syntax:** `/ge-explain <run|last>`
- **Arguments:** `<run>`
- **Example:** `/ge-explain last`
- **State requirements:** the run exists.

## `/ge-submit`

- **Purpose:** deliver outputs (or a failure) for a node waiting on agent work.
- **Syntax:** `/ge-submit <run|last> <node> <json>` or `/ge-submit <run|last> <node> --failed <reason>`
- **Arguments:**
  - `<node>`: node id.
  - `<json>`: JSON object matching the node's output contract.
  - `--failed <reason>`: report that the work could not be done.
- **Example:** `/ge-submit last read_input {"result": "loaded 12 records"}`
- **State requirements:** the node is `RUNNING` and waiting for a submission. Outputs
  that violate the output contract or any success criterion fail the attempt.

## `/ge-retry`

- **Purpose:** authorize re-running a node held for operator attention.
- **Syntax:** `/ge-retry <run|last> <node>`
- **Arguments:** `<node>`: node id.
- **Example:** `/ge-retry last deploy`
- **State requirements:** the node is held in `NEEDS_ATTENTION` (an interrupted
  non-idempotent node found by `/ge-resume`).

## `/ge-cancel`

- **Purpose:** cancel a run.
- **Syntax:** `/ge-cancel <run|last>`
- **Arguments:** `<run>`
- **Example:** `/ge-cancel last`
- **State requirements:** the run is not finished. Open nodes become `SKIPPED`.

---

## Agent tool `ge_graph`

Registered in toolset `graph_engineering`. Actions: `analyze`, `validate`, `create`,
`plan`, `run`, `status`, `work`, `submit`, `verify`, `explain`, `resume`, `list`.
Parameters: `action` (required), `task`, `spec` (JSON/YAML text), `template`, `text`,
`run_id`, `node`, `outputs`, `failed`, `detail`. The tool cannot approve, deny, retry
or cancel.

With `autonomous_agent_execution: true`, `run` and `resume` also execute the waiting agent
nodes through the running Hermes host, one at a time, and return an `autonomous` block
(`dispatched`, `stopped`, and `error`/`message` when something prevented it). Without a host
agent execution API, or outside an agent turn (for example the `/ge-run` and `/ge-resume`
commands typed outside a turn), the block carries `HOST_EXECUTION_UNAVAILABLE` and the nodes
keep waiting for a manual submit. While a run is being executed, `run`, `resume`, `submit` and
`verify` on it are refused with `DISPATCH_IN_PROGRESS`; operator commands stay available.
