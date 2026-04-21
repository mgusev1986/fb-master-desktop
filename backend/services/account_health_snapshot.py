"""Сводка «центр здоровья» для карточки FB-аккаунта (UI + простой trust-score).

Полноценный мониторинг прокси и авто-пауза кампаний — в proxy_health_guard + поток в app_factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.models import FBAccount, FBAccountDayUsage, JobEvent
from backend.services import proxy_lease
from backend.services.fb_session_state import session_state_is_usable, session_state_from_account


def _utc_today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _job_events_warn_error_24h(db: Session, fb_account_id: int) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    n = (
        db.query(JobEvent)
        .filter(
            JobEvent.fb_account_id == int(fb_account_id),
            JobEvent.ts >= since,
            JobEvent.severity.in_(("error", "warn")),
        )
        .count()
    )
    return int(n or 0)


def _readiness_label(status: str | None) -> str | None:
    s = (status or "").strip().lower()
    if s == "warming":
        return "Прогрев (warming)"
    if s == "ready":
        return "Готов к нагрузке"
    if s == "cold":
        return "Без прогрева (cold)"
    return None


@dataclass(frozen=True)
class AccountHealthSnapshot:
    trust_score: int
    risk_level: str  # low | medium | high
    proxy_monitor_summary: str
    proxy_detail: str
    session_quality: str
    daily_outreach: int
    daily_warmup: int
    daily_sequence: int
    warming_label: str | None
    job_errors_24h: int
    recommendations: tuple[str, ...]
    messenger_status_label: str
    messenger_status_tone: str
    messenger_status_hint: str
    automation_status_label: str
    automation_status_tone: str
    automation_status_hint: str
    login_entry_status_label: str
    login_entry_status_tone: str
    login_entry_status_hint: str


def build_account_health_snapshot(
    db: Session,
    acc: FBAccount,
    *,
    active_messenger_account_id: int | None = None,
) -> AccountHealthSnapshot:
    """Оценка 0–100: выше = спокойнее состояние (надёжность)."""
    aid = int(acc.id)
    day = _utc_today_key()
    row = (
        db.query(FBAccountDayUsage)
        .filter(FBAccountDayUsage.fb_account_id == aid, FBAccountDayUsage.usage_date == day)
        .one_or_none()
    )
    outreach = int(row.outreach_actions or 0) if row else 0
    warmup = int(row.warmup_actions or 0) if row else 0
    sequence = int(row.sequence_actions or 0) if row else 0
    err_n = _job_events_warn_error_24h(db, aid)
    snap = session_state_from_account(acc)
    session_usable, session_reason = session_state_is_usable(
        snap,
        expected_login=(acc.fb_login_username or "").strip() or None,
    )

    score = 52
    rec: list[str] = []

    if acc.session_ok is True:
        score += 22
    elif acc.session_ok is False:
        score -= 24
        rec.append("Нужна переавторизация: выполните вход или «Проверить сессию».")
    else:
        score += 4
        rec.append("Сессию ещё не подтверждали — проверьте вход перед массовыми действиями.")

    if not acc.proxy_enabled or not (acc.proxy_url or "").strip():
        score += 14
        proxy_sum = "Прокси выключен — туннель не мониторится."
        proxy_det = "Для стабильности Facebook рекомендуется отдельный резидентский прокси под ГЕО аккаунта."
    else:
        lease_expired = proxy_lease.proxy_lease_expired_for_automation(acc)
        tunnel_blocked = bool(acc.proxy_tunnel_blocked)
        if lease_expired:
            score -= 40
            proxy_sum = "Прокси: срок аренды (с запасом 1 ч) истёк"
            proxy_det = (
                "Автоматизация по аккаунту остановлена до обновления срока или прокси "
                "в блоке «Прокси и название»."
            )
            rec.append(
                "Истёк расчётный срок прокси с запасом 1 час до конца у провайдера — "
                "укажите новую дату окончания в настройках прокси или смените прокси."
            )
            rec.append(
                "Слот снят автоматически (аккаунт в ожидании); в названии добавлена пометка — "
                "после обновления прокси или даты снова назначьте слот 1–3 при необходимости."
            )
        elif tunnel_blocked:
            score -= 38
            err = (acc.proxy_tunnel_last_error or "").strip()
            proxy_sum = "Прокси: автоматизация остановлена защитой"
            proxy_det = (
                (err[:220] + ("…" if len(err) > 220 else ""))
                if err
                else "Туннель не прошёл проверку. Рассылки и сценарии по этому аккаунту на паузе."
            )
            rec.append(
                "Система защиты: прокси не отвечает — рассылки, прогревы и агент на паузе. "
                "Исправьте прокси ниже («Прокси и название») и сохраните."
            )
        else:
            score += 12
            proxy_sum = "Прокси: мониторинг активен, туннель в порядке"
            proxy_det = "Фоновые проверки идут на сервере; при обрыве кампании ставятся на паузу автоматически."
            if proxy_lease.proxy_lease_configured(acc):
                tier = proxy_lease.proxy_lease_ui_tier(acc)
                proxy_sum = "Прокси: мониторинг и срок аренды"
                rem = proxy_lease.proxy_lease_remaining_td(acc)
                line = (
                    proxy_lease.format_proxy_lease_remaining_ru(rem, expired=False)
                    if rem is not None and rem.total_seconds() > 0
                    else "—"
                )
                mdead = proxy_lease.format_proxy_lease_deadline_madrid(acc)
                proxy_det = (
                    f"До окончания у провайдера (по указанной дате/времени): ~{line}. "
                    f"Окончание: {mdead} Europe/Madrid (сверьте с кабинетом продавца). "
                    "Автоостановка автоматизации в FB Master — за 1 ч до этого момента."
                )
                if tier == "warn_orange":
                    rec.append(
                        f"Срок прокси: до окончания осталось ~{line} (порог предупреждения 3–7 суток) — запланируйте продление."
                    )
                elif tier == "alert_red":
                    rec.append(
                        f"Срок прокси: осталось ~{line} (меньше 3 суток до автоостановки) — срочно продлите или смените прокси."
                    )
                elif tier == "critical_blink":
                    rec.append(f"До автоостановки по прокси осталось ~{line} (меньше суток).")

    if (acc.readiness_status or "").strip().lower() == "warming":
        rec.append("Идёт прогрев — снизьте темп комментингов и массовых действий до завершения.")

    if outreach >= 80:
        score -= 10
        rec.append("Суточная нагрузка по рассылке высокая (UTC) — имеет смысл снизить темп.")
    elif outreach >= 40:
        score -= 4

    if err_n >= 12:
        score -= 14
        rec.append("Много предупреждений/ошибок в задачах за 24 ч — сделайте паузу и проверьте сессию и прокси.")
    elif err_n >= 5:
        score -= 7

    score = max(0, min(100, int(score)))

    if score >= 72:
        risk = "low"
    elif score >= 44:
        risk = "medium"
    else:
        risk = "high"

    sess_q = (
        "Сессия подтверждена"
        if acc.session_ok is True
        else ("Сессия не ок" if acc.session_ok is False else "Сессия не проверялась")
    )

    if active_messenger_account_id == aid:
        if session_usable:
            messenger_status_label = "Messenger: живой"
            messenger_status_tone = "success"
            messenger_status_hint = (
                "Этот аккаунт сейчас выбран для Мессенджера и у него есть рабочая Facebook-сессия."
            )
        else:
            messenger_status_label = "Messenger: выбран, но вход спорный"
            messenger_status_tone = "warning"
            messenger_status_hint = (
                "Профиль выбран для Мессенджера, но серверный снимок сессии выглядит неполным. "
                "Если чат всё ещё открыт, нажмите «Проверить синхронизацию» или «Восстановить вход» в Мессенджере."
            )
    else:
        messenger_status_label = "Messenger: не выбран"
        messenger_status_tone = "default"
        messenger_status_hint = "Этот аккаунт сейчас не активен во встроенном Мессенджере."

    if session_usable:
        automation_status_label = "Автоматизация: живая"
        automation_status_tone = "success"
        automation_status_hint = (
            "Рассылка, парсер, прогрев и режим агента могут использовать рабочий снимок Facebook-сессии."
        )
    else:
        automation_status_label = "Автоматизация: нет входа"
        automation_status_tone = "error" if acc.session_ok is False else "warning"
        automation_status_hint = (
            session_reason
            or "Рабочий снимок Facebook-сессии не подтверждён. Нужен новый вход или восстановление сессии."
        )

    if acc.proxy_enabled and (acc.proxy_url or "").strip():
        if proxy_lease.proxy_lease_expired_for_automation(acc):
            automation_status_label = "Автоматизация: пауза (срок прокси)"
            automation_status_tone = "error"
            automation_status_hint = (
                "Истёк расчётный срок прокси с запасом 1 ч до конца у провайдера — рассылки и сценарии на паузе."
            )
        elif acc.proxy_tunnel_blocked:
            automation_status_label = "Автоматизация: пауза (туннель прокси)"
            automation_status_tone = "error"
            automation_status_hint = (
                (acc.proxy_tunnel_last_error or "").strip()[:300]
                or "Прокси не проходит проверку — автоматизация остановлена до исправления."
            )

    if acc.login_blocked_at:
        login_entry_status_label = "Вход Meta: ограничен"
        login_entry_status_tone = "warning" if session_usable else "error"
        login_entry_status_hint = (
            (acc.login_blocked_reason or "").strip()
            or "Meta недавно ограничила обычный путь входа для этого аккаунта."
        )
    elif acc.last_login_check_at or session_usable:
        login_entry_status_label = "Вход Meta: без блокировки"
        login_entry_status_tone = "success" if session_usable else "default"
        login_entry_status_hint = (
            "Последняя проверка не показала временную блокировку обычного login-flow со стороны Meta."
        )
    else:
        login_entry_status_label = "Вход Meta: не проверяли"
        login_entry_status_tone = "default"
        login_entry_status_hint = "Обычный путь входа для этого аккаунта ещё не проверяли."

    return AccountHealthSnapshot(
        trust_score=score,
        risk_level=risk,
        proxy_monitor_summary=proxy_sum,
        proxy_detail=proxy_det,
        session_quality=sess_q,
        daily_outreach=outreach,
        daily_warmup=warmup,
        daily_sequence=sequence,
        warming_label=_readiness_label(acc.readiness_status),
        job_errors_24h=err_n,
        recommendations=tuple(dict.fromkeys([x for x in rec if (x or "").strip()])),
        messenger_status_label=messenger_status_label,
        messenger_status_tone=messenger_status_tone,
        messenger_status_hint=messenger_status_hint,
        automation_status_label=automation_status_label,
        automation_status_tone=automation_status_tone,
        automation_status_hint=automation_status_hint,
        login_entry_status_label=login_entry_status_label,
        login_entry_status_tone=login_entry_status_tone,
        login_entry_status_hint=login_entry_status_hint,
    )


def _proxy_lease_line_for_map(acc: FBAccount) -> str:
    if not proxy_lease.proxy_lease_configured(acc):
        return ""
    if proxy_lease.proxy_lease_expired_for_automation(acc):
        return proxy_lease.format_proxy_lease_remaining_ru(None, expired=True)
    rem = proxy_lease.proxy_lease_remaining_td(acc)
    return proxy_lease.format_proxy_lease_remaining_ru(rem, expired=False)


def build_account_health_map(
    db: Session,
    accounts: list[FBAccount],
    *,
    active_messenger_account_id: int | None = None,
) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for acc in accounts:
        snap = build_account_health_snapshot(
            db,
            acc,
            active_messenger_account_id=active_messenger_account_id,
        )
        out[int(acc.id)] = {
            "trust_score": snap.trust_score,
            "risk_level": snap.risk_level,
            "proxy_monitor_summary": snap.proxy_monitor_summary,
            "proxy_detail": snap.proxy_detail,
            "session_quality": snap.session_quality,
            "daily_outreach": snap.daily_outreach,
            "daily_warmup": snap.daily_warmup,
            "daily_sequence": snap.daily_sequence,
            "warming_label": snap.warming_label,
            "job_errors_24h": snap.job_errors_24h,
            "recommendations": list(snap.recommendations),
            "proxy_last_check_at": acc.proxy_tunnel_last_check_at,
            "proxy_blocked": bool(acc.proxy_tunnel_blocked),
            "proxy_lease_tier": proxy_lease.proxy_lease_ui_tier(acc),
            "proxy_lease_line": _proxy_lease_line_for_map(acc),
            "proxy_lease_configured": proxy_lease.proxy_lease_configured(acc),
            "proxy_lease_expired": proxy_lease.proxy_lease_expired_for_automation(acc),
            "messenger_status_label": snap.messenger_status_label,
            "messenger_status_tone": snap.messenger_status_tone,
            "messenger_status_hint": snap.messenger_status_hint,
            "automation_status_label": snap.automation_status_label,
            "automation_status_tone": snap.automation_status_tone,
            "automation_status_hint": snap.automation_status_hint,
            "login_entry_status_label": snap.login_entry_status_label,
            "login_entry_status_tone": snap.login_entry_status_tone,
            "login_entry_status_hint": snap.login_entry_status_hint,
            "login_blocked_at": acc.login_blocked_at,
            "login_blocked_reason": acc.login_blocked_reason,
        }
    return out
