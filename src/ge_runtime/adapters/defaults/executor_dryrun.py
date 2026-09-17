"""Default executor: deterministic dry run without side effects.

Produces one placeholder value per declared output. The values depend only on
the node id and the output name, so repeated attempts are identical.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..types import CostSource, ExecutionRequest, ExecutorOutcome, ExecutorResult, Usage


def _declared_outputs(contract: Mapping[str, Any]) -> list[str] | None:
    outputs = contract.get("outputs", [])
    if isinstance(outputs, Mapping):
        return [str(name) for name in outputs]
    if isinstance(outputs, (list, tuple)) and all(isinstance(name, str) for name in outputs):
        return list(outputs)
    return None


class DryRunExecutor:
    name = "dry-run"

    def execute(self, request: ExecutionRequest) -> ExecutorResult:
        names = _declared_outputs(request.contract)
        if names is None:
            return ExecutorResult(ExecutorOutcome.NON_RETRYABLE_FAILURE, detail="unreadable output declaration")
        outputs = {name: "dry-run:%s:%s" % (request.node_id, name) for name in names}
        return ExecutorResult(
            ExecutorOutcome.SUCCESS,
            outputs=outputs,
            usage=Usage(cost=0.0, cost_source=CostSource.ADAPTER_ATTESTED),
        )

    def cancel(self, execution_id: str) -> bool:
        # dry runs complete synchronously; there is nothing to cancel
        return False
