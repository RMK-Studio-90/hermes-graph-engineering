# Example: deterministic text pipeline

A four-node graph that runs entirely on the `builtin` executor: no model, network or
filesystem access, so every run produces the same result.

```
read_input ─▶ validate_input ─▶ transform_input ─▶ verify_result
```

| Node | Operation | Success criterion |
| --- | --- | --- |
| `read_input` | `identity` of `graph.inputs.text` | `value` is a string |
| `validate_input` | `require` non-empty string, max 10000 chars | `valid` is `true` |
| `transform_input` | `text_transform`: collapse whitespace, upper-case | `value` is non-empty |
| `verify_result` | `require` pattern: upper case, single spaces, no padding | `valid` is `true` |

`spec.yaml` is identical to the built-in template `text-pipeline`.

## Run it in Hermes

Using the built-in template (optionally with your own text):

```
/ge-create --template text-pipeline --text "  hello   graph  "
/ge-plan last
/ge-approve last plan
/ge-run last
/ge-status last
/ge-explain last
/ge-verify last
```

Or from the spec file (the path must be readable by the Hermes process):

```
/ge-validate <path-to>/examples/text-pipeline/spec.yaml
/ge-create <path-to>/examples/text-pipeline/spec.yaml
```

In Telegram use the underscore form: `/ge_create --template text-pipeline`,
`/ge_approve last plan`, `/ge_run last`, `/ge_verify last`.

Expected result: the run ends `SUCCEEDED` with all four nodes `SUCCEEDED`, the
transformed value is `HELLO GRAPH`, and `/ge-verify` reports `VERIFIED`.

## Run it without Hermes

```python
from ge_runtime.engine import GraphEngine
from ge_runtime.store import RunStore

engine = GraphEngine(RunStore("./example-state"))
run_id = engine.create(open("examples/text-pipeline/spec.yaml", encoding="utf-8").read())["run_id"]
engine.decide(run_id, "plan", approve=True, decided_by="me")
print(engine.execute(run_id)["status"])      # SUCCEEDED
print(engine.verify(run_id)["verified"])     # True
```

## Agent-driven variant

`/ge-analyze` turns a task description into a graph whose nodes are performed by the
Hermes agent:

```
/ge-analyze Create an execution graph for: 1. read input 2. validate input 3. transform input 4. verify result
```

The agent receives work orders through the `ge_graph` tool and submits outputs, which
are checked against each node's output contract and success criteria.
