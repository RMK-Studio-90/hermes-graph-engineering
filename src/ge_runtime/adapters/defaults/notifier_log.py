"""Default notifier: Python logging."""
from __future__ import annotations

import logging

from ..types import NotifyCategory, NotifyResult

_LOGGER = logging.getLogger("ge_runtime.notify")
_LEVEL = {
    NotifyCategory.REPORT_IMMEDIATELY: logging.WARNING,
    NotifyCategory.LOG_ONLY: logging.INFO,
    NotifyCategory.NO_USER_QUERY: logging.DEBUG,
}


class LoggingNotifier:
    name = "log"

    def notify(self, category: NotifyCategory, message: str) -> NotifyResult:
        try:
            category = NotifyCategory(category)
            _LOGGER.log(_LEVEL[category], "[%s] %s", category.value, message)
        except Exception as exc:  # delivery failure is reported, never raised
            return NotifyResult(delivered=False, detail=type(exc).__name__)
        return NotifyResult(delivered=True)
