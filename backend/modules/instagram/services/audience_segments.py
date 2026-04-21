"""Instagram audience segments."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import InstagramAudienceSegment


_VALID_KINDS = ("users", "hashtag", "competitor_followers", "competitor_likers", "competitor_commenters")


def list_segments(db: Session, organization_id: int) -> list[InstagramAudienceSegment]:
    return (
        db.query(InstagramAudienceSegment)
        .filter(
            InstagramAudienceSegment.organization_id == organization_id,
            InstagramAudienceSegment.is_archived.is_(False),
        )
        .order_by(InstagramAudienceSegment.created_at.desc())
        .all()
    )


def get_segment(db: Session, segment_id: int) -> InstagramAudienceSegment | None:
    return db.get(InstagramAudienceSegment, int(segment_id))


def create_segment(db: Session, organization_id: int, **fields: Any) -> InstagramAudienceSegment:
    keywords = (fields.get("keywords") or "").strip()
    if not keywords:
        raise ValueError("keywords обязательны")
    kind = (fields.get("search_kind") or "hashtag").strip().lower()
    if kind not in _VALID_KINDS:
        kind = "hashtag"
    seg = InstagramAudienceSegment(
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
