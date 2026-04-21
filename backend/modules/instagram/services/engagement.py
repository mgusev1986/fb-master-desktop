"""Instagram engagement tasks — лайки/комменты/follow/story-like.

Workflow:
  1. Пользователь создаёт EngagementTask (kind, account, segment_id или targets list).
  2. Сервис разворачивает segment_id в targets (handles или post URLs) и создаёт
     EngagementQueueItem records со state='pending'.
  3. Worker обходит pending items с safety-капами (per-day per-account) и
     обновляет state.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import (
    InstagramAudienceSegment,
    InstagramEngagementQueueItem,
    InstagramEngagementTask,
    InstagramLead,
)


VALID_KINDS = ("like_post", "comment_post", "follow_user", "story_like")


def list_tasks(db: Session, organization_id: int) -> list[InstagramEngagementTask]:
    return (
        db.query(InstagramEngagementTask)
        .filter(InstagramEngagementTask.organization_id == organization_id)
        .order_by(InstagramEngagementTask.created_at.desc())
        .all()
    )


def get_task(db: Session, task_id: int) -> InstagramEngagementTask | None:
    return db.get(InstagramEngagementTask, int(task_id))


def create_task(db: Session, organization_id: int, **fields: Any) -> InstagramEngagementTask:
    kind = (fields.get("kind") or "like_post").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    task = InstagramEngagementTask(
        organization_id=organization_id,
        name=(fields.get("name") or "Engagement task").strip()[:255],
        kind=kind,
        account_id=int(fields["account_id"]) if fields.get("account_id") else None,
        template_id=int(fields["template_id"]) if fields.get("template_id") else None,
        segment_id=int(fields["segment_id"]) if fields.get("segment_id") else None,
        daily_cap=int(fields["daily_cap"]) if fields.get("daily_cap") else None,
        approval_mode=(fields.get("approval_mode") or "auto").strip(),
        targets_json=fields.get("targets_json") if isinstance(fields.get("targets_json"), list) else None,
        notes=(fields.get("notes") or "").strip() or None,
    )
    db.add(task)
    db.commit()
    return task


def expand_targets_to_queue(db: Session, task_id: int, *, manual_targets: list[str] | None = None) -> dict[str, Any]:
    """Разворачивает targets (manual_targets или segment) в EngagementQueueItem."""
    task = get_task(db, task_id)
    if task is None:
        return {"created": 0, "errors": ["task_not_found"]}

    sources: list[tuple[str, str, int | None]] = []  # (target_kind, target_ref, lead_id?)
    if manual_targets:
        for t in manual_targets:
            t = (t or "").strip()
            if not t:
                continue
            if "instagram.com/p/" in t.lower() or "instagram.com/reel/" in t.lower():
                sources.append(("post", t, None))
            else:
                handle = t.lstrip("@").lstrip("/")
                sources.append(("user", handle, None))

    if task.segment_id and not sources:
        # Берём leads из БД с source_ref совпадающим с segment.keywords (best-effort).
        seg = db.get(InstagramAudienceSegment, task.segment_id)
        if seg is not None:
            leads = (
                db.query(InstagramLead)
                .filter(
                    InstagramLead.organization_id == task.organization_id,
                    InstagramLead.source_ref == seg.keywords,
                )
                .limit(500)
                .all()
            )
            for l in leads:
                if task.kind == "like_post" or task.kind == "comment_post":
                    # Без post_url — не можем; пропустим (для like нужен post URL).
                    continue
                if task.kind == "follow_user" or task.kind == "story_like":
                    sources.append(("user", l.handle, l.id))

    # Дедуп: один и тот же target_ref не дважды.
    seen: set[str] = set()
    created = 0
    for kind, ref, lid in sources:
        key = f"{kind}:{ref.lower()}"
        if key in seen:
            continue
        seen.add(key)
        db.add(
            InstagramEngagementQueueItem(
                task_id=task.id, target_kind=kind, target_ref=ref,
                lead_id=lid, review_state="pending",
            )
        )
        created += 1
    db.commit()
    return {"created": created, "total_sources": len(sources)}


def list_queue_items(db: Session, task_id: int) -> list[InstagramEngagementQueueItem]:
    return (
        db.query(InstagramEngagementQueueItem)
        .filter(InstagramEngagementQueueItem.task_id == task_id)
        .order_by(InstagramEngagementQueueItem.created_at.asc())
        .limit(500)
        .all()
    )


def queue_summary(db: Session, task_id: int) -> dict[str, int]:
    items = list_queue_items(db, task_id)
    out = {"total": len(items), "pending": 0, "done": 0, "failed": 0, "skipped": 0}
    for it in items:
        out[it.review_state] = out.get(it.review_state, 0) + 1
    return out


def set_task_status(db: Session, task_id: int, status: str) -> InstagramEngagementTask | None:
    task = get_task(db, task_id)
    if task is None or status not in ("draft", "active", "paused", "completed", "stopped"):
        return None
    task.status = status
    db.commit()
    return task


__all__ = [
    "VALID_KINDS",
    "create_task", "expand_targets_to_queue", "get_task", "list_queue_items",
    "list_tasks", "queue_summary", "set_task_status",
]
