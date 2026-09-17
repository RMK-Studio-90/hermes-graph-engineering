"""Default approval provider: request and decision files in a local directory.

``request`` writes ``<request_id>.request.json`` atomically. An operator
approves or denies by writing ``<request_id>.decision.json``::

    {"status": "APPROVED" | "DENIED", "binding_digest": "sha256:...", "decided_by": "..."}

The decision must repeat the digest of the exact binding it approves. Any
missing, malformed, mismatched or unexpected content yields ERROR, never
APPROVED. The random request id doubles as a per-request nonce.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..._fs import atomic_write_text
from ..types import ApprovalBinding, ApprovalDecision, ApprovalStatus

_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


class FileApprovalProvider:
    name = "file"

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)

    def _request_file(self, request_id: str) -> Path:
        return self.directory / ("%s.request.json" % request_id)

    def _decision_file(self, request_id: str) -> Path:
        return self.directory / ("%s.decision.json" % request_id)

    def request(self, binding: ApprovalBinding) -> str:
        request_id = uuid.uuid4().hex
        payload = {
            "request_id": request_id,
            "binding": binding.to_dict(),
            "binding_digest": binding.digest(),
            "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        atomic_write_text(self._request_file(request_id), json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return request_id

    def poll(self, request_id: str) -> ApprovalDecision:
        if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
            return ApprovalDecision(ApprovalStatus.ERROR, str(request_id), detail="invalid request id")
        try:
            request = json.loads(self._request_file(request_id).read_text(encoding="utf-8"))
            binding = ApprovalBinding(**request["binding"])
        except FileNotFoundError:
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, detail="unknown request")
        except (OSError, ValueError, TypeError, KeyError):
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, detail="unreadable request")
        if request.get("binding_digest") != binding.digest():
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, binding, detail="request digest mismatch")

        decision_file = self._decision_file(request_id)
        if not decision_file.exists():
            return ApprovalDecision(ApprovalStatus.PENDING, request_id, binding)
        try:
            decision = json.loads(decision_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, binding, detail="unreadable decision")
        if not isinstance(decision, dict):
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, binding, detail="malformed decision")
        if decision.get("binding_digest") != binding.digest():
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, binding, detail="decision binding mismatch")
        status = decision.get("status")
        if status not in (ApprovalStatus.APPROVED.value, ApprovalStatus.DENIED.value):
            return ApprovalDecision(ApprovalStatus.ERROR, request_id, binding, detail="unexpected decision status")
        decided_by = decision.get("decided_by")
        return ApprovalDecision(
            ApprovalStatus(status),
            request_id,
            binding,
            decided_by=decided_by if isinstance(decided_by, str) else None,
        )
