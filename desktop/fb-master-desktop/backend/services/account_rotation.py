"""Ротация FB-аккаунтов по суточному лимиту и слотам (опционально для рассылки / прогрева / сценариев)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from backend.models import FBAccount, FBAccountDayUsage, OutreachCampaign, SequenceCampaign, WarmupCampaign
from backend.services.fb_account_slots import account_in_active_slot, assign_active_slot

logger = logging.getLogger(__name__)

Kind = Literal["outreach", "warmup", "sequence"]


def _utc_date_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _cfg_dict(cfg) -> dict:
    return cfg if isinstance(cfg, dict) else {}


def rotation_enabled_outreach(camp: OutreachCampaign) -> bool:
    return bool(_cfg_dict(camp.config).get("account_rotation"))


def auto_slot_rotation_outreach(camp: OutreachCampaign) -> bool:
    return bool(_cfg_dict(camp.config).get("auto_slot_rotation"))


def rotation_enabled_warmup(camp: WarmupCampaign) -> bool:
    return bool(_cfg_dict(camp.config).get("account_rotation"))


def auto_slot_rotation_warmup(camp: WarmupCampaign) -> bool:
    return bool(_cfg_dict(camp.config).get("auto_slot_rotation"))


def rotation_enabled_sequence(camp: SequenceCampaign) -> bool:
    return bool(_cfg_dict(camp.config).get("account_rotation"))


def auto_slot_rotation_sequence(camp: SequenceCampaign) -> bool:
    return bool(_cfg_dict(camp.config).get("auto_slot_rotation"))


def _daily_cap_outreach(camp: OutreachCampaign) -> int:
    try:
        return max(1, min(int(camp.messages_per_account or 20), 500))
    except (TypeError, ValueError):
        return 20


def _daily_cap_warmup(camp: WarmupCampaign) -> int:
    try:
        return max(1, min(int(camp.actions_per_account or 10), 500))
    except (TypeError, ValueError):
        return 10


def _daily_cap_sequence(camp: SequenceCampaign) -> int:
    cfg = _cfg_dict(camp.config)
    try:
        v = int(cfg.get("sequence_actions_per_account") or cfg.get("rotation_daily_cap") or 50)
    except (TypeError, ValueError):
        v = 50
    return max(1, min(v, 500))


def _get_usage_row(db: Session, fb_account_id: int, usage_date: str) -> FBAccountDayUsage | None:
    return (
        db.query(FBAccountDayUsage)
        .filter(
            FBAccountDayUsage.fb_account_id == fb_account_id,
            FBAccountDayUsage.usage_date == usage_date,
        )
        .first()
    )


def _usage_count(db: Session, fb_account_id: int, usage_date: str, kind: Kind) -> int:
    row = _get_usage_row(db, fb_account_id, usage_date)
    if not row:
        return 0
    if kind == "outreach":
        return int(row.outreach_actions or 0)
    if kind == "warmup":
        return int(row.warmup_actions or 0)
    return int(row.sequence_actions or 0)


def increment_usage(db: Session, fb_account_id: int, kind: Kind) -> None:
    """Увеличить счётчик успешного действия за текущий UTC-день (commit снаружи)."""
    d = _utc_date_str()
    row = _get_usage_row(db, fb_account_id, d)
    if not row:
        row = FBAccountDayUsage(
            fb_account_id=fb_account_id,
            usage_date=d,
            outreach_actions=0,
            warmup_actions=0,
            sequence_actions=0,
        )
        db.add(row)
        db.flush()
    if kind == "outreach":
        row.outreach_actions = int(row.outreach_actions or 0) + 1
    elif kind == "warmup":
        row.warmup_actions = int(row.warmup_actions or 0) + 1
    else:
        row.sequence_actions = int(row.sequence_actions or 0) + 1


def _pool_ids(camp_fb_ids) -> list[int]:
    if not camp_fb_ids:
        return []
    out: list[int] = []
    for x in camp_fb_ids:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _pick_victim_from_pool(
    db: Session,
    pool: list[int],
    usage_date: str,
    cap: int,
    kind: Kind,
) -> int | None:
    """Аккаунт из пула со слотом: сначала с usage >= cap, иначе с максимальным usage."""
    victims: list[tuple[int, int]] = []
    for aid in pool:
        acc = db.get(FBAccount, aid)
        if not acc or not account_in_active_slot(acc):
            continue
        u = _usage_count(db, aid, usage_date, kind)
        victims.append((aid, u))
    if not victims:
        return None
    over = [(a, u) for a, u in victims if u >= cap]
    if over:
        return max(over, key=lambda t: (t[1], t[0]))[0]
    return max(victims, key=lambda t: (t[1], t[0]))[0]


def _resolve_account_for_row(
    db: Session,
    *,
    organization_id: int,
    pool: list[int],
    current_fb_account_id: int | None,
    cap: int,
    kind: Kind,
    auto_slot: bool,
) -> tuple[FBAccount | None, str | None]:
    """
    Выбрать аккаунт для выполнения шага. При ротации может сменить слоты (auto_slot).
    Пока текущий аккаунт строки в пуле, в слоте и не исчерпал суточный лимит — оставляем его.
    """
    usage_date = _utc_date_str()
    if not pool:
        return None, "empty_pool"

    def usage(aid: int) -> int:
        return _usage_count(db, aid, usage_date, kind)

    candidates = [aid for aid in pool if usage(aid) < cap]
    if not candidates:
        return None, "daily_cap_all_accounts"

    def acc_get(aid: int) -> FBAccount | None:
        a = db.get(FBAccount, aid)
        if not a or a.organization_id != organization_id:
            return None
        return a

    cur_id = current_fb_account_id
    if cur_id is not None and cur_id in pool:
        cur_acc = acc_get(cur_id)
        if cur_acc and usage(cur_id) < cap and account_in_active_slot(cur_acc):
            return cur_acc, None

    slotted = [c for c in candidates if account_in_active_slot(acc_get(c))]
    if slotted:
        pick_id = min(slotted, key=lambda x: (usage(x), x))
    else:
        if not auto_slot:
            return None, "need_active_slot"
        noslot = [c for c in candidates if acc_get(c) and not account_in_active_slot(acc_get(c))]
        if not noslot:
            return None, "need_active_slot"
        winner_id = min(noslot, key=lambda x: (usage(x), x))
        victim_id = _pick_victim_from_pool(db, pool, usage_date, cap, kind)
        if victim_id is None:
            return None, "no_slot_in_pool"
        winner = acc_get(winner_id)
        victim = acc_get(victim_id)
        if not winner or not victim:
            return None, "missing_account"
        sn = victim.active_slot
        if sn is None:
            return None, "no_slot_in_pool"
        err = assign_active_slot(db, victim, None)
        if err:
            return None, err
        err = assign_active_slot(db, winner, int(sn))
        if err:
            assign_active_slot(db, victim, int(sn))
            return None, err
        logger.info(
            "account_rotation: swapped slots org=%s kind=%s winner=%s victim=%s slot=%s",
            organization_id,
            kind,
            winner_id,
            victim_id,
            sn,
        )
        pick_id = winner_id

    account = acc_get(pick_id)
    if not account:
        return None, "missing_account"
    return account, None


def resolve_outreach_executor(
    db: Session,
    camp: OutreachCampaign,
    row_fb_account_id: int,
) -> tuple[FBAccount | None, str | None, int | None]:
    """
    Аккаунт для строки рассылки. Возвращает (account, error, new_fb_id если сменили с row).
    Без ротации — как раньше: только текущий id и проверка слота.
    """
    pool = _pool_ids(camp.fb_account_ids)
    if not rotation_enabled_outreach(camp):
        acc = db.get(FBAccount, row_fb_account_id)
        if not acc or acc.organization_id != camp.organization_id:
            return None, "missing_account", None
        if not account_in_active_slot(acc):
            return None, "no_active_slot", None
        return acc, None, None

    cap = _daily_cap_outreach(camp)
    auto_slot = auto_slot_rotation_outreach(camp)
    acc, err = _resolve_account_for_row(
        db,
        organization_id=camp.organization_id,
        pool=pool,
        current_fb_account_id=row_fb_account_id,
        cap=cap,
        kind="outreach",
        auto_slot=auto_slot,
    )
    new_id = acc.id if acc and acc.id != row_fb_account_id else None
    return acc, err, new_id


def resolve_warmup_executor(
    db: Session,
    camp: WarmupCampaign,
    row_fb_account_id: int,
) -> tuple[FBAccount | None, str | None, int | None]:
    pool = _pool_ids(camp.fb_account_ids)
    if not rotation_enabled_warmup(camp):
        acc = db.get(FBAccount, row_fb_account_id)
        if not acc or acc.organization_id != camp.organization_id:
            return None, "missing_account", None
        if not account_in_active_slot(acc):
            return None, "no_active_slot", None
        return acc, None, None

    cap = _daily_cap_warmup(camp)
    auto_slot = auto_slot_rotation_warmup(camp)
    acc, err = _resolve_account_for_row(
        db,
        organization_id=camp.organization_id,
        pool=pool,
        current_fb_account_id=row_fb_account_id,
        cap=cap,
        kind="warmup",
        auto_slot=auto_slot,
    )
    new_id = acc.id if acc and acc.id != row_fb_account_id else None
    return acc, err, new_id


def resolve_sequence_executor(
    db: Session,
    camp: SequenceCampaign,
    enrollment_fb_account_id: int | None,
    enrollment_id: int,
) -> tuple[FBAccount | None, str | None, int | None]:
    pool = _pool_ids(camp.fb_account_ids)
    if not pool:
        return None, "empty_pool", None

    if enrollment_fb_account_id is None:
        cur = pool[enrollment_id % len(pool)]
    else:
        cur = enrollment_fb_account_id

    if not rotation_enabled_sequence(camp):
        acc = db.get(FBAccount, cur)
        if not acc or acc.organization_id != camp.organization_id:
            return None, "missing_account", None
        if not account_in_active_slot(acc):
            return None, "no_active_slot", None
        return acc, None, None

    cap = _daily_cap_sequence(camp)
    auto_slot = auto_slot_rotation_sequence(camp)
    acc, err = _resolve_account_for_row(
        db,
        organization_id=camp.organization_id,
        pool=pool,
        current_fb_account_id=cur,
        cap=cap,
        kind="sequence",
        auto_slot=auto_slot,
    )
    new_id = acc.id if acc and acc.id != cur else None
    return acc, err, new_id
