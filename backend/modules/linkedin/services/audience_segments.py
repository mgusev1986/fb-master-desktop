"""LinkedIn audience segments — saved-фильтры для повторных поисков."""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import LinkedInAudienceSegment


def list_segments(db: Session, organization_id: int) -> list[LinkedInAudienceSegment]:
    return (
        db.query(LinkedInAudienceSegment)
        .filter(
            LinkedInAudienceSegment.organization_id == organization_id,
            LinkedInAudienceSegment.is_archived.is_(False),
        )
        .order_by(LinkedInAudienceSegment.created_at.desc())
        .all()
    )


def get_segment(db: Session, segment_id: int) -> LinkedInAudienceSegment | None:
    return db.get(LinkedInAudienceSegment, int(segment_id))


def create_segment(db: Session, organization_id: int, **fields: Any) -> LinkedInAudienceSegment:
    keywords = (fields.get("keywords") or "").strip()
    if not keywords:
        raise ValueError("keywords обязательны")
    seg = LinkedInAudienceSegment(
        organization_id=organization_id,
        name=(fields.get("name") or keywords)[:255],
        keywords=keywords[:512],
        company=(fields.get("company") or "").strip() or None,
        location=(fields.get("location") or "").strip() or None,
        industry=(fields.get("industry") or "").strip() or None,
        title=(fields.get("title") or "").strip() or None,
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


def build_search_keywords(seg: LinkedInAudienceSegment) -> str:
    """Скомбинировать filters в одну строку keywords для LinkedIn search.

    LinkedIn без Sales Navigator не позволяет передавать структурированные
    фильтры через URL для большинства полей — остаётся только keyword-поиск.
    Поэтому строим строку: <keywords> [<title>] [<company>] [<location>] [<industry>].
    """
    parts: list[str] = [seg.keywords]
    for extra in (seg.title, seg.company, seg.location, seg.industry):
        if extra and extra.strip():
            parts.append(extra.strip())
    return " ".join(parts).strip()


__all__ = [
    "archive_segment",
    "build_search_keywords",
    "create_segment",
    "get_segment",
    "list_segments",
    "record_run",
]
