"""Durable run storage: checksummed state, hash-chained trace, receipts, locks.

Layout below the store root::

    runs/<run_id>/state.json    checksummed envelope, replaced atomically
    runs/<run_id>/trace.jsonl   append-only, hash-chained events
    runs/<run_id>/receipt.json  last verification receipt
    runs/<run_id>/.lock         inter-process lock (OS file lock)

A state file that cannot be parsed, has the wrong schema version or a checksum
mismatch is reported as ``STATE_CORRUPT`` and left untouched for diagnosis.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ._fs import atomic_write_text
from .adapters.defaults.trace_jsonl import JsonlTraceSink
from .adapters.types import TraceWriteError
from .errors import ErrorCode, GraphEngineeringError
from .spec import canonical_json, digest_of
from .version import RUN_STATE_SCHEMA_VERSION, TRACE_SCHEMA_VERSION

RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}-[0-9]{8}t[0-9]{6}z-[0-9a-f]{6}$")
STATE_KIND = "ge-run-state"
LOCK_TIMEOUT_SECONDS = 5.0

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / "runs"

    # -- identity ---------------------------------------------------------------
    def new_run_id(self, graph_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
        for _ in range(16):
            run_id = "%s-%s-%s" % (graph_id[:64], stamp, secrets.token_hex(3))
            if not self.run_dir(run_id).exists():
                return run_id
        raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "could not allocate a unique run id")

    def run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "invalid run id %r" % (run_id,))
        return self.runs_dir / run_id

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "state.json").is_file()

    def list_runs(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        found = []
        for child in self.runs_dir.iterdir():
            if child.is_dir() and RUN_ID_RE.match(child.name) and (child / "state.json").is_file():
                found.append((child.stat().st_mtime, child.name))
        return [name for _, name in sorted(found, reverse=True)]

    # -- state ------------------------------------------------------------------
    def save(self, run_id: str, state: Mapping[str, Any]) -> None:
        if state.get("run_id") != run_id:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "state does not belong to run %s" % run_id)
        payload = json.loads(canonical_json(dict(state)))
        envelope = {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "kind": STATE_KIND,
            "checksum": digest_of(payload),
            "state": payload,
        }
        path = self.run_dir(run_id) / "state.json"
        try:
            atomic_write_text(path, json.dumps(envelope, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise _write_failed(run_id, "write the run state", path, exc) from exc

    def load(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "state.json"
        if not path.is_file():
            raise GraphEngineeringError(ErrorCode.RUN_NOT_FOUND, "no run %s" % run_id)

        def corrupt(reason: str) -> GraphEngineeringError:
            return GraphEngineeringError(ErrorCode.STATE_CORRUPT, "run %s state rejected: %s" % (run_id, reason),
                                         {"path_name": "runs/%s/state.json" % run_id, "reason": reason})

        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise corrupt("unreadable JSON (%s)" % type(exc).__name__) from exc
        if not isinstance(envelope, dict) or envelope.get("kind") != STATE_KIND:
            raise corrupt("not a run state envelope")
        if envelope.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
            raise corrupt("unsupported schema_version %r" % envelope.get("schema_version"))
        state = envelope.get("state")
        if not isinstance(state, dict):
            raise corrupt("state is not an object")
        if envelope.get("checksum") != digest_of(state):
            raise corrupt("checksum mismatch")
        if state.get("run_id") != run_id:
            raise corrupt("run id mismatch")
        return state

    # -- locking ----------------------------------------------------------------
    @contextmanager
    def lock(self, run_id: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Exclusive per-run lock across threads and processes; released on process death."""
        lock_path = self.run_dir(run_id) / ".lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _write_failed(run_id, "create the run directory", lock_path.parent, exc) from exc
        key = str(lock_path.resolve(strict=False))
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        if not thread_lock.acquire(timeout=timeout):
            raise GraphEngineeringError(ErrorCode.RUN_LOCKED, "run %s is busy in this process" % run_id)
        try:
            try:
                lock_handle = open(lock_path, "a+b")
            except OSError as exc:
                raise _write_failed(run_id, "open the run lock", lock_path, exc) from exc
            with lock_handle as handle:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        _os_lock(handle)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise GraphEngineeringError(ErrorCode.RUN_LOCKED,
                                                        "run %s is locked by another process" % run_id) from None
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    _os_unlock(handle)
        finally:
            thread_lock.release()

    # -- trace / receipt --------------------------------------------------------
    def append_trace(self, run_id: str, event_type: str, **fields: Any) -> dict[str, Any]:
        path = self.run_dir(run_id) / "trace.jsonl"
        seq, prev = 0, None
        if path.is_file():
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                try:
                    last = json.loads(lines[-1])
                    seq, prev = int(last["seq"]), last["hash"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise GraphEngineeringError(ErrorCode.STATE_CORRUPT, "run %s trace tail unreadable" % run_id) from exc
        event = {"schema_version": TRACE_SCHEMA_VERSION, "seq": seq + 1, "ts": utc_now(), "type": event_type,
                 "prev": prev}
        event.update(fields)
        event["hash"] = digest_of(event)
        try:
            JsonlTraceSink(path).emit(event)
        except TraceWriteError as exc:
            raise GraphEngineeringError(ErrorCode.TRACE_WRITE_FAILED, "trace write failed for run %s%s" % (
                run_id, _path_hint(path)), {"path_length": len(str(path))}) from exc
        return event

    def read_trace(self, run_id: str) -> list[dict[str, Any]]:
        path = self.run_dir(run_id) / "trace.jsonl"
        if not path.is_file():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def verify_trace(self, run_id: str) -> tuple[bool, str]:
        try:
            events = self.read_trace(run_id)
        except ValueError:
            return False, "trace contains unreadable lines"
        prev = None
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                return False, "trace event %d is not an object" % index
            body = {k: v for k, v in event.items() if k != "hash"}
            if event.get("seq") != index or event.get("prev") != prev or event.get("hash") != digest_of(body):
                return False, "trace chain broken at event %d" % index
            prev = event["hash"]
        return True, "%d trace event(s) chained" % len(events)

    def write_receipt(self, run_id: str, receipt: Mapping[str, Any]) -> None:
        path = self.run_dir(run_id) / "receipt.json"
        try:
            atomic_write_text(path, json.dumps(dict(receipt), indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise _write_failed(run_id, "write the verification receipt", path, exc) from exc


# Windows rejects paths of 260+ characters unless long paths are enabled; temp files add ~30 characters.
_WINDOWS_PATH_LIMIT = 260
_TEMP_SUFFIX_ALLOWANCE = 30


def _path_hint(path: Path) -> str:
    length = len(str(path)) + _TEMP_SUFFIX_ALLOWANCE
    if os.name == "nt" and length >= _WINDOWS_PATH_LIMIT:
        return ("; the run path is about %d characters and Windows limits paths to %d unless long paths "
                "are enabled: use a shorter Hermes home or enable long paths" % (length, _WINDOWS_PATH_LIMIT))
    return ""


def _write_failed(run_id: str, action: str, path: Path, exc: OSError) -> GraphEngineeringError:
    """Actionable error for filesystem write failures (never leaks the absolute path)."""
    return GraphEngineeringError(
        ErrorCode.STATE_WRITE_FAILED,
        "could not %s for run %s (%s)%s" % (action, run_id, type(exc).__name__, _path_hint(path)),
        {"reason": type(exc).__name__, "path_length": len(str(path))},
    )


def _os_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
