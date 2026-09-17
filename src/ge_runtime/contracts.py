"""Input/output contracts and declarative success criteria.

Contracts and criteria are pure data: they are validated when a spec is
admitted and evaluated deterministically against node outputs. Nothing here
executes user-provided code.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

VALUE_TYPES = ("string", "number", "integer", "boolean", "object", "array", "any")

MAX_PATTERN_LENGTH = 200
MAX_MATCH_INPUT_CHARS = 100_000

# criterion check -> whether it needs a "value" parameter
CRITERION_CHECKS = {
    "non_empty": False,
    "true": False,
    "false": False,
    "equals": True,
    "not_equals": True,
    "matches": True,
    "min_length": True,
    "max_length": True,
    "min": True,
    "max": True,
    "type": True,
    "in": True,
}


def is_type(value: Any, type_name: str) -> bool:
    if type_name == "any":
        return True
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compile_pattern(pattern: Any) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError("pattern must be a non-empty string of at most %d characters" % MAX_PATTERN_LENGTH)
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError("invalid pattern: %s" % exc) from exc


def criterion_errors(criterion: Any, declared_outputs: Iterable[str], where: str) -> list[str]:
    """Return definition errors for one success criterion."""
    if not isinstance(criterion, Mapping):
        return ["%s: criterion must be a mapping" % where]
    errors: list[str] = []
    unknown = sorted(set(criterion) - {"output", "check", "value", "description"})
    if unknown:
        errors.append("%s: unknown criterion field(s) %s" % (where, ", ".join(unknown)))
    output = criterion.get("output")
    if output not in set(declared_outputs):
        errors.append("%s: criterion references undeclared output %r" % (where, output))
    check = criterion.get("check")
    if check not in CRITERION_CHECKS:
        errors.append("%s: unknown check %r (known: %s)" % (where, check, ", ".join(sorted(CRITERION_CHECKS))))
        return errors
    needs_value = CRITERION_CHECKS[check]
    if needs_value and "value" not in criterion:
        errors.append("%s: check %r requires 'value'" % (where, check))
        return errors
    if not needs_value and "value" in criterion:
        errors.append("%s: check %r takes no 'value'" % (where, check))
    value = criterion.get("value")
    if check == "matches":
        try:
            compile_pattern(value)
        except ValueError as exc:
            errors.append("%s: %s" % (where, exc))
    elif check in ("min_length", "max_length") and not (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ):
        errors.append("%s: %s value must be a non-negative integer" % (where, check))
    elif check in ("min", "max") and not _is_number(value):
        errors.append("%s: %s value must be a number" % (where, check))
    elif check == "type" and value not in VALUE_TYPES:
        errors.append("%s: type value must be one of %s" % (where, ", ".join(VALUE_TYPES)))
    elif check == "in" and not isinstance(value, list):
        errors.append("%s: in value must be a list" % where)
    return errors


def evaluate_criterion(criterion: Mapping[str, Any], outputs: Mapping[str, Any]) -> tuple[bool, str]:
    """Evaluate one (already validated) criterion. Never raises; any doubt fails."""
    name = criterion.get("output")
    check = criterion.get("check")
    if name not in outputs:
        return False, "output %r missing" % name
    actual = outputs[name]
    expected = criterion.get("value")
    try:
        if check == "non_empty":
            if isinstance(actual, str):
                ok = bool(actual.strip())
            else:
                ok = actual is not None and not (isinstance(actual, (list, dict)) and len(actual) == 0)
        elif check == "true":
            ok = actual is True
        elif check == "false":
            ok = actual is False
        elif check == "equals":
            ok = type(actual) is type(expected) and actual == expected
        elif check == "not_equals":
            ok = not (type(actual) is type(expected) and actual == expected)
        elif check == "matches":
            ok = (
                isinstance(actual, str)
                and len(actual) <= MAX_MATCH_INPUT_CHARS
                and compile_pattern(expected).fullmatch(actual) is not None
            )
        elif check == "min_length":
            ok = isinstance(actual, (str, list, dict)) and len(actual) >= expected
        elif check == "max_length":
            ok = isinstance(actual, (str, list, dict)) and len(actual) <= expected
        elif check == "min":
            ok = _is_number(actual) and actual >= expected
        elif check == "max":
            ok = _is_number(actual) and actual <= expected
        elif check == "type":
            ok = is_type(actual, expected)
        elif check == "in":
            ok = any(type(actual) is type(item) and actual == item for item in expected)
        else:
            return False, "unknown check %r" % check
    except Exception as exc:  # evaluation fails closed, never crashes the run
        return False, "check %s raised %s" % (check, type(exc).__name__)
    return (True, "%s %s passed" % (name, check)) if ok else (False, "%s %s failed" % (name, check))


def evaluate_criteria(criteria: Iterable[Mapping[str, Any]], outputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    results = []
    for index, criterion in enumerate(criteria):
        passed, detail = evaluate_criterion(criterion, outputs)
        results.append({
            "index": index,
            "output": criterion.get("output"),
            "check": criterion.get("check"),
            "passed": passed,
            "detail": detail,
        })
    return results


def output_contract_errors(declared: Mapping[str, Mapping[str, Any]], outputs: Any) -> list[str]:
    """Validate produced outputs against the declared output contract (strict)."""
    if not isinstance(outputs, Mapping):
        return ["outputs must be an object"]
    errors = ["undeclared output %r" % name for name in sorted(set(outputs) - set(declared))]
    for name, spec in declared.items():
        if name not in outputs:
            if spec.get("required", True):
                errors.append("required output %r missing" % name)
            continue
        type_name = spec.get("type", "any")
        if not is_type(outputs[name], type_name):
            errors.append("output %r is not of type %s" % (name, type_name))
    return errors
