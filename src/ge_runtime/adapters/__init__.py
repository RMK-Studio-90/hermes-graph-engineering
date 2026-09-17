"""Typed extension points and their built-in defaults.

Every extension kind ships a built-in default, so a runtime with zero optional
adapters configured is complete. Optional integrations live outside this
package and are never imported by it.
"""
from .protocols import ApprovalProvider, ExecutorAdapter, KnowledgeGraphAdapter, Notifier, TraceSink
from .registry import AdapterKind, AdapterRegistry, UnknownAdapterError, default_registry

__all__ = [
    "AdapterKind",
    "AdapterRegistry",
    "ApprovalProvider",
    "ExecutorAdapter",
    "KnowledgeGraphAdapter",
    "Notifier",
    "TraceSink",
    "UnknownAdapterError",
    "default_registry",
]
