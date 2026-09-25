"""Durable run storage: checksummed state bound to a hash-chained journal.

``StateStore`` is the storage contract the engine depends on. ``LocalStateStore``
(alias ``RunStore``) is the default and keeps everything on the local filesystem::

    runs/<run_id>/state.json         checksummed envelope, replaced atomically (+ directory fsync)
    runs/<run_id>/trace.jsonl        append-only, hash-chained journal, every line fsynced
    runs/<run_id>/receipt.json       terminal receipt, checksummed, replaced atomically
    runs/<run_id>/artifacts/*.json   evidence and work-order artifacts, checksummed
    runs/<run_id>/lease.json         durable dispatch lease (owner, heartbeat, expiry)
    runs/<run_id>/quarantine/        damaged trace bytes moved aside, never deleted
    runs/<run_id>/.lock              inter-process lock (OS file lock)

State <-> journal binding
-------------------------
Every saved state carries ``revision`` (monotonic) and ``journal`` (the ``seq`` and
``hash`` of the last trace event it reflects). After the state file is replaced the
store appends ``state.saved`` with the new revision and checksum. On load the engine
compares the state with the journal (see ``scan_trace``): a truncated or diverged
journal, a torn final line and a state older than a recorded ``state.saved`` are all
detected instead of being trusted.

A state file that cannot be parsed, has an unsupported schema version or a checksum
mismatch is reported as ``STATE_CORRUPT`` and left untouched for diagnosis. Schema
version 1 states (1.x releases) are migrated to version 2 in memory on load and
persisted as version 2 on the next save.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from ._fs import append_line_durable, atomic_write_bytes, atomic_write_text, fsync_dir
from .adapters.types import TraceWriteError
from .errors import ErrorCode, GraphEngineeringError
from .spec import canonical_json, digest_of
from .version import RECEIPT_SCHEMA_VERSION, RUN_STATE_SCHEMA_VERSION, TRACE_SCHEMA_VERSION

RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}-[0-9]{8}t[0-9]{6}z-[0-9a-f]{6}$")
ARTIFACT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
STATE_KIND = "ge-run-state"
RECEIPT_KIND = "ge-run-receipt"
ARTIFACT_KIND = "ge-artifact"
LOCK_TIMEOUT_SECONDS = 5.0
SUPPORTED_STATE_SCHEMAS = (1, RUN_STATE_SCHEMA_VERSION)
SUPPORTED_TRACE_SCHEMAS = (1, TRACE_SCHEMA_VERSION)

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()  # lock path -> depth held by the current thread (the OS lock is taken once)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_precise() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: Any) -> float | None:
    """Epoch seconds of a stored UTC timestamp, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class TraceScan:
    """Result of reading a journal: its valid chained prefix and what (if anything) follows it."""

    status: str  # "ok" | "missing" | "torn_tail" | "corrupt"
    events: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    valid_bytes: int = 0
    total_bytes: int = 0

    @property
    def head(self) -> dict[str, Any]:
        if not self.events:
            return {"seq": 0, "hash": None}
        return {"seq": self.events[-1]["seq"], "hash": self.events[-1]["hash"]}


def _verify_event(event: Any, expected_seq: int, prev: str | None) -> str:
    if not isinstance(event, dict):
        return "event is not an object"
    if event.get("schema_version") not in SUPPORTED_TRACE_SCHEMAS:
        return "unsupported event schema_version %r" % event.get("schema_version")
    body = {k: v for k, v in event.items() if k != "hash"}
    if event.get("seq") != expected_seq or event.get("prev") != prev or event.get("hash") != digest_of(body):
        return "chain broken"
    return ""


