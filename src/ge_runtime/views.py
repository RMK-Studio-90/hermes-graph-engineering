"""Input views: what exactly a node's worker may see of an upstream artifact.

Nodes never share a conversation. A worker receives only its own resolved inputs, and an
input may be *projected* before it is handed over::

    "inputs": {"findings": {"from": "hunt.findings", "type": "array",
                            "view": {"pick": ["id", "title", "claim", "location", "severity"]}}}

View operations (applied in this order, each optional):

* ``pick``: keep only these fields of an object, or of every object in a list;
* ``omit``: drop these fields (for example ``reasoning``);
* ``attach_source``: for every object whose ``location`` field reads ``path:start-end``
  (or ``path:line``), attach the exact source lines from the run workspace as
  ``source`` (``{"path", "start", "end", "text", "sha256"}``), with ``context_lines`` of
  context and at most ``max_chars`` characters. The worker sees the code the finding is
  about without seeing how the finding was produced.

This is how a blind reviewer sees findings but no code and no hunter reasoning, and how
an independent verifier sees a finding plus its source ranges but not the reasoning chain.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import ErrorCode, GraphEngineeringError

_LOCATION = re.compile(r"^(?P<path>[^:\n]+):(?P<start>\d+)(?:-(?P<end>\d+))?$")
MAX_ITEMS = 500


def view_errors(view: Any, where: str) -> list[str]:
    if not isinstance(view, Mapping):
        return ["%s: view must be a mapping" % where]
    errors = []
    unknown = sorted(set(view) - {"pick", "omit", "attach_source"})
    if unknown:
        errors.append("%s: unknown view operation(s) %s" % (where, ", ".join(unknown)))
    for key in ("pick", "omit"):
        if key in view and (not isinstance(view[key], list) or not all(isinstance(f, str) and f for f in view[key])):
            errors.append("%s: view.%s must be a list of field names" % (where, key))
    if "attach_source" in view:
        spec = view["attach_source"]
        if spec is True:
            spec = {}
        if not isinstance(spec, Mapping) or set(spec) - {"field", "context_lines", "max_chars"}:
            errors.append("%s: view.attach_source must be true or {field, context_lines, max_chars}" % where)
        else:
            for key, low, high in (("context_lines", 0, 50), ("max_chars", 100, 20_000)):
                value = spec.get(key)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                          or not low <= value <= high):
                    errors.append("%s: view.attach_source.%s must be %d..%d" % (where, key, low, high))
    return errors


def _project(item: Any, view: Mapping[str, Any]) -> Any:
    if not isinstance(item, Mapping):
        return item
    result = dict(item)
    if "pick" in view:
        result = {k: v for k, v in result.items() if k in view["pick"]}
    if "omit" in view:
        result = {k: v for k, v in result.items() if k not in view["omit"]}
    return result


def read_source_range(workspace: str | None, location: Any, context_lines: int = 3,
                      max_chars: int = 6000) -> dict[str, Any] | None:
    """The exact lines ``path:start-end`` of a workspace file, or None if it does not resolve."""
    if not workspace or not isinstance(location, str):
        return None
    match = _LOCATION.match(location.strip())
    if not match:
        return None
    root = Path(workspace).resolve()
    target = (root / match.group("path").strip()).resolve()
    if target != root and root not in target.parents or not target.is_file():
        return None
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start or start > len(lines):
        return None
    lo, hi = max(1, start - context_lines), min(len(lines), end + context_lines)
    text = "\n".join("%d: %s" % (n, lines[n - 1]) for n in range(lo, hi + 1))
    if len(text) > max_chars:
        text = text[:max_chars] + "\n..."
    return {"path": target.relative_to(root).as_posix(), "start": lo, "end": hi, "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def apply_view(value: Any, view: Mapping[str, Any], *, workspace: str | None = None) -> Any:
    items = value if isinstance(value, list) else None
    if items is not None and len(items) > MAX_ITEMS:
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "view input has more than %d items" % MAX_ITEMS)
    projected = [_project(i, view) for i in items] if items is not None else _project(value, view)
    attach = view.get("attach_source")
    if attach:
        spec = attach if isinstance(attach, Mapping) else {}
        field = spec.get("field", "location")
        originals = items if items is not None else [value]
        targets = projected if isinstance(projected, list) else [projected]
        for original, item in zip(originals, targets):
            if isinstance(item, dict) and isinstance(original, Mapping):
                # the location is read from the original item: a view may hide it from the worker
                item["source"] = read_source_range(workspace, original.get(field), spec.get("context_lines", 3),
                                                   spec.get("max_chars", 6000))
    return projected
