"""Default knowledge-graph adapter: optional and inert."""
from __future__ import annotations

from typing import Any, Mapping

from ..types import ImpactResult


class NoOpKnowledgeGraph:
    name = "noop"
    required = False

    def register_spec(self, meta: Mapping[str, Any]) -> None:
        return None

    def register_run(self, receipt: Mapping[str, Any]) -> None:
        return None

    def impact(self, node_ref: str) -> ImpactResult:
        return ImpactResult(supported=False, detail="no knowledge graph configured")
