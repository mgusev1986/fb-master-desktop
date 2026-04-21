"""
Видимая причина ожидания outreach-job'а + circuit-breaker.

Проблема: когда rotator возвращает defer-коды (нет активного слота, daily cap,
прокси заблокирован и т.п.) — worker тихо ставит row обратно в queued и спит,
job висит в `running`, UI показывает «Идёт отправка в фоне…» бесконечно.

Решение:
 1) При каждом defer записываем причину в `Job.progress_json.wait_reason_*`,
    фронт читает и показывает понятный баннер.
 2) Если подряд N раз одна и та же причина — завершаем job как failed,
    чтобы пользователь мог исправить настройки и запустить новый.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.models import Job

logger = logging.getLogger(__name__)

# Порог circuit-breaker: сколько подряд defer'ов с одной причиной допустимо.
# Бюджет: 20 × throttle-sleep (≈ 20 × 30 сек = 10 мин) — этого достаточно,
# чтобы временные ошибки (например кратковременный cap) прошли, но бесконечный
# цикл «нет активного слота» умрёт быстро.
DEFAULT_MAX_SAME_REASON_DEFERS = 20

# Коды defer'ов → человеческие объяснения для пользователя.
OUTREACH_WAIT_REASONS: dict[str, str] = {
    "empty_pool": (
        "Пул аккаунтов кампании пуст. Откройте настройки кампании и выберите "
        "хотя бы один аккаунт для отправки."
    ),
    "daily_cap_all_accounts": (
        "Все аккаунты кампании сегодня исчерпали суточный лимит отправки. "
        "Рассылка продолжится автоматически после сброса лимита (00:00 UTC)."
    ),
    "need_active_slot": (
        "Нет аккаунта в активном слоте (1–3). Зайдите в «Аккаунты» и "
        "переведите нужный аккаунт в активный слот."
    ),
    "no_slot_in_pool": (
        "В пуле кампании нет ни одного аккаунта в активном слоте (1–3). "
        "Проверьте «Аккаунты» и слоты."
    ),
    "missing_account": (
        "Аккаунт для отправки не найден в базе. Проверьте настройки кампании."
    ),
    "proxy_tunnel_blocked": (
        "Прокси аккаунта заблокирован (HTTP 407 / срок истёк). Откройте "
        "«Аккаунты» и замените прокси или снимите блокировку."
    ),
    "send_window_closed": (
        "Сейчас вне окна отправки кампании. Рассылка возобновится, когда "
        "окно откроется."
    ),
}


def reason_humanize(reason_code: str) -> str:
    return OUTREACH_WAIT_REASONS.get(reason_code) or (
        f"Рассылка поставлена на паузу: {reason_code}"
    )


def record_outreach_wait(
    db: Session,
    job_id: int | None,
    reason_code: str,
    *,
    reason_details: str = "",
) -> int:
    """
    Записать «почему job сейчас ждёт» в `progress_json`. Возвращает счётчик
    подряд идущих defer'ов с этой же причиной (для circuit-breaker).
    """
    if not job_id or not reason_code:
        return 0
    try:
        job = db.get(Job, int(job_id))
        if not job:
            return 0
        prog = dict(job.progress_json or {}) if isinstance(job.progress_json, dict) else {}
        prev_reason = str(prog.get("wait_reason") or "")
        if prev_reason == reason_code:
            count = int(prog.get("wait_reason_count") or 0) + 1
        else:
            count = 1
            prog["wait_reason_since"] = datetime.now(timezone.utc).isoformat()
        prog["wait_reason"] = reason_code
        prog["wait_reason_ru"] = reason_humanize(reason_code)
        prog["wait_reason_count"] = count
        if reason_details:
            prog["wait_reason_details"] = str(reason_details)[:500]
        else:
            prog.pop("wait_reason_details", None)
        prog["wait_reason_updated_at"] = datetime.now(timezone.utc).isoformat()
        job.progress_json = prog
        db.commit()
        return count
    except Exception:
        logger.exception("record_outreach_wait job=%s reason=%s", job_id, reason_code)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def clear_outreach_wait(db: Session, job_id: int | None) -> None:
    """Сбросить wait_reason в progress_json — job снова «работает нормально»."""
    if not job_id:
        return
    try:
        job = db.get(Job, int(job_id))
        if not job or not isinstance(job.progress_json, dict):
            return
        prog = dict(job.progress_json)
        changed = False
        for key in (
            "wait_reason",
            "wait_reason_ru",
            "wait_reason_since",
            "wait_reason_count",
            "wait_reason_details",
            "wait_reason_updated_at",
        ):
            if key in prog:
                prog.pop(key, None)
                changed = True
        if changed:
            job.progress_json = prog
            db.commit()
    except Exception:
        logger.exception("clear_outreach_wait job=%s", job_id)
        try:
            db.rollback()
        except Exception:
            pass


def should_abort_due_to_wait(
    count: int,
    *,
    max_same: int = DEFAULT_MAX_SAME_REASON_DEFERS,
) -> bool:
    """True — если подряд повторённых defer'ов с этой причиной уже столько,
    что пора завершить job (circuit breaker)."""
    return count >= max(1, int(max_same))
