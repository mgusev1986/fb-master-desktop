"""Параллельные фоновые задачи автоматизации: лимит совпадает с PLAYWRIGHT_MAX_CONCURRENT.

Слоты FB-аккаунтов (1–3) задают, какие профили могут работать; этот модуль ограничивает,
сколько воркеров одного типа (или суммарно по типам — через семафор в playwright_run_slot)
может выполняться одновременно на одной машине.
"""

from __future__ import annotations

import threading

from backend.config import PLAYWRIGHT_MAX_CONCURRENT

_CAP = max(1, int(PLAYWRIGHT_MAX_CONCURRENT))


class AutomationWorkerSlotBook:
    """Учёт активных job_id для типа воркера (рассылка, парсер, прогрев, сценарий, …)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_ids: set[int] = set()

    @property
    def cap(self) -> int:
        return _CAP

    def try_begin(self, job_id: int) -> str | None:
        """None — слот выдан; иначе «duplicate» или «capacity»."""
        with self._lock:
            if job_id in self._job_ids:
                return "duplicate"
            if len(self._job_ids) >= _CAP:
                return "capacity"
            self._job_ids.add(job_id)
            return None

    def end(self, job_id: int) -> None:
        with self._lock:
            self._job_ids.discard(job_id)

    def running_count(self) -> int:
        with self._lock:
            return len(self._job_ids)

    def running_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._job_ids)

    def has_any(self) -> bool:
        return self.running_count() > 0

    def at_capacity(self) -> bool:
        with self._lock:
            return len(self._job_ids) >= _CAP


outreach_worker_slots = AutomationWorkerSlotBook()
parser_worker_slots = AutomationWorkerSlotBook()
sequence_worker_slots = AutomationWorkerSlotBook()
warmup_worker_slots = AutomationWorkerSlotBook()
people_language_worker_slots = AutomationWorkerSlotBook()
account_branding_worker_slots = AutomationWorkerSlotBook()
