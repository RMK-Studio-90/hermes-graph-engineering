# Architecture

## Layers

```
Hermes Agent                  plugin discovery, slash-command dispatch, tool registry, model routing
  │
  ▼
plugin directory entry        <hermes-home>/plugins/hermes-graph-engineering/__init__.py
  │                           binds the bundled lib/ and calls ge_hermes.register(ctx)
  ▼
ge_hermes (binding)           /ge* commands, ge_graph tool, skill, settings; talks to Hermes only
  │                           through the PluginContext passed to register(ctx)
  ▼
ge_runtime (core)             spec admission, executors, engine, store, analysis, reports;
                              standard library + PyYAML only, never imports Hermes
```

The dependency direction is enforced by `scripts/boundary_check.py imports` and the
boundary tests: `ge_runtime` may not import `ge_hermes` or any Hermes module, and
`ge_hermes` may not import Hermes internals. The single exception is the module the host
documents as a plugin API (`agent.subagent_lifecycle`), allowlisted in
`scripts/sanitizer_policy.json` for one file (`ge_hermes/host.py`), imported lazily inside a
function, two names only.

## GE2: autonomous, durable, policy-governed, evidence-verified

```
/ge <task>  |  ge_graph action=auto  |  hermes ge run  |  python -m ge_runtime.headless
        │
        ▼
ge_runtime.autopilot      ANALYZE → PLAN → POLICY → EXECUTE → VERIFY → DIAGNOSE → REPAIR/REPLAN → RETEST → TERMINAL
   ├─ planner             task text → graph; evidence extracted from the task (commands, files, contents)
   ├─ policy              risk classes, digest-bound auto approval, operator gates, denial
   ├─ engine              explicit state machine, gates, contracts, criteria, evidence, repair, revision
   ├─ dispatch            durable lease + write-ahead claims; one fresh worker context per node attempt
   ├─ evidence            deterministic checkers; checksummed evidence artifacts
   ├─ findings / views    isolated review roles; verdicts only count with quotes of runtime-read source
   └─ store (StateStore)  state ↔ journal binding, repair/quarantine, receipts, artifacts, leases
        │
ge_hermes                 standard plugin surface only: commands (fn(args) -> str), tool, skill,
                          hooks (subagent_start, pre_tool_call, post_llm_call), CLI command (hermes ge),
                          inject_message, subagent_lifecycle
```

### Durable kernel (`ge_runtime.store`, `ge_runtime.engine`, `ge_runtime.states`)

- **State machine.** `RUN_TRANSITIONS` and `NODE_TRANSITIONS` list every allowed status change;
  terminal states have none. A plan revision may reset nodes to `PENDING` (never a running one).
  `REPAIRING` is the node state between a failed attempt and the autopilot's decision.
- **State bound to the journal.** Every saved state carries a monotonic `revision` and the
  `journal` head (`seq`, `hash`) it reflects; the store then appends `state.saved` with the
  revision and checksum. Every mutation first *reconciles* under the run lock:
  - torn final journal line (a crash mid-append) → moved to `quarantine/`, journal cut back,
    `trace.repaired`, recovery entry (`repaired`);
  - any other damage → the whole journal is quarantined (never deleted), a new journal starts
    with `trace.quarantined`, and the run carries a permanent integrity violation: verification
    fails `trace_chain`/`journal_binding` forever;
  - journal shorter than or diverging from the state's head → violation (`trace_truncated`,
    `trace_diverged`);
  - a `state.saved` newer than the state → `STATE_DIVERGED` (a rolled-back `state.json` is refused);
  - state-changing events after the head (crash between event and save) → `state.reconciled`;
    the saved state stays authoritative, interrupted work is recovered conservatively.
- **Crash-safe finalization.** A run that becomes terminal is saved with `finalization: PENDING`,
  then a checksummed receipt (schema 2: checks, verification level, journal head, per-node history,
  approvals, policy, revisions, recovery) is written atomically, `run.receipt_written` is journaled
  and the finalization becomes `COMPLETE` with the receipt checksum. `ensure_finalized` completes an
  interrupted finalization and regenerates a missing or damaged receipt (`receipt_recovered`).
