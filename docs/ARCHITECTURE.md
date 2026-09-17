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
`ge_hermes` may not import Hermes internals.

## Lifecycle

```
Hermes plugin loader
        ↓  scans <hermes-home>/plugins, loads enabled plugins, calls register(ctx)
Graph Engineering plugin entry
        ↓  registers commands, tool and skill; binds the profile's data directory
graph creation            /ge-analyze, /ge-create, ge_graph analyze/create
        ↓  strict admission: schema, references, cycles, executors, capabilities
persistent run state      runs/<run_id>/state.json  (status DRAFT)
        ↓
approval                  /ge-approve <run> plan   (binding: graph, spec digest, run, action digest)
        ↓
execution                 /ge-run: dependency order, gates, write-ahead RUNNING record,
        ↓                 builtin execution or agent work orders, output contracts, criteria
trace                     runs/<run_id>/trace.jsonl  (hash-chained events)
        ↓
verification              /ge-verify: integrity, digests, trace chain, approvals, contracts,
                          criteria → runs/<run_id>/receipt.json
```

### Spec admission (`ge_runtime.spec`)

A spec is plain JSON or YAML. Admission normalizes it and rejects, in order:
structural problems (`GRAPH_INVALID`: unknown fields, dangling or non-upstream
input references, invalid criteria, retries on non-idempotent nodes, side effects
without an approval gate and rollback boundary), cycles (`DEPENDENCY_CYCLE`, with the
cycle path) and unavailable executors or capabilities (`EXECUTOR_UNAVAILABLE`). The
normalized spec has a SHA-256 digest and a deterministic topological order (ties
broken by declaration order).

### Executors (`ge_runtime.executors`)

- `builtin`: pure in-process operations (`identity`, `require`, `text_transform`,
  `compare`, `measure`). No filesystem, network, subprocess or model access. Only
  declared outputs leave the executor.
- `agent`: deferred. The engine publishes a work order (purpose, instructions,
  resolved inputs, output contract, success criteria, required capabilities) and waits
  for a submission. Graph Engineering never chooses a model or provider; the host
  agent and its routing do the work.

### Engine (`ge_runtime.engine`)

Node states: `PENDING → READY → RUNNING → SUCCEEDED | FAILED`, plus `BLOCKED`
(an upstream node failed) and `SKIPPED` (the run stopped or was cancelled).
Run states: `DRAFT`, `APPROVED`, `RUNNING`, `WAITING`, `SUCCEEDED`, `FAILED`,
`CANCELLED`. A waiting run carries holds: `WAITING_FOR_APPROVAL`,
`WAITING_FOR_SUBMISSION`, `WAITING_FOR_REVIEW`, `NEEDS_ATTENTION`.

Rules the engine enforces:

- execution requires an approved plan (unless the operator disables plan approval);
- before a node runs: dependencies succeeded, approval gates approved for the exact
  binding, executor available, input contract satisfied;
- the node is persisted as `RUNNING` before its executor is called (write-ahead);
- afterwards the output contract and every success criterion are evaluated; an
  executor reporting success is never sufficient on its own;
- retries happen only for retryable outcomes on idempotent, side-effect-free nodes;
- `on_failure: stop` skips remaining work, `continue` blocks only dependents;
- on resume, interrupted idempotent nodes are re-queued and non-idempotent ones are
  held for an operator decision (`/ge-retry`).

Approvals are bound to a digest of the graph id, spec digest, run id and the approved
action (plus submitted outputs for review gates). Verification recomputes the
bindings, so an approval cannot be transferred to a changed plan.

### Store (`ge_runtime.store`)

Per run: `state.json` is a checksummed envelope (schema version, SHA-256 of the
canonical state) replaced atomically; `trace.jsonl` is append-only with each event
hashing its predecessor; `receipt.json` holds the last verification; `.lock` is an OS
file lock (`msvcrt` on Windows, `fcntl` elsewhere) held for every mutation. A state
file that fails to parse, has an unsupported schema version, a checksum mismatch or a
spec that no longer matches its digest is reported as `STATE_CORRUPT` and left
untouched. Filesystem write failures (permissions, disk, or the 260-character path
limit on Windows without long-path support) are reported as `STATE_WRITE_FAILED` or
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
