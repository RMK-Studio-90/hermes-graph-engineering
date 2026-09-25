"""Helper process for multi-process and kill tests: drives one run with a slow, recording worker.

usage: dispatch_proc.py <root> <run_id> <log_file> <sleep_seconds> [ttl_seconds]

Every worker execution appends "<pid> <node> <attempt>" to <log_file> *before* sleeping, so a
test can tell exactly how often a node was really executed, even if this process is killed.
Prints the dispatcher report as JSON.
"""
import json
import os
import sys
import time

from ge_runtime.adapters.types import ExecutorOutcome, ExecutorResult
from ge_runtime.dispatch import AutonomousDispatcher
from ge_runtime.engine import GraphEngine
from ge_runtime.store import RunStore


class SlowWorker:
    def __init__(self, log, sleep):
        self.log, self.sleep = log, sleep

    def supports_agent_execution(self):
        return True

    def execute_agent_work(self, order, context):
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write("%d %s %d\n" % (os.getpid(), order["node"], order["attempt"]))
            handle.flush()
            os.fsync(handle.fileno())
        time.sleep(self.sleep)
        return ExecutorResult(ExecutorOutcome.SUCCESS, outputs={"result": "by-%d" % os.getpid()})


def main():
    root, run_id, log, sleep = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    ttl = float(sys.argv[5]) if len(sys.argv) > 5 else 60.0
    engine = GraphEngine(RunStore(root))
    report = AutonomousDispatcher(engine, SlowWorker(log, sleep), state_key="proc:%d" % os.getpid(),
                                  lease_ttl_seconds=ttl).drive(run_id)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
