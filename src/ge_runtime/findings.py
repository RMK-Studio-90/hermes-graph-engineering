"""Findings of audit/review workflows and their deterministic adjudication.

A finding is only a claim until the runtime has checked it::

    UNVERIFIED            no independent verification (the default for every hunter finding)
    CONFIRMED             an independent verifier confirmed it AND quoted the cited source
    PARTIALLY_CONFIRMED   confirmed in part, with valid quotes
    REJECTED              the independent verifier rejected it
    NEEDS_MORE_EVIDENCE   a verdict without valid quotes, or a location that does not resolve

A verifier's quotes are checked against the source lines the *runtime* read from the
workspace (``views.attach_source``), not against anything an agent supplied. No finding is
ever CONFIRMED because an agent says so.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Mapping

MIN_QUOTE_CHARS = 3
_SOURCE_LINE = re.compile(r"^(\d+): ?(.*)$")
PUBLIC_FIELDS = ("id", "title", "claim", "location", "severity", "category")


class FindingStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    REJECTED = "REJECTED"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"


VERDICTS = frozenset({FindingStatus.CONFIRMED.value, FindingStatus.PARTIALLY_CONFIRMED.value,
                      FindingStatus.REJECTED.value, FindingStatus.NEEDS_MORE_EVIDENCE.value})


def collect_findings(sources: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the hunters' finding lists into one list with stable ids (``F001``...)."""
    merged: list[dict[str, Any]] = []
    invalid = 0
    for origin in sorted(sources):
        items = sources[origin]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("claim"), str) \
                    or not isinstance(item.get("location"), str) or not item["claim"].strip():
                invalid += 1
                continue
            finding = {key: item[key] for key in PUBLIC_FIELDS[1:] if isinstance(item.get(key), str)}
            finding["id"] = "F%03d" % (len(merged) + 1)
            finding["origin"] = origin
            finding["status"] = FindingStatus.UNVERIFIED.value
            if isinstance(item.get("reasoning"), str):
                finding["reasoning"] = item["reasoning"]  # kept for the record, never shown to reviewers
            merged.append(finding)
    return {"findings": merged, "invalid": invalid}


def _source_lines(source: Any) -> dict[int, str]:
    lines: dict[int, str] = {}
    if isinstance(source, Mapping) and isinstance(source.get("text"), str):
        for raw in source["text"].splitlines():
            match = _SOURCE_LINE.match(raw)
            if match:
                lines[int(match.group(1))] = match.group(2)
    return lines


def check_quotes(source: Any, quotes: Any) -> tuple[int, int, list[str]]:
    """``(valid, total, problems)`` for a verifier's quotes against runtime-read source lines."""
    lines = _source_lines(source)
    if not isinstance(quotes, list):
        return 0, 0, ["quotes must be a list"]
    valid, problems = 0, []
    for quote in quotes:
        if not isinstance(quote, Mapping):
            problems.append("quote is not an object")
            continue
        text = quote.get("text")
        line = quote.get("line")
        if not isinstance(text, str) or len(text.strip()) < MIN_QUOTE_CHARS:
            problems.append("quote text too short")
            continue
        if isinstance(line, int) and not isinstance(line, bool):
            candidates = [lines.get(line, "")]
        else:
            candidates = list(lines.values())
        if any(text.strip() in candidate for candidate in candidates):
            valid += 1
        else:
            problems.append("quote %r not found at line %s of the cited source" % (text.strip()[:60], line))
    return valid, len(quotes), problems


def adjudicate(findings: Any, verifications: Any, reviews: Any = None) -> dict[str, Any]:
    """Deterministic finding statuses from independent verifications (see module docstring)."""
    by_id = {v.get("id"): v for v in verifications or [] if isinstance(v, Mapping)}
    review_by_id = {r.get("id"): r for r in reviews or [] if isinstance(r, Mapping)}
    ledger: list[dict[str, Any]] = []
    for finding in findings or []:
        if not isinstance(finding, Mapping):
            continue
        entry = {key: finding.get(key) for key in PUBLIC_FIELDS}
        entry["origin"] = finding.get("origin")
        verification = by_id.get(finding.get("id"))
        review = review_by_id.get(finding.get("id"))
        if review is not None:
            entry["review"] = {k: review.get(k) for k in ("assessment", "notes") if k in review}
        source = finding.get("source")
        if verification is None:
            entry.update(status=FindingStatus.UNVERIFIED.value, reason="no independent verification")
        elif source is None:
            entry.update(status=FindingStatus.NEEDS_MORE_EVIDENCE.value,
                         reason="the cited location does not resolve to workspace source")
        else:
            verdict = verification.get("verdict")
            valid, total, problems = check_quotes(source, verification.get("quotes"))
            entry.update(verdict=verdict, quotes_valid=valid, quotes_total=total)
            if verdict not in VERDICTS:
                entry.update(status=FindingStatus.NEEDS_MORE_EVIDENCE.value, reason="unknown verdict %r" % verdict)
            elif verdict in (FindingStatus.CONFIRMED.value, FindingStatus.PARTIALLY_CONFIRMED.value):
                if valid and valid == total:
                    entry.update(status=verdict, reason="verifier verdict backed by %d quote(s) of the cited "
                                                        "source" % valid)
                else:
                    entry.update(status=FindingStatus.NEEDS_MORE_EVIDENCE.value,
                                 reason="verdict %s without valid quotes: %s" % (verdict, "; ".join(problems) or
                                                                                  "no quotes"))
            elif verdict == FindingStatus.REJECTED.value:
                entry.update(status=verdict, reason=str(verification.get("explanation") or "rejected by the "
                                                                                            "independent verifier")[:500])
            else:
                entry.update(status=verdict, reason=str(verification.get("explanation") or "")[:500])
            if isinstance(verification.get("explanation"), str):
                entry["explanation"] = verification["explanation"][:1000]
            if source is not None:
                entry["source_sha256"] = source.get("sha256")
        ledger.append(entry)
    counts = {status.value: sum(1 for e in ledger if e["status"] == status.value) for status in FindingStatus}
    return {"ledger": ledger, "counts": counts}
