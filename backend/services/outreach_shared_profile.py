"""
Общий дисковый профиль Chromium для рассылки и встроенного Messenger (Desktop).

Пока идёт outreach job, fb_accounts.outreach_shared_profile_lock_job_id не NULL —
раздел Messenger для этого аккаунта не открывает webview на тот же каталог (mutex).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.models import FBAccount, Job

logger = logging.getLogger(__name__)

JOB_TYPE_OUTREACH = "outreach"


def outreach_shared_profile_feature_enabled() -> bool:
    """Включается в bundle Desktop (local-backend-launcher: FB_MASTER_OUTREACH_SHARED_PROFILE=1)."""
    return os.environ.get("FB_MASTER_OUTREACH_SHARED_PROFILE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _storage_state_has_cookies(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies")
    return isinstance(cookies, list) and len(cookies) > 0


def outreach_shared_disk_profile_eligible(
    db: Session,
    account: FBAccount,
    storage_state: dict[str, Any] | None,
) -> bool:
    """
    Те же условия, что у встроенного Messenger на диске: есть cookies в снимке,
    SOCKS с авторизацией во webview не поддерживается — тогда общий профиль не используем.
    """
    if not outreach_shared_profile_feature_enabled():
        return False
    if not _storage_state_has_cookies(storage_state):
        return False
    from backend.services.electron_proxy import electron_partition_proxy_for_webview

    proxy = electron_partition_proxy_for_webview(account)
    if proxy and proxy.get("socks_with_auth"):
        return False
    return True


def clear_stale_outreach_shared_profile_locks(db: Session) -> int:
    """
    Снять блокировки, если задача уже не в running/queued (падение процесса, перезапуск).
    """
    rows = (
        db.query(FBAccount)
        .filter(FBAccount.outreach_shared_profile_lock_job_id.isnot(None))
        .all()
    )
    cleared = 0
    for acc in rows:
        jid = acc.outreach_shared_profile_lock_job_id
        if not jid:
            continue
        job = db.get(Job, int(jid))
        ok = (
            job is not None
            and job.job_type == JOB_TYPE_OUTREACH
            and job.status in ("running", "queued")
        )
        if not ok:
            acc.outreach_shared_profile_lock_job_id = None
            acc.outreach_shared_profile_locked_at = None
            cleared += 1
    if cleared:
        db.commit()
    return cleared


def try_acquire_outreach_shared_profile_lock(
    db: Session, *, account_id: int, job_id: int
) -> bool:
    clear_stale_outreach_shared_profile_locks(db)
    res = db.execute(
        update(FBAccount)
        .where(
            FBAccount.id == int(account_id),
            FBAccount.outreach_shared_profile_lock_job_id.is_(None),
        )
        .values(
            outreach_shared_profile_lock_job_id=int(job_id),
            outreach_shared_profile_locked_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    ok = getattr(res, "rowcount", 0) == 1
    if ok:
        logger.info(
            "outreach shared profile lock: account_id=%s job_id=%s",
            account_id,
            job_id,
        )
    return ok


def release_outreach_shared_profile_lock(
    db: Session, *, account_id: int, job_id: int
) -> None:
    acc = db.get(FBAccount, int(account_id))
    if not acc:
        return
    if acc.outreach_shared_profile_lock_job_id != int(job_id):
        return
    acc.outreach_shared_profile_lock_job_id = None
    acc.outreach_shared_profile_locked_at = None
    db.commit()
    logger.info(
        "outreach shared profile unlock: account_id=%s job_id=%s",
        account_id,
        job_id,
    )


def messenger_blocked_by_outreach(db: Session, fb_account_id: int | None) -> bool:
    if not fb_account_id or not outreach_shared_profile_feature_enabled():
        return False
    clear_stale_outreach_shared_profile_locks(db)
    acc = db.get(FBAccount, int(fb_account_id))
    if not acc or not acc.outreach_shared_profile_lock_job_id:
        return False
    return True


def active_fb_account_messenger_blocked(request, db: Session, org_id: int) -> bool:
    from backend.services.active_fb_account import get_active_fb_account_id

    aid = get_active_fb_account_id(request, db, org_id)
    return messenger_blocked_by_outreach(db, aid)
