"""Фоновый мониторинг прокси FB-аккаунтов: при обрыве туннеля блокируется вся серверная автоматизация по аккаунту."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.services import proxy_lease

logger = logging.getLogger(__name__)

PROXY_AUTOMATION_BLOCKED_HINT = (
    "Прокси не отвечает — автоматизация по этому аккаунту остановлена. "
    "Проверьте настройки в «Аккаунты»."
)

# Однократно дописывается к label при автоостановке по сроку прокси; снимается при продлении/сбросе срока.
PROXY_LEASE_EXPIRED_LABEL_SUFFIX = " [FBM: срок прокси — смените прокси или дату]"


class ProxyTunnelBlockedError(RuntimeError):
    """Прокси помечен как недоступный — нельзя открывать Playwright-слот для этого fb_account_id."""


def account_uses_proxy(acc: Any) -> bool:
    return bool(acc and acc.proxy_enabled and (acc.proxy_url or "").strip())


def account_proxy_tunnel_blocked(acc: Any) -> bool:
    if not acc or not account_uses_proxy(acc):
        return False
    if bool(getattr(acc, "proxy_tunnel_blocked", False)):
        return True
    return proxy_lease.proxy_lease_expired_for_automation(acc)


def check_proxy_connectivity_sync(acc: Any) -> tuple[bool, str]:
    """Проверка туннеля через исходящий HTTP к сервису гео (как в карточке аккаунта)."""
    from backend.services.proxy_geo_lookup import (
        lookup_geo_through_proxy,
        parsed_proxy_line_from_account_fields,
    )

    if not account_uses_proxy(acc):
        return True, ""
    line = parsed_proxy_line_from_account_fields(
        acc.proxy_url, acc.proxy_username, acc.proxy_password
    )
    if not line:
        return False, "Некорректный адрес прокси"
    data = lookup_geo_through_proxy(line)
    if data.get("ok"):
        return True, ""
    return False, (data.get("error") or "Прокси недоступен").strip()[:500]


def clear_proxy_tunnel_block_on_account(db: Session, acc: Any) -> None:
    acc.proxy_tunnel_blocked = False
    acc.proxy_tunnel_blocked_at = None
    acc.proxy_tunnel_last_error = None


def _int_ids(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _pause_campaigns_using_account(db: Session, account_id: int) -> None:
    from backend.models import OutreachCampaign, SequenceCampaign, WarmupCampaign
    from backend.services.automation_auto_resume import strip_auto_resume_flag_from_config

    def _strip_restart_flag(camp: Any) -> None:
        if isinstance(getattr(camp, "config", None), dict):
            camp.config = strip_auto_resume_flag_from_config(camp.config)

    aid = int(account_id)
    for camp in db.query(OutreachCampaign).filter(OutreachCampaign.status == "running").all():
        if aid in _int_ids(camp.fb_account_ids):
            camp.status = "paused"
            _strip_restart_flag(camp)
    for camp in db.query(WarmupCampaign).filter(WarmupCampaign.status == "running").all():
        if aid in _int_ids(camp.fb_account_ids):
            camp.status = "paused"
            _strip_restart_flag(camp)
    for camp in db.query(SequenceCampaign).filter(SequenceCampaign.status == "active").all():
        ids = _int_ids(camp.fb_account_ids)
        if ids and aid in ids:
            camp.status = "paused"
            _strip_restart_flag(camp)


def pause_all_automation_for_fb_account(db: Session, account_id: int) -> None:
    """Пауза рассылки, прогрева и сценариев по fb_account_id (единый стоп автоматизации на аккаунте)."""
    _pause_campaigns_using_account(db, int(account_id))


def _label_append_proxy_lease_expired_marker(acc: Any) -> bool:
    label = (getattr(acc, "label", None) or "").strip()
    if PROXY_LEASE_EXPIRED_LABEL_SUFFIX in label:
        return False
    base = label or (str(getattr(acc, "fb_login_username", None) or "").strip() or f"Аккаунт {acc.id}")
    suf = PROXY_LEASE_EXPIRED_LABEL_SUFFIX
    max_base = max(1, 255 - len(suf))
    new_label = (base[:max_base] + suf)[:255]
    if new_label == (acc.label or ""):
        return False
    acc.label = new_label
    return True


def _label_remove_proxy_lease_expired_marker(acc: Any) -> bool:
    if not acc.label or PROXY_LEASE_EXPIRED_LABEL_SUFFIX not in acc.label:
        return False
    cleaned = acc.label.replace(PROXY_LEASE_EXPIRED_LABEL_SUFFIX, "").strip()
    if not cleaned:
        cleaned = (str(getattr(acc, "fb_login_username", None) or "").strip() or f"Аккаунт {acc.id}")[:255]
    if cleaned == acc.label:
        return False
    acc.label = cleaned[:255]
    return True


def sync_proxy_lease_slot_and_label_for_account(db: Session, acc: Any) -> bool:
    """
    Если истёк срок прокси (с запасом 1 ч): пауза кампаний, отмена парсера, снятие слота 1–3, пометка в названии.
    Если срок снова в порядке — только снять пометку с названия (слот не назначаем автоматически).
    Возвращает True, если менялись поля аккаунта (label или active_slot).
    """
    from backend.models import FBAccount
    from backend.services.fb_account_slots import account_in_active_slot, assign_active_slot

    if not isinstance(acc, FBAccount):
        return False
    changed = False
    if not account_uses_proxy(acc):
        if _label_remove_proxy_lease_expired_marker(acc):
            changed = True
        return changed
    if proxy_lease.proxy_lease_expired_for_automation(acc):
        _pause_campaigns_using_account(db, acc.id)
        _maybe_cancel_parser_for_account(db, acc.id)
        if account_in_active_slot(acc):
            assign_active_slot(db, acc, None)
            changed = True
            logger.warning(
                "Истёк срок прокси: слот снят (не в работе), fb_account_id=%s",
                acc.id,
            )
        if _label_append_proxy_lease_expired_marker(acc):
            changed = True
    else:
        if _label_remove_proxy_lease_expired_marker(acc):
            changed = True
    return changed


def enforce_proxy_lease_if_expired(db: Session, acc: Any) -> None:
    """Совместимость: пауза, слот, пометка при истечении срока; снятие пометки при продлении."""
    sync_proxy_lease_slot_and_label_for_account(db, acc)


def _maybe_cancel_parser_for_account(db: Session, account_id: int) -> None:
    try:
        from backend.services.parser_worker import request_parser_cancel_for_account
    except ImportError:
        return
    request_parser_cancel_for_account(db, int(account_id))


def suspend_account_proxy_automation(db: Session, acc: Any, reason: str) -> None:
    """Поставить флаг блокировки, при первом срабатывании — пауза кампаний и отмена парсера."""
    from backend.models import FBAccount

    if not isinstance(acc, FBAccount):
        return
    if not account_uses_proxy(acc):
        clear_proxy_tunnel_block_on_account(db, acc)
        return
    now = datetime.now(timezone.utc)
    if not acc.proxy_tunnel_blocked:
        acc.proxy_tunnel_blocked = True
        acc.proxy_tunnel_blocked_at = now
        acc.proxy_tunnel_last_error = (reason or "")[:512]
        logger.warning(
            "Прокси недоступен: fb_account_id=%s (%s): %s",
            acc.id,
            acc.label,
            (reason or "")[:240],
        )
        _pause_campaigns_using_account(db, acc.id)
        _maybe_cancel_parser_for_account(db, acc.id)
    else:
        acc.proxy_tunnel_last_error = (reason or "")[:512]
    acc.proxy_tunnel_last_check_at = now


def apply_successful_proxy_check(db: Session, acc: Any) -> None:
    now = datetime.now(timezone.utc)
    acc.proxy_tunnel_last_check_at = now
    if acc.proxy_tunnel_blocked:
        clear_proxy_tunnel_block_on_account(db, acc)
        logger.info("Прокси восстановлен: fb_account_id=%s", acc.id)


def refresh_accounts_proxy_health_round() -> None:
    from backend.database import SessionLocal
    from backend.models import FBAccount

    db = SessionLocal()
    try:
        rows = (
            db.query(FBAccount)
            .filter(FBAccount.proxy_enabled.is_(True))
            .order_by(FBAccount.id.asc())
            .all()
        )
        for acc in rows:
            if not (acc.proxy_url or "").strip():
                clear_proxy_tunnel_block_on_account(db, acc)
                acc.proxy_tunnel_last_check_at = datetime.now(timezone.utc)
                enforce_proxy_lease_if_expired(db, acc)
                db.commit()
                continue
            ok, err = check_proxy_connectivity_sync(acc)
            db.refresh(acc)
            if ok:
                apply_successful_proxy_check(db, acc)
            else:
                suspend_account_proxy_automation(db, acc, err or "Прокси недоступен")
            enforce_proxy_lease_if_expired(db, acc)
            db.commit()
    except Exception:
        logger.exception("refresh_accounts_proxy_health_round")
        db.rollback()
    finally:
        db.close()


def on_fb_account_proxy_settings_saved(db: Session, account_id: int) -> None:
    """После сохранения карточки: если прокси выключен — снять блок; иначе проверить туннель."""
    from backend.models import FBAccount

    acc = db.get(FBAccount, int(account_id))
    if not acc:
        return
    if not account_uses_proxy(acc):
        clear_proxy_tunnel_block_on_account(db, acc)
        acc.proxy_tunnel_last_check_at = datetime.now(timezone.utc)
        sync_proxy_lease_slot_and_label_for_account(db, acc)
        db.commit()
        return
    ok, err = check_proxy_connectivity_sync(acc)
    if ok:
        apply_successful_proxy_check(db, acc)
    else:
        suspend_account_proxy_automation(db, acc, err or "Проверка не пройдена")
    enforce_proxy_lease_if_expired(db, acc)
    db.commit()


def start_proxy_health_monitor_thread() -> None:
    from backend.config import proxy_health_check_interval_sec

    interval = proxy_health_check_interval_sec()

    def _loop() -> None:
        time.sleep(min(8.0, interval))
        while True:
            try:
                refresh_accounts_proxy_health_round()
            except Exception:
                logger.exception("proxy health monitor tick")
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="proxy-health-monitor").start()
    logger.info("Запущен мониторинг прокси (интервал %.1f с)", interval)


def campaign_fb_account_ids_blocked_message(db: Session, fb_account_ids: list[Any]) -> str | None:
    from backend.models import FBAccount

    for raw in fb_account_ids or []:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        acc = db.get(FBAccount, aid)
        if not acc:
            continue
        if proxy_lease.proxy_lease_expired_for_automation(acc):
            return (
                f'Аккаунт «{acc.label}»: по настройке срока прокси автоматизация остановлена '
                "(запас 1 ч до расчётного окончания у провайдера). Укажите новый срок или прокси в «Аккаунты»."
            )
        if bool(getattr(acc, "proxy_tunnel_blocked", False)):
            err = (getattr(acc, "proxy_tunnel_last_error", None) or "").strip()
            extra = f" ({err})" if err else ""
            return f'Аккаунт «{acc.label}»: прокси недоступен{extra}. Обновите прокси в «Аккаунты».'
    return None
