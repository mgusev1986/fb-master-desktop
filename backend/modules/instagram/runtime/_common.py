"""Instagram runtime — общие helpers."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.modules.instagram.models import InstagramAccount, InstagramAccountDayUsage
from backend.modules.instagram.services import accounts as accounts_svc


def human_pause(min_ms: int = 350, max_ms: int = 1100) -> None:
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def try_locator(page, selectors: tuple[str, ...], timeout_ms: int = 4000):
    deadline = time.monotonic() + timeout_ms / 1000
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            remaining = max(200, int((deadline - time.monotonic()) * 1000))
            if loc.is_visible(timeout=remaining):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def bump_usage(db: Session, account: InstagramAccount, field: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(InstagramAccountDayUsage)
        .filter(InstagramAccountDayUsage.account_id == account.id, InstagramAccountDayUsage.usage_date == today)
        .one_or_none()
    )
    if row is None:
        row = InstagramAccountDayUsage(account_id=account.id, usage_date=today)
        db.add(row)
        db.flush()
    setattr(row, field, (getattr(row, field, 0) or 0) + 1)


def check_cap(db: Session, account: InstagramAccount, field: str, soft_cap: int) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(InstagramAccountDayUsage)
        .filter(InstagramAccountDayUsage.account_id == account.id, InstagramAccountDayUsage.usage_date == today)
        .one_or_none()
    )
    if row is None:
        return True
    used = int(getattr(row, field, 0) or 0)
    return used < soft_cap


def mark_login_required(db: Session, acc: InstagramAccount, reason: str) -> None:
    now = datetime.now(timezone.utc)
    acc.session_ok = False
    acc.last_login_check_at = now
    acc.login_blocked_at = now
    acc.login_blocked_reason = reason[:480]
    acc.status = "needs_attention"
    accounts_svc.log_event(db, acc.organization_id, acc.id, "login_required", "warn", {"reason": reason[:120]})
    db.commit()


def mark_restricted(db: Session, acc: InstagramAccount, reason: str) -> None:
    now = datetime.now(timezone.utc)
    acc.session_ok = False
    acc.last_login_check_at = now
    acc.login_blocked_at = now
    acc.login_blocked_reason = reason[:480]
    acc.status = "restricted"
    accounts_svc.log_event(db, acc.organization_id, acc.id, "account_restricted", "error", {"reason": reason[:120]})
    db.commit()


__all__ = [
    "bump_usage", "check_cap", "human_pause",
    "mark_login_required", "mark_restricted", "try_locator",
]
