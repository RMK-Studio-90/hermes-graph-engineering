"""Default trace sink: append-only local JSONL file.

Each event is written as one canonical JSON line and fsynced before ``emit``
returns (the directory too when the file is created). Any write failure raises ``TraceWriteError``. Hash chaining and
redaction are applied by the runtime's trace layer, not by this sink.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from ..._fs import append_line_durable
from ..types import TraceWriteError


class JsonlTraceSink:
    name = "jsonl"

    def __init__(self, path: str | os.PathLike[str], required: bool = True) -> None:
        self.path = Path(path)
        self.required = required

    def emit(self, event: Mapping[str, Any]) -> None:
        try:
            line = json.dumps(dict(event), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            append_line_durable(self.path, line)
        except (OSError, TypeError, ValueError) as exc:
            raise TraceWriteError("%s: %s" % (type(exc).__name__, self.path)) from exc

    def flush(self) -> None:
        # every emit is already flushed and fsynced
        return None