- **Durability.** Every replaced file is written to a temporary file, fsynced, renamed and its
  directory fsynced; every journal line is fsynced (and the directory when the journal is created).
- **Migration.** Schema 1 states load, are migrated in memory (`state.migrated`) and persisted as
  schema 2; 1.x receipts (no checksum) are regenerated. Unknown schemas fail closed.
- **StateStore.** The engine depends on the `StateStore` protocol; `LocalStateStore` (`RunStore`)
  is the default. A database or object-store backend implements the same methods (atomic state
  replace, append-only journal, per-run exclusive lock, receipts, artifacts, leases).

### Durable dispatch (`ge_runtime.dispatch`)

- One **lease** per run (`lease.json`: owner = host:pid:id, epoch, heartbeat, ttl), taken under the
  run lock, heartbeated by a background thread, released in `finally`. A second dispatcher in any
  thread, process or host sharing the directory gets `DISPATCH_IN_PROGRESS`. Expired leases (no
  heartbeat within the ttl, or the owner process is gone on this host) are taken over and journaled.
- A **claim** (`node.dispatched` with owner, epoch and the worker context digest) is journaled before
  the worker starts; results are submitted only while the lease is still held.
- An abandoned claim is **reclaimed automatically** for idempotent, side-effect-free nodes and
  persisted as **`DISPATCH_INTERRUPTED`** (NEEDS_ATTENTION with that code) for everything else;
  only an operator can then retry, submit or cancel.

### Policy (`ge_runtime.policy`)

Risk classes `READ_ONLY < WORKSPACE_WRITE < LOCAL_EXEC < NETWORK < EXTERNAL_SIDE_EFFECT < PAID <
DESTRUCTIVE` are inferred from executor, capabilities, evidence commands and node text; declared
`risk`/`side_effects` can raise but never lower them. Default decisions: auto-approve up to
`LOCAL_EXEC`, operator gate (`<node>:policy`, digest-bound to node and decision) for NETWORK,
EXTERNAL_SIDE_EFFECT and PAID, deny DESTRUCTIVE (the plan is rejected, `POLICY_DENIED`). The
policy record (configuration, spec digest, decisions, digest) is persisted; a policy plan approval
stores its digest as basis; verification re-derives the decisions. Worker rights follow the class:
the Hermes binding narrows the worker's toolsets at launch and blocks other tools in
`pre_tool_call`; a READ_ONLY worker that changes the workspace fails with `POLICY_VIOLATION`.

### Evidence (`ge_runtime.evidence`)

`command` (exit code, output digests and tails), `file_exists`, `file_absent`, `file_contains`,
`file_sha256` (fixed or the agent's claimed hash), `git_diff` (files changed by this attempt,
relative to a baseline, within allowed globs), `output_equals_file`, and graph-level `acceptance`
(re-checked at verification). Results are checksummed artifacts referenced by digest from state,
journal and receipt. Receipts carry a verification level: `DETERMINISTIC`, `EVIDENCE` or
`SELF_REPORTED`; with `require_evidence` a self-reported success is not verified.

### Autopilot (`ge_runtime.autopilot`, `ge_runtime.planner`)

Failures are classified (`TRANSIENT`, `CONTRACT`, `EVIDENCE`, `STRUCTURAL`, `POLICY`). Repairs
re-run a node with a diagnosis artifact (failed checks, exit codes, output tails) in a new worker
context, with backoff for transient failures. Structural failures (and failed checkpoints) revise
the subgraph: the agent nodes a failed check is about are reopened with the diagnosis as an explicit
input, or a read-only diagnosis node is inserted; the revised plan is re-approved by policy and
re-tested. Hard budgets (attempts, repairs, replans, runtime, cost, drive calls) end hopeless runs
with `BUDGET_EXHAUSTED`. Everything lives in the run, so any call in any process continues.