def scan_trace_bytes(data: bytes) -> TraceScan:
    """Split journal bytes into the valid hash-chained prefix and the damaged remainder.

    * a final fragment without a newline is ``torn_tail``: every event is written as one
      ``line + newline`` append, so only a write interrupted by a crash leaves one; it is
      safe to cut off;
    * any other bad line is ``corrupt``: the journal was altered and cannot
      be repaired without losing provable history.
    """
    if not data:
        return TraceScan("ok")
    events: list[dict[str, Any]] = []
    offset, prev, total = 0, None, len(data)
    while offset < total:
        newline = data.find(b"\n", offset)
        complete = newline != -1
        end = newline + 1 if complete else total
        chunk = data[offset:end].strip()
        if not chunk:
            offset = end
            continue
        problem = ""
        try:
            event = json.loads(chunk.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            event, problem = None, "unreadable line"
        if not problem:
            problem = _verify_event(event, len(events) + 1, prev)
        if problem or not complete:
            if not complete:
                return TraceScan("torn_tail", events, "final line %s" % (problem or "has no newline"),
                                 offset, total)
            return TraceScan("corrupt", events, "event %d: %s" % (len(events) + 1, problem), offset, total)
        events.append(event)
        prev = event["hash"]
        offset = end
    return TraceScan("ok", events, "%d event(s) chained" % len(events), total, total)


@runtime_checkable
class StateStore(Protocol):
    """Storage contract of the engine. The local filesystem store is the default implementation;
    other backends (for example a database or object store for cloud runners) implement the same
    methods with the same atomicity and locking guarantees."""

    def new_run_id(self, graph_id: str) -> str: ...
    def exists(self, run_id: str) -> bool: ...
    def list_runs(self) -> list[str]: ...
    def save(self, run_id: str, state: Mapping[str, Any]) -> dict[str, Any]: ...
    def load(self, run_id: str) -> dict[str, Any]: ...
    def lock(self, run_id: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> Any: ...
    def journal_head(self, run_id: str) -> dict[str, Any]: ...
    def append_trace(self, run_id: str, event_type: str, **fields: Any) -> dict[str, Any]: ...
    def read_trace(self, run_id: str) -> list[dict[str, Any]]: ...
    def scan_trace(self, run_id: str) -> TraceScan: ...
    def verify_trace(self, run_id: str) -> tuple[bool, str]: ...
    def repair_torn_tail(self, run_id: str) -> dict[str, Any]: ...
    def quarantine_trace(self, run_id: str, reason: str) -> dict[str, Any]: ...
    def write_receipt(self, run_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]: ...
    def read_receipt(self, run_id: str) -> dict[str, Any] | None: ...
    def write_artifact(self, run_id: str, name: str, payload: Any) -> dict[str, Any]: ...
    def read_artifact(self, run_id: str, name: str) -> Any: ...
    def read_lease(self, run_id: str) -> dict[str, Any] | None: ...
    def write_lease(self, run_id: str, lease: Mapping[str, Any]) -> None: ...
    def delete_lease(self, run_id: str) -> None: ...


class LocalStateStore:
    """Filesystem implementation of :class:`StateStore` (the default)."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        # journal head per trace file, valid while (size, mtime) are unchanged; avoids rescanning on every append
        self._heads: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._heads_lock = threading.Lock()

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
            state = child / "state.json"
            if child.is_dir() and RUN_ID_RE.match(child.name) and state.is_file():
                found.append((state.stat().st_mtime_ns, child.name))
        return [name for _, name in sorted(found, reverse=True)]

    # -- state ------------------------------------------------------------------
    def save(self, run_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
        """Persist ``state`` bound to the current journal head; returns the ``state.saved`` event.

        Callers hold the run lock. Keys starting with ``_`` are transient and never persisted.
        """
        if state.get("run_id") != run_id:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "state does not belong to run %s" % run_id)
        payload = json.loads(canonical_json({k: v for k, v in dict(state).items() if not k.startswith("_")}))
        payload["state_schema_version"] = RUN_STATE_SCHEMA_VERSION
        payload["revision"] = int(payload.get("revision") or 0) + 1
        payload["journal"] = self.journal_head(run_id)
        checksum = digest_of(payload)
        envelope = {"schema_version": RUN_STATE_SCHEMA_VERSION, "kind": STATE_KIND, "checksum": checksum,
                    "state": payload}
        path = self.run_dir(run_id) / "state.json"
        try:
            atomic_write_text(path, json.dumps(envelope, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise _write_failed(run_id, "write the run state", path, exc) from exc
        if isinstance(state, dict):
            state["revision"], state["journal"], state["state_schema_version"] = (
                payload["revision"], payload["journal"], RUN_STATE_SCHEMA_VERSION)
            state["_checksum"] = checksum
        return self.append_trace(run_id, "state.saved", revision=payload["revision"], checksum=checksum,
                                 status=payload.get("status"))

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
        version = envelope.get("schema_version")
        if version not in SUPPORTED_STATE_SCHEMAS:
            raise corrupt("unsupported schema_version %r" % version)
        state = envelope.get("state")
        if not isinstance(state, dict):
            raise corrupt("state is not an object")
        if envelope.get("checksum") != digest_of(state):
            raise corrupt("checksum mismatch")
        if state.get("run_id") != run_id:
            raise corrupt("run id mismatch")
        if version == 1:
            state = migrate_state_v1(state)
        state["_checksum"] = envelope["checksum"]
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
        held = getattr(_HELD, "paths", None)
        if held is None:
            held = _HELD.paths = {}
        try:
            if held.get(key):
                # re-entered by the thread that already holds the OS lock: flock on a second
                # descriptor of the same file would block against ourselves
                held[key] += 1
                try:
                    yield
                finally:
                    held[key] -= 1
                return
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
                        time.sleep(0.02)
                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)
                    _os_unlock(handle)
        finally:
            thread_lock.release()

    # -- journal ----------------------------------------------------------------
    def _trace_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "trace.jsonl"

    def scan_trace(self, run_id: str) -> TraceScan:
        path = self._trace_path(run_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return TraceScan("missing", detail="no journal")
        except OSError as exc:
            return TraceScan("corrupt", detail="journal unreadable (%s)" % type(exc).__name__)
        return scan_trace_bytes(data)

    def journal_head(self, run_id: str) -> dict[str, Any]:
        """Head of an intact journal; raises ``TRACE_CORRUPT`` for a damaged one."""
        path = self._trace_path(run_id)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return {"seq": 0, "hash": None}
        key = str(path)
        with self._heads_lock:
            cached = self._heads.get(key)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return dict(cached[2])
        scan = self.scan_trace(run_id)
        if scan.status not in ("ok", "missing"):
            raise GraphEngineeringError(ErrorCode.TRACE_CORRUPT, "run %s journal is damaged (%s); it must be "
                                        "repaired before it can be extended" % (run_id, scan.detail),
                                        {"trace_status": scan.status})
        self._remember_head(path, scan.head)
        return scan.head

    def _remember_head(self, path: Path, head: dict[str, Any]) -> None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        with self._heads_lock:
            self._heads[str(path)] = (stat.st_size, stat.st_mtime_ns, dict(head))

    def append_trace(self, run_id: str, event_type: str, **fields: Any) -> dict[str, Any]:
        """Append one chained event. Callers hold the run lock; a damaged journal is never extended."""
        path = self._trace_path(run_id)
        head = self.journal_head(run_id)
        event = {"schema_version": TRACE_SCHEMA_VERSION, "seq": head["seq"] + 1, "ts": utc_now_precise(),
                 "type": event_type, "prev": head["hash"]}
        event.update(fields)
        event["hash"] = digest_of(event)
        try:
            line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            append_line_durable(path, line)
        except (OSError, TypeError, ValueError) as exc:
            raise GraphEngineeringError(ErrorCode.TRACE_WRITE_FAILED, "trace write failed for run %s%s" % (
                run_id, _path_hint(path)), {"path_length": len(str(path))}) from TraceWriteError(str(exc))
        self._remember_head(path, {"seq": event["seq"], "hash": event["hash"]})
        return event

    def read_trace(self, run_id: str) -> list[dict[str, Any]]:
        """The valid chained prefix of the journal (damaged remainders are reported by ``scan_trace``)."""
        return self.scan_trace(run_id).events

    def verify_trace(self, run_id: str) -> tuple[bool, str]:
        scan = self.scan_trace(run_id)
        if scan.status in ("ok", "missing"):
            return True, "%d trace event(s) chained" % len(scan.events)
        return False, "trace %s: %s" % (scan.status, scan.detail)

    def _quarantine_path(self, run_id: str, label: str, suffix: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S%fz")
        return self.run_dir(run_id) / "quarantine" / ("%s-%s%s" % (label, stamp, suffix))

    def repair_torn_tail(self, run_id: str) -> dict[str, Any]:
        """Move an interrupted final line aside and cut the journal back to its valid prefix."""
        path = self._trace_path(run_id)
        data = path.read_bytes()
        scan = scan_trace_bytes(data)
        if scan.status != "torn_tail":
            raise GraphEngineeringError(ErrorCode.TRACE_CORRUPT, "run %s journal has no torn tail to repair" % run_id)
        fragment = data[scan.valid_bytes:]
        target = self._quarantine_path(run_id, "trace-torn", ".bin")
        atomic_write_bytes(target, fragment)
        with open(path, "r+b") as handle:
            handle.truncate(scan.valid_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        return {"kind": "trace_torn_tail_repaired", "dropped_bytes": len(fragment),
                "fragment_digest": _bytes_digest(fragment), "quarantine": "quarantine/" + target.name,
                "kept_events": len(scan.events), "detail": scan.detail}

    def quarantine_trace(self, run_id: str, reason: str) -> dict[str, Any]:
        """Move the whole journal aside (it is never deleted) so a fresh journal can start."""
        path = self._trace_path(run_id)
        data = path.read_bytes() if path.is_file() else b""
        target = self._quarantine_path(run_id, "trace-corrupt", ".jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            os.replace(path, target)
            fsync_dir(target.parent)
            fsync_dir(path.parent)
        return {"kind": "trace_quarantined", "reason": reason, "quarantine": "quarantine/" + target.name,
                "quarantined_bytes": len(data), "quarantined_digest": _bytes_digest(data)}

    # -- receipts ---------------------------------------------------------------
    def write_receipt(self, run_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in dict(receipt).items() if k != "checksum"}
        body.setdefault("kind", RECEIPT_KIND)
        body.setdefault("receipt_schema_version", RECEIPT_SCHEMA_VERSION)
        body = json.loads(canonical_json(body))
        sealed = dict(body, checksum=digest_of(body))
        path = self.run_dir(run_id) / "receipt.json"
        try:
            atomic_write_text(path, json.dumps(sealed, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise _write_failed(run_id, "write the receipt", path, exc) from exc
        return sealed

    def read_receipt(self, run_id: str) -> dict[str, Any] | None:
        """The stored receipt, or None if there is none. A damaged receipt raises ``RECEIPT_CORRUPT``."""
        path = self.run_dir(run_id) / "receipt.json"
        if not path.is_file():
            return None
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GraphEngineeringError(ErrorCode.RECEIPT_CORRUPT, "run %s receipt is unreadable" % run_id) from exc
        if not isinstance(receipt, dict):
            raise GraphEngineeringError(ErrorCode.RECEIPT_CORRUPT, "run %s receipt is not an object" % run_id)
        version = receipt.get("receipt_schema_version")
        if version == 1:
            return receipt  # legacy receipts carry no checksum; callers regenerate them
        if version != RECEIPT_SCHEMA_VERSION or receipt.get("kind") != RECEIPT_KIND:
            raise GraphEngineeringError(ErrorCode.RECEIPT_CORRUPT, "run %s receipt has unsupported schema %r" % (
                run_id, version))
        body = {k: v for k, v in receipt.items() if k != "checksum"}
        if receipt.get("checksum") != digest_of(body):
            raise GraphEngineeringError(ErrorCode.RECEIPT_CORRUPT, "run %s receipt checksum mismatch" % run_id)
        return receipt

    # -- artifacts --------------------------------------------------------------
    def _artifact_path(self, run_id: str, name: str) -> Path:
        if not isinstance(name, str) or not ARTIFACT_NAME_RE.match(name):
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "invalid artifact name %r" % (name,))
        return self.run_dir(run_id) / "artifacts" / (name + ".json")

    def write_artifact(self, run_id: str, name: str, payload: Any) -> dict[str, Any]:
        body = {"kind": ARTIFACT_KIND, "name": name, "payload": json.loads(canonical_json(payload)),
                "created_at": utc_now_precise()}
        digest = digest_of(body)
        path = self._artifact_path(run_id, name)
        try:
            atomic_write_text(path, json.dumps(dict(body, checksum=digest), indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise _write_failed(run_id, "write an artifact", path, exc) from exc
        return {"artifact": name, "digest": digest}

    def read_artifact(self, run_id: str, name: str) -> Any:
        return self.read_artifact_record(run_id, name)["payload"]

    def read_artifact_record(self, run_id: str, name: str) -> dict[str, Any]:
        path = self._artifact_path(run_id, name)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GraphEngineeringError(ErrorCode.INVALID_ARGUMENT, "run %s has no artifact %s" % (run_id, name)) from exc
        except (OSError, ValueError) as exc:
            raise GraphEngineeringError(ErrorCode.STATE_CORRUPT, "artifact %s of run %s is unreadable" % (
                name, run_id)) from exc
        body = {k: v for k, v in record.items() if k != "checksum"} if isinstance(record, dict) else None
        if body is None or record.get("kind") != ARTIFACT_KIND or record.get("checksum") != digest_of(body):
            raise GraphEngineeringError(ErrorCode.STATE_CORRUPT, "artifact %s of run %s failed its checksum" % (
                name, run_id))
        return record

    # -- dispatch lease ---------------------------------------------------------
    def read_lease(self, run_id: str) -> dict[str, Any] | None:
        path = self.run_dir(run_id) / "lease.json"
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"corrupt": True}
        return lease if isinstance(lease, dict) else {"corrupt": True}

    def write_lease(self, run_id: str, lease: Mapping[str, Any]) -> None:
        path = self.run_dir(run_id) / "lease.json"
        try:
            atomic_write_text(path, json.dumps(dict(lease), indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise _write_failed(run_id, "write the dispatch lease", path, exc) from exc

    def delete_lease(self, run_id: str) -> None:
        path = self.run_dir(run_id) / "lease.json"
        try:
            path.unlink()
        except FileNotFoundError:
            return
        fsync_dir(path.parent)


RunStore = LocalStateStore  # 1.x name, kept for compatibility


def migrate_state_v1(state: dict[str, Any]) -> dict[str, Any]:
    """Schema 1 -> 2: add the fields the durable kernel relies on. Pure and deterministic."""
    migrated = dict(state)
    migrated["state_schema_version"] = RUN_STATE_SCHEMA_VERSION
    migrated.setdefault("revision", 0)
    migrated.setdefault("journal", None)  # 1.x did not bind state to the journal
    migrated.setdefault("recovery", [])
    migrated.setdefault("finalization", None)
    migrated.setdefault("policy", None)
    migrated.setdefault("autopilot", None)
    migrated.setdefault("spec_revisions", [])
    migrated.setdefault("evidence", None)
    nodes = {}
    for node_id, node_state in (migrated.get("nodes") or {}).items():
        node_state = dict(node_state)
        node_state.setdefault("evidence", [])
        node_state.setdefault("repair", None)
        nodes[node_id] = node_state
    migrated["nodes"] = nodes
    migrated["_migrated_from"] = 1
    return migrated


def _bytes_digest(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


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
