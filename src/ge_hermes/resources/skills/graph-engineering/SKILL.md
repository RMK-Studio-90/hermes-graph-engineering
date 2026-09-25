---
name: graph-engineering
description: Plan, run and verify multi-step work as a Graph Engineering execution graph with dependencies, gates, contracts and verification.
---

# Graph Engineering

Use the `ge_graph` tool when a task has several dependent steps that should be
planned explicitly, approved, executed in order and verified.

## Flow

1. `ge_graph {"action": "analyze", "task": "..."}` creates a DRAFT run from a
   task (numbered steps become nodes). For a better graph, write your own spec
   and use `{"action": "create", "spec": "<JSON>"}`.
2. Show the plan to the user and ask them to approve it with
   `/ge-approve <run_id> plan`. You cannot approve plans or gates yourself.
   On supported Desktop/TUI hosts with autonomous execution enabled, that
   approval automatically starts a new agent turn to resume the exact run.
   When receiving this continuation, execute it; do not merely acknowledge the
   approval or create another draft. `/ge <task>` starts the planning turn.
3. `{"action": "run", "run_id": "..."}` executes. `builtin` nodes run
   immediately. `agent` nodes become work orders for you.
4. `{"action": "work", "run_id": "..."}` lists work orders: node purpose,
   instructions, resolved inputs, required outputs and success criteria.
   Do the work with your normal tools, then submit:
   `{"action": "submit", "run_id": "...", "node": "...", "outputs": {...}}`.
   If you cannot do it, submit `{"failed": true, "detail": "why"}`.
   Outputs are rejected unless they match the output contract and every
   success criterion. Never claim a node succeeded without a submission.
   With autonomous agent execution enabled, `run` (or `resume`) may already have
   executed the agent nodes through the host: read the `autonomous` block of the
   response. If it reports `HOST_EXECUTION_UNAVAILABLE`, do the work orders yourself
   as described above.
5. When the run is SUCCEEDED, `{"action": "verify", "run_id": "..."}`
   re-checks contracts, criteria, gates, plan approval and the trace chain.

## If you are the worker

If your task says "You are executing one node from an approved Graph Engineering
run", you are a worker. Do only that node's work and answer with one JSON object of
the declared outputs in a ```json block. Do not create, run, resume, submit or verify
graphs (`ge_graph` refuses this for workers), and never approve anything.

## Holds

- `WAITING_FOR_APPROVAL`: a gate needs `/ge-approve <run> <node>:<gate>` by the user.
- `WAITING_FOR_REVIEW`: submitted outputs need a user review gate decision.
- `WAITING_FOR_SUBMISSION`: a work order is waiting for your submission.
- `NEEDS_ATTENTION`: an interrupted non-idempotent node; only the user can
  authorize `/ge-retry <run> <node>`.

## Errors

Responses with `"ok": false` carry a stable `error` code such as
`GRAPH_INVALID`, `DEPENDENCY_CYCLE`, `EXECUTOR_UNAVAILABLE`, `NODE_FAILED`,
`GATE_FAILED`, `STATE_CORRUPT` or `PLUGIN_CONFIGURATION_INVALID`. Report them
to the user; do not retry blindly.

## Spec essentials

Top level: `schema_version: 1`, `graph_id`, `title`, `inputs`, `nodes`.
Node: `id`, `purpose`, `executor` (`builtin` or `agent`), `depends_on`,
`requires` (capabilities such as `agent.code`, `agent.research`),
`inputs` (`{"name": {"from": "graph.inputs.x" | "<node>.<output>"}}`),
`outputs` (`{"name": {"type": "string"}}`), `success_criteria`
(`{"output": "name", "check": "non_empty"}`), `on_failure` (`stop`/`continue`),
`retry.max_attempts` (only for idempotent nodes), `gates`
(`approval` before execution, `review` after), `side_effects` (requires an
approval gate and a `rollback_boundary`).
