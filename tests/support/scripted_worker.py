"""A deterministic node worker process for headless tests (stands in for ``hermes -z``).

Reads the node prompt (argv[1] or stdin), does what a step like 'Create `f` containing "t"'
asks inside the workspace named in the prompt, logs "<pid> <node> <toolsets>" and answers with
the declared outputs as a JSON block. GE_TEST_WORKER_SLEEP delays the answer (kill tests).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

prompt = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else sys.stdin.read()
toolsets = sys.argv[sys.argv.index("--toolsets") + 1] if "--toolsets" in sys.argv else ""
node = re.search(r"Node: (\S+)", prompt).group(1)
workspace = Path(re.search(r"Workspace directory: (.+?) \(keep", prompt).group(1))
log = os.environ.get("GE_TEST_WORKER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("%d %s %s %s\n" % (os.getpid(), node, toolsets or "-", os.environ.get("GE_WORKER_RISK", "-")))
purpose = prompt.split("Purpose:\n", 1)[1].split("\n", 1)[0]
match = re.search(r"[Cc]reate `([^`]+)` containing \"([^\"]+)\"", purpose)
if match:
    (workspace / match.group(1)).write_text(match.group(2) + "\n", encoding="utf-8")
time.sleep(float(os.environ.get("GE_TEST_WORKER_SLEEP", "0")))
contract = prompt.split("Required output contract - a JSON object with exactly these keys:\n", 1)[1].split("\n\n")[0]
outputs = {}
for line in contract.splitlines():
    name, _, kind = line[2:].partition(": ")
    outputs[name] = True if kind.startswith("boolean") else [] if kind.startswith("array") else "done %s" % node
print("Finished.\n```json\n%s\n```" % json.dumps(outputs))