### Isolation (`ge_runtime.views`, `ge_runtime.findings`, `ge_runtime.workflows`)

Workers never share a conversation: each attempt gets a new worker (Hermes: a new child session;
headless: a new process) whose context is built only from the node's goal and declared inputs, and
whose digest is journaled. Input views project artifacts per role. The `audit` workflow wires
context mapper → hunters → blind reviewer (findings only) → code verifier (finding + runtime-read
source ranges) → deterministic adjudication → synthesizer (ledger only). Findings are
`UNVERIFIED | CONFIRMED | PARTIALLY_CONFIRMED | REJECTED | NEEDS_MORE_EVIDENCE`; CONFIRMED requires the
verifier's verdict *and* quotes that exist on the cited lines of the source the runtime read.

### Hosts without a chat turn

`python -m ge_runtime.headless` and `hermes ge run|drive|status|recover` drive runs from cron, CI or
containers. Agent nodes run through `CommandWorker` (default `hermes -z {prompt} -t {toolsets}`): one
process per attempt, toolsets restricted by risk class, worker identity in `GE_WORKER_*`. Exit codes:
0 verified, 1 not verified, 2 operator decision, 3 agent work without a worker, 4 error.

### Continuation across agent turns (Hermes)

`/ge <task>` plans, applies the policy and runs what it can; when agent work waits, it requests an
agent turn with the public `PluginContext.inject_message`. The turn's model calls `ge_graph
action=auto` while the response says `"continue": true`. The `post_llm_call` hook requests a further
turn when a turn ended with the run still unfinished, but only if the run was driven since the last
request (loop guard). There is no dependency on any desktop-specific handoff.

## Lifecycle (manual flow, unchanged)

```
graph creation            /ge-analyze, /ge-create, ge_graph analyze/create
        ↓  strict admission: schema, references, cycles, executors, capabilities, evidence, views
persistent run state      runs/<run_id>/state.json  (status DRAFT)
        ↓
approval                  /ge-approve <run> plan   (binding: graph, spec digest, run, action digest)
        ↓
execution                 /ge-run: dependency order, gates, write-ahead RUNNING record,
        ↓                 builtin execution or agent work orders, contracts, criteria, evidence
journal                   runs/<run_id>/trace.jsonl  (hash-chained, bound to the state)
        ↓
