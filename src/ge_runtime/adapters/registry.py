"""Adapter registry.

Adapters are registered as named factories per kind. Resolution is explicit and
fails closed: an unknown name or a kind without a default raises
``UnknownAdapterError``. The registry never imports modules by name.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Callable

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class AdapterKind(str, Enum):
    KNOWLEDGE_GRAPH = "knowledge_graph"
    TRACE_SINK = "trace_sink"
    APPROVAL_PROVIDER = "approval_provider"
    NOTIFIER = "notifier"
    EXECUTOR = "executor"


class UnknownAdapterError(LookupError):
    """No adapter is registered under the requested kind/name."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[AdapterKind, dict[str, Callable[..., Any]]] = {kind: {} for kind in AdapterKind}
        self._defaults: dict[AdapterKind, str] = {}

    def register(self, kind: AdapterKind, name: str, factory: Callable[..., Any], *, default: bool = False) -> None:
        kind = AdapterKind(kind)
        if not _NAME.match(name):
            raise ValueError("invalid adapter name: %r" % name)
        if name in self._factories[kind]:
            raise ValueError("adapter already registered: %s/%s" % (kind.value, name))
        self._factories[kind][name] = factory
        if default:
            self._defaults[kind] = name

    def names(self, kind: AdapterKind) -> tuple[str, ...]:
        return tuple(sorted(self._factories[AdapterKind(kind)]))

    def default_name(self, kind: AdapterKind) -> str | None:
        return self._defaults.get(AdapterKind(kind))

    def create(self, kind: AdapterKind, name: str | None = None, **kwargs: Any) -> Any:
        kind = AdapterKind(kind)
        resolved = name if name is not None else self._defaults.get(kind)
        if resolved is None:
            raise UnknownAdapterError("no default adapter for kind %s" % kind.value)
        factory = self._factories[kind].get(resolved)
        if factory is None:
            raise UnknownAdapterError("unknown adapter %s/%s" % (kind.value, resolved))
        return factory(**kwargs)


def default_registry() -> AdapterRegistry:
    """Return a fresh registry holding exactly the built-in defaults."""
    from .defaults import (
        DryRunExecutor,
        FileApprovalProvider,
        JsonlTraceSink,
        LoggingNotifier,
        NoOpKnowledgeGraph,
    )

    registry = AdapterRegistry()
    registry.register(AdapterKind.KNOWLEDGE_GRAPH, NoOpKnowledgeGraph.name, NoOpKnowledgeGraph, default=True)
    registry.register(AdapterKind.TRACE_SINK, JsonlTraceSink.name, JsonlTraceSink, default=True)
    registry.register(AdapterKind.APPROVAL_PROVIDER, FileApprovalProvider.name, FileApprovalProvider, default=True)
    registry.register(AdapterKind.NOTIFIER, LoggingNotifier.name, LoggingNotifier, default=True)
    registry.register(AdapterKind.EXECUTOR, DryRunExecutor.name, DryRunExecutor, default=True)
    return registry
