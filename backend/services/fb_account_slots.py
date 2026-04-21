"""До MAX_FB_ACTIVE_SLOTS аккаунтов одновременно «в работе»; остальные в ожидании (без браузерных задач)."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.models import FBAccount

logger = logging.getLogger(__name__)

MAX_FB_ACTIVE_SLOTS = 3


def account_in_active_slot(acc: FBAccount | None) -> bool:
    if not acc:
        return False
    s = acc.active_slot
    return s is not None and 1 <= int(s) <= MAX_FB_ACTIVE_SLOTS


def count_filled_slots(db: Session, organization_id: int) -> int:
    return (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == organization_id,
            FBAccount.active_slot.isnot(None),
        )
        .count()
    )


def fb_accounts_for_jobs(db: Session, organization_id: int) -> list[FBAccount]:
    return (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == organization_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label.asc())
        .all()
    )


def fb_account_ids_eligible_for_jobs(
    db: Session, organization_id: int, account_ids: list[int]
) -> bool:
    """Все id из списка принадлежат организации и имеют назначенный слот (в работе)."""
    if not account_ids:
        return True
    uniq = list(dict.fromkeys(account_ids))
    n = (
        db.query(FBAccount.id)
        .filter(
            FBAccount.id.in_(uniq),
            FBAccount.organization_id == organization_id,
            FBAccount.active_slot.isnot(None),
        )
        .count()
    )
    return n == len(uniq)


def assign_first_free_active_slot_if_unslotted(db: Session, acc: FBAccount) -> None:
    """
    Если у аккаунта ещё нет слота — занять минимальный свободный 1…MAX.
    Вызывается после импорта/создания карточки и после успешной проверки сессии.
    Если все слоты заняты — active_slot остаётся None (очередь / «Не в работе»).
    """
    if account_in_active_slot(acc):
        return
    used: set[int] = set()
    for (s,) in (
        db.query(FBAccount.active_slot)
        .filter(
            FBAccount.organization_id == acc.organization_id,
            FBAccount.active_slot.isnot(None),
        )
        .all()
    ):
        if s is not None and 1 <= int(s) <= MAX_FB_ACTIVE_SLOTS:
            used.add(int(s))
    for sn in range(1, MAX_FB_ACTIVE_SLOTS + 1):
        if sn not in used:
            err = assign_active_slot(db, acc, sn)
            if err:
                logger.warning("assign_first_free_active_slot_if_unslotted: %s", err)
            else:
                logger.info(
                    "FB account id=%s auto slot=%s (org=%s)",
                    acc.id,
                    sn,
                    acc.organization_id,
                )
            return


def assign_active_slot(db: Session, acc: FBAccount, slot: int | None) -> str | None:
    """
    Назначить слот 1..3 или снять (None → ожидание).
    Если слот занят другим аккаунтом — тот переводится в ожидание.
    Возвращает текст ошибки или None при успехе.
    """
    if slot is None:
        acc.active_slot = None
        return None
    try:
        sn = int(slot)
    except (TypeError, ValueError):
        return "Некорректный номер слота"
    if sn < 1 or sn > MAX_FB_ACTIVE_SLOTS:
        return f"Слот только 1…{MAX_FB_ACTIVE_SLOTS}"
    other = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == acc.organization_id,
            FBAccount.active_slot == sn,
            FBAccount.id != acc.id,
        )
        .first()
    )
    if other:
        other.active_slot = None
    acc.active_slot = sn
    return None


def slot_label(slot: int | None) -> str:
    if slot is None:
        return "Ожидание"
    return f"Слот {int(slot)}"
