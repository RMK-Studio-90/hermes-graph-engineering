"""Package version and persisted-contract schema versions.

Every persisted public artifact carries its schema version. Readers reject
unsupported versions instead of coercing them.
"""
__version__ = "1.1.0"

SPEC_SCHEMA_VERSION = 1
RUN_STATE_SCHEMA_VERSION = 1
TRACE_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
