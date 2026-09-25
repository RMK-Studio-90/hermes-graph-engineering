---
name: graph-engineering
description: Hand multi-step work to the Graph Engineering autopilot, which plans it as an execution graph, applies a risk policy, runs every node in its own worker, verifies results with deterministic evidence, repairs failures and finishes with a receipt.
---

# Graph Engineering

Use the `ge_graph` tool when a task has several dependent steps that should be
planned, executed, checked and finished reliably.

## Autopilot (default)

1. Start: `ge_graph {"action": "auto", "task": "..."}` (or `"spec": "<JSON>"` for a
   graph you wrote yourself). The user may also have typed `/ge <task>`; then you
   receive a message asking you to call `{"action": "auto", "run_id": "..."}`.
2. Continue: while the response says `"continue": true`, call
   `{"action": "auto", "run_id": "<same id>"}` again. Every call makes durable
   progress; nothing is lost between calls, turns or restarts.
3. Finish: when the response carries `final_report`, reply with its summary: outcome,
   whether it is VERIFIED by evidence, and any failed checks.
4. If the response names an operator action (`NEEDS_OPERATOR`), tell the user the
   exact command (for example `/ge-approve <run> <node>:approval`). You cannot
   approve anything yourself, and you must not try to work around a hold.

Do not do the nodes' work yourself in the autopilot flow: each node runs in its own
fresh worker with only its explicit inputs, and the runtime checks the results.

### Writing a good spec for `auto`

Top level: `schema_version: 1`, `graph_id`, `title`, `inputs`, `nodes`, optional
`acceptance` (checks for the whole run) and `require_evidence: true`.
Give every node that produces something deterministic `evidence` the runtime can
check itself, for example:

- `{"type": "command", "argv": ["python", "-m", "pytest", "-q"], "expect_exit": 0}`
- `{"type": "file_exists", "path": "docs/report.md", "non_empty": true}`
- `{"type": "file_contains", "path": "out.txt", "text": "OK"}`
- `{"type": "file_sha256", "path": "dist/app.zip", "from_output": "sha256"}`
- `{"type": "git_diff", "allowed_paths": ["src/*", "tests/*"], "require_changes": true}`

Paths are relative to the workspace. An agent saying "tests pass" is not evidence;
an exit code is. Nodes are classified into risk classes (READ_ONLY, WORKSPACE_WRITE,
LOCAL_EXEC, NETWORK, EXTERNAL_SIDE_EFFECT, PAID, DESTRUCTIVE) from what they do;
declaring a lower `risk` or `side_effects: false` does not lower it.

### Isolated review workflows

For audits use separate nodes that see only explicit artifacts. Input `view`s
project what a worker sees: `{"pick": [...]}`, `{"omit": ["reasoning"]}`,
`{"attach_source": true}` (adds the source lines of each finding's `location`).
The `audit` template builds: context mapper -> specialist hunters -> blind report
reviewer (findings only) -> independent code verifier (finding + source ranges, no
reasoning) -> final synthesizer. Findings are UNVERIFIED until the verifier's
verdict is backed by quotes that exist in the cited source.

## If you are the worker

If your task says "You are executing one node from an approved Graph Engineering
run", you are a worker. Do only that node's work, within its permitted risk class,
and answer with one JSON object of the declared outputs in a ```json block. Do not
create, run, resume, submit or verify graphs (`ge_graph` refuses this for workers)
and never approve anything. If a diagnosis of a previous attempt is included, fix
exactly that.

## Manual flow (still supported)

`analyze`/`create` a run, the operator approves with `/ge-approve <run> plan`,
`run`, fetch `work` orders, `submit` outputs, `verify`. Holds:
`WAITING_FOR_APPROVAL` (operator gate), `WAITING_FOR_REVIEW`,
`WAITING_FOR_SUBMISSION` (work order), `WAITING_FOR_REPAIR` (the autopilot will
repair it), `NEEDS_ATTENTION` (only the operator can `/ge-retry`).

## Errors

Responses with `"ok": false` carry a stable `error` code such as `GRAPH_INVALID`,
`POLICY_DENIED`, `EVIDENCE_FAILED`, `BUDGET_EXHAUSTED`, `DISPATCH_IN_PROGRESS`,
`STATE_CORRUPT` or `PLUGIN_CONFIGURATION_INVALID`. Report them to the user; do not
retry blindly.
