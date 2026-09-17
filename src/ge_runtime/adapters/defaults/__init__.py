"""Built-in default adapters: a complete runnable system with zero optional adapters."""
from .approval_file import FileApprovalProvider
from .executor_dryrun import DryRunExecutor
from .kg_noop import NoOpKnowledgeGraph
from .notifier_log import LoggingNotifier
from .trace_jsonl import JsonlTraceSink

__all__ = [
    "DryRunExecutor",
    "FileApprovalProvider",
    "JsonlTraceSink",
    "LoggingNotifier",
    "NoOpKnowledgeGraph",
]
