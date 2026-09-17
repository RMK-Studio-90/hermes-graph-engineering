"""ge_runtime - deterministic task/execution graph runtime with governance.

The runtime is host-independent: it never imports Hermes, optional adapters,
presets or private integrations. Hosts integrate through thin bindings
(see ``ge_hermes``) and the typed extension points in ``ge_runtime.adapters``.

Independent community project; not affiliated with, endorsed by, or sponsored
by Nous Research.
"""
from .version import (
    RECEIPT_SCHEMA_VERSION,
    RUN_STATE_SCHEMA_VERSION,
    SPEC_SCHEMA_VERSION,
    TRACE_SCHEMA_VERSION,
    __version__,
)

__all__ = [
    "__version__",
    "SPEC_SCHEMA_VERSION",
    "RUN_STATE_SCHEMA_VERSION",
    "TRACE_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
]
