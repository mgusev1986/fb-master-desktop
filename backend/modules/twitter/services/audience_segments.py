"""Twitter audience segments — saved-фильтры для повторных поисков."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterAudienceSegment


def list_segments(db: Session, organization_id: int) -> list[TwitterAudienceSegment]:
    return (
        db.query(TwitterAudienceSegment)
        .filter(
            TwitterAudienceSegment.organization_id == organization_id,
            TwitterAudienceSegment.is_archived.is_(False),
        )
        .order_by(TwitterAudienceSegment.created_at.desc())
        .all()
    )


def get_segment(db: Session, segment_id: int) -> TwitterAudienceSegment | None:
    return db.get(TwitterAudienceSegment, int(segment_id))


def create_segment(db: Session, organization_id: int, **fields: Any) -> TwitterAudienceSegment:
    keywords = (fields.get("keywords") or "").strip()
    if not keywords:
        raise ValueError("keywords обязательны")
    kind = (fields.get("search_kind") or "users").strip().lower()
    if kind not in ("users", "tweets"):
        kind = "users"
    seg = TwitterAudienceSegment(
        organization_id=organization_id,
        name=(fields.get("name") or keywords)[:255],
        keywords=keywords[:512],
        search_kind=kind,
    )
    db.add(seg)
    db.commit()
    return seg


def archive_segment(db: Session, segment_id: int) -> bool:
    seg = get_segment(db, segment_id)
    if seg is None:
        return False
    seg.is_archived = True
    db.commit()
    return True


def record_run(db: Session, segment_id: int, count: int) -> None:
    seg = get_segment(db, segment_id)
    if seg is None:
        return
    seg.last_run_at = datetime.now(timezone.utc)
    seg.last_run_count = int(count)
    db.commit()


__all__ = ["archive_segment", "create_segment", "get_segment", "list_segments", "record_run"]