finalization              automatic checksummed receipt at the terminal state; /ge-verify re-checks
```

### Spec admission (`ge_runtime.spec`)

A spec is plain JSON or YAML. Admission normalizes it and rejects, in order:
structural problems (`GRAPH_INVALID`: unknown fields, dangling or non-upstream
input references, invalid criteria, evidence or views, retries on non-idempotent nodes,
side effects without an approval gate and rollback boundary), cycles (`DEPENDENCY_CYCLE`,
with the cycle path) and unavailable executors or capabilities (`EXECUTOR_UNAVAILABLE`).
The normalized spec has a SHA-256 digest and a deterministic topological order (ties
broken by declaration order). The optional GE2 fields (`risk`, `evidence`, input `view`,
`acceptance`, `require_evidence`) enter the normalized spec only when declared, so 1.x
specs keep their digests.

### Executors (`ge_runtime.executors`)

- `builtin`: pure in-process operations (`identity`, `require`, `text_transform`,
  `compare`, `measure`, the `verify` checkpoint, `collect_findings`,
  `adjudicate_findings`). No filesystem, network, subprocess or model access. Only
  declared outputs leave the executor.
- `agent`: deferred. The engine publishes a work order (purpose, instructions,
  resolved inputs, output contract, success criteria, evidence, risk, diagnosis) and
  waits for a submission, which the durable dispatcher obtains from a fresh worker or an
  operator provides. Graph Engineering never chooses a model or provider.

### Engine rules (`ge_runtime.engine`)

- execution requires an approved plan (operator, or digest-bound policy decision);
- before a node runs: dependencies succeeded, declared and policy gates approved for the
  exact binding, the policy does not deny it, executor available, input contract satisfied;
- the node is persisted as `RUNNING` (with its wait state) before its executor is called;
- afterwards the output contract, every success criterion and all declared evidence are
  evaluated; an executor or agent reporting success is never sufficient on its own;
- retries happen only for retryable outcomes on idempotent, side-effect-free nodes;
  under the autopilot a failed replay-safe, auto-approved node becomes `REPAIRING`;
- `on_failure: stop` skips remaining work, `continue` blocks only dependents;
- on resume, interrupted idempotent nodes are re-queued and non-idempotent ones are
  held for an operator decision (`/ge-retry`).

Approvals are bound to a digest of the graph id, spec digest, run id and the approved
action (plus submitted outputs for review gates, plus the policy decision for policy
gates). Verification recomputes the bindings, so an approval cannot be transferred to a
changed plan.

### Store layout (`ge_runtime.store`)

```
runs/<run_id>/state.json         checksummed envelope (schema 2), bound to the journal head
runs/<run_id>/trace.jsonl        append-only hash-chained journal (state.saved per revision)
runs/<run_id>/receipt.json       checksummed terminal receipt (schema 2)
runs/<run_id>/artifacts/*.json   evidence, baselines, final report (checksummed)
runs/<run_id>/lease.json         durable dispatch lease
runs/<run_id>/quarantine/        damaged journal bytes, never deleted
runs/<run_id>/.lock              OS file lock (msvcrt on Windows, fcntl elsewhere), reentrant per thread
```

Filesystem write failures (permissions, disk, or the 260-character path limit on
Windows without long-path support) are reported as `STATE_WRITE_FAILED` or
`TRACE_WRITE_FAILED` with the path length, never with the absolute path.

## Profiles and state isolation

Hermes can run several profiles, each with its own home directory, configuration and
`plugins/` directory. The plugin must be installed into and enabled in every profile
that should offer it.

`register(ctx)` resolves `ctx.state.data_dir` once, inside the loader's profile scope,
and every command and tool handler created for that profile is bound to that
directory:

```
<profile-home>/plugin-data/<plugin namespace>/runs/<run_id>/...
```

A handler never receives a path from another profile, run ids are validated against a
strict pattern (no path traversal), and `last` resolves within the profile's own run
directory. Two profiles therefore never see each other's runs.

## Multi-profile gateway loading

A single Hermes gateway process can serve several profiles at once. It creates one
plugin manager per profile home, and each manager imports that profile's copy of the
plugin directory under its own module name. The bundled packages `ge_runtime` and
`ge_hermes`, however, are ordinary top-level packages and can exist only once in the
process's module table.

The directory entry (`plugin_init.py.tmpl`, installed as `__init__.py`) therefore
applies one rule when it finds those packages already imported from a different
installation:

- if that installation has the same install-manifest fingerprint (the SHA-256 of the
  file→hash map written by the installer), the already-loaded packages are reused;
  run state stays separate because each profile's `register(ctx)` binds its own data
  directory;
- otherwise the load fails closed with `PLUGIN_CONFIGURATION_INVALID`, and that profile
  shows the plugin as not loaded. Different versions are never mixed in one process.

Consequence for operators: install the same release into every profile, then restart
the gateway. The installer writes byte-identical trees for identical sources, so
repeated installs produce matching fingerprints.

## Installer (`scripts/install_plugin.py`)

Copies an allowlist of runtime files (entry shim, `plugin.yaml`, `README.md`,
`LICENSE`, `lib/ge_runtime`, `lib/ge_hermes`) into a staging directory outside
`plugins/`, writes `INSTALL_MANIFEST.json` (relative paths and SHA-256 hashes only),
verifies hashes, manifest, the sanitizer, the absence of retired names and an import of
the entry shim, then swaps the tree into place. Existing installations are moved to
`<hermes-home>/plugin-install/hermes-graph-engineering/backups/`. `verify`, `rollback`
and `uninstall` operate only on that one plugin directory.
