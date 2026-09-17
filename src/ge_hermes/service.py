"""Shared glue between Hermes entry points (commands, tool) and ge_runtime.

Configuration is read on every call so settings changes apply without a
restart; the state directory is fixed at registration time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ge_runtime.engine import GraphEngine
from ge_runtime.errors import ErrorCode, GraphEngineeringError
from ge_runtime.executors import ExecutorCatalog
from ge_runtime.spec import MAX_SPEC_BYTES, load_spec_text
from ge_runtime.store import RunStore

from .config import PluginConfig, parse_config
from .templates import TEMPLATES, build_template

LATEST_ALIASES = ("last", "latest")
SPEC_FILE_SUFFIXES = (".json", ".yaml", ".yml")


class GraphService:
    def __init__(self, data_dir: str | Path, get_config: Callable[[str, Any], Any]) -> None:
        self.data_dir = Path(data_dir)
        self._get_config = get_config

    def config(self) -> PluginConfig:
        return parse_config(self._get_config)

    def engine(self) -> GraphEngine:
        config = self.config()
        return GraphEngine(
            RunStore(self.data_dir),
            ExecutorCatalog(disabled=config.disabled_executors),
            require_plan_approval=config.require_plan_approval,
            max_steps=config.max_steps,
        )

    def resolve_run(self, engine: GraphEngine, token: Any) -> str:
        if not isinstance(token, str) or not token.strip():
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "a run id (or 'last') is required")
        token = token.strip()
        if token in LATEST_ALIASES:
            runs = engine.store.list_runs()
            if not runs:
                raise GraphEngineeringError(ErrorCode.RUN_NOT_FOUND, "there are no runs yet")
            return runs[0]
        engine.store.run_dir(token)  # validates the id format
        return token

    @staticmethod
    def spec_from_source(source: Any) -> Any:
        """Accept a spec mapping, inline JSON/YAML text, or a path to a .json/.yaml file."""
        if isinstance(source, Mapping):
            return dict(source)
        if not isinstance(source, str) or not source.strip():
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "a spec (inline JSON/YAML or file path) is required")
        text = source.strip()
        if "\n" not in text and text.lower().endswith(SPEC_FILE_SUFFIXES):
            path = Path(text.strip('"'))
            if not path.is_file():
                raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "spec file not found: %s" % path.name)
            if path.stat().st_size > MAX_SPEC_BYTES:
                raise GraphEngineeringError(ErrorCode.GRAPH_INVALID, "spec file exceeds %d bytes" % MAX_SPEC_BYTES)
            try:
                return load_spec_text(path.read_text(encoding="utf-8"))
            except (GraphEngineeringError, UnicodeDecodeError) as exc:
                # parser messages quote file content; never echo a local file into chat
                raise GraphEngineeringError(ErrorCode.GRAPH_INVALID,
                                            "spec file %s is not valid JSON or YAML" % path.name) from exc
        return load_spec_text(text)

    @staticmethod
    def spec_from_template(name: str, text: str | None = None) -> dict[str, Any]:
        if name not in TEMPLATES:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "unknown template %r (known: %s)" % (
                name, ", ".join(sorted(TEMPLATES))))
        return build_template(name, text)
