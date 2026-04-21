"""Audience search — поиск авторов постов / комментариев / по темам.

Основные источники:
  - by_subreddit       : авторы N недавних постов в r/<name>
  - by_post_permalink  : комментаторы конкретного поста
  - by_user_search     : /users/search?q=...
  - by_subreddit_comments : комментаторы последних постов в сабреддите
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import RedditUserProfile
from backend.modules.reddit.services.api_client import RedditApiError, api_call

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.modules.reddit.models import RedditAccount


@dataclass(frozen=True)
class ProspectCard:
    username: str
    source_kind: str
    source_ref: str | None
    activity_at: datetime | None
    title_excerpt: str | None
    subreddit: str | None
    karma_total: int | None
    account_age_days: int | None
    already_lead: bool


def _upsert_user(db: "Session", organization_id: int | None, data: dict[str, Any]) -> RedditUserProfile:
    username = (data.get("author") or data.get("name") or "").strip()
    if not username or username.lower() in ("[deleted]", "automoderator"):
        raise ValueError("empty username")
    row = (
        db.query(RedditUserProfile)
        .filter(
            RedditUserProfile.organization_id == organization_id,
            RedditUserProfile.username == username,
        )
        .one_or_none()
    )
    if row is None:
        row = RedditUserProfile(organization_id=organization_id, username=username)
        db.add(row)
        db.flush()
    return row


def _decorate_from_post(post: dict[str, Any]) -> tuple[datetime | None, str, str]:
    created = post.get("created_utc")
    at: datetime | None = None
    try:
        at = datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None
    except (TypeError, ValueError):
        at = None
    subreddit = (post.get("subreddit") or "").strip()
    title = (post.get("title") or post.get("body") or "")[:160]
    return at, subreddit, title


def search_by_subreddit(
    db: "Session",
    account: "RedditAccount",
    organization_id: int | None,
    subreddit: str,
    *,
    limit: int = 25,
    from_comments: bool = False,
) -> list[ProspectCard]:
    """Вернуть авторов недавних постов в r/<subreddit> (или комментаторов, если from_comments=True)."""
    name = (subreddit or "").lstrip("r/").lstrip("/").strip()
    if not name:
        return []

    if from_comments:
        path = f"/r/{name}/comments"
    else:
        path = f"/r/{name}/new"
    params = {"limit": str(min(100, max(1, limit)))}

    resp = api_call(db, account, "GET", path, params=params)
    data = resp.data or {}
    existing_lead_names = _existing_lead_names(db, organization_id)
    cards: list[ProspectCard] = []

    listing = data.get("data", {}).get("children", []) if isinstance(data, dict) else []
    seen = set()
    for child in listing:
        d = (child or {}).get("data") or {}
        if not d:
            continue
        try:
            user = _upsert_user(db, organization_id, d)
        except ValueError:
            continue
        if user.username in seen:
            continue
        seen.add(user.username)

        at, sub, title = _decorate_from_post(d)
        # обновить поля профиля
        if at:
            if from_comments:
                if user.last_comment_at is None or (user.last_comment_at and at > user.last_comment_at):
                    user.last_comment_at = at
            else:
                if user.last_post_at is None or (user.last_post_at and at > user.last_post_at):
                    user.last_post_at = at
            if (
                user.last_visible_action_at is None
                or (user.last_visible_action_at and at > user.last_visible_action_at)
            ):
                user.last_visible_action_at = at

        cards.append(ProspectCard(
            username=user.username,
            source_kind=("subreddit_comments" if from_comments else "subreddit_posts"),
            source_ref=f"r/{name}",
            activity_at=at,
            title_excerpt=title,
            subreddit=sub,
            karma_total=(user.karma_link or 0) + (user.karma_comment or 0),
            account_age_days=user.account_age_days,
            already_lead=user.username in existing_lead_names,
        ))
    db.commit()
    return cards


def search_by_post_permalink(
    db: "Session",
    account: "RedditAccount",
    organization_id: int | None,
    permalink: str,
    *,
    limit: int = 50,
) -> list[ProspectCard]:
    """Комментаторы поста по permalink."""
    raw = (permalink or "").strip()
    if not raw:
        return []
    # Приводим к пути API: /r/<sr>/comments/<id>/<slug>.json
    if raw.startswith("http"):
        # снять префикс https://www.reddit.com
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        path = parsed.path
    else:
        path = raw
    if not path.startswith("/"):
        path = "/" + path
    # Reddit API comments endpoint
    if "/comments/" not in path:
        return []

    params = {"limit": str(min(500, max(1, limit))), "depth": "3"}
    resp = api_call(db, account, "GET", path, params=params)
    data = resp.data
    existing_lead_names = _existing_lead_names(db, organization_id)
    cards: list[ProspectCard] = []
    seen: set[str] = set()

    # Response: [post_listing, comments_listing]
    if isinstance(data, list) and len(data) >= 2:
        comments = (data[1] or {}).get("data", {}).get("children", [])
        stack = list(comments)
        while stack:
            node = stack.pop(0)
            d = (node or {}).get("data") or {}
            if not d:
                continue
            try:
                user = _upsert_user(db, organization_id, d)
            except ValueError:
                continue
            if user.username in seen:
                # но дети всё равно обойти
                replies = d.get("replies")
                if isinstance(replies, dict):
                    stack.extend(replies.get("data", {}).get("children", []) or [])
                continue
            seen.add(user.username)
            at, sub, title = _decorate_from_post(d)
            if at and (user.last_comment_at is None or (user.last_comment_at and at > user.last_comment_at)):
                user.last_comment_at = at
                user.last_visible_action_at = at
            cards.append(ProspectCard(
                username=user.username,
                source_kind="post_comments",
                source_ref=raw,
                activity_at=at,
                title_excerpt=title,
                subreddit=sub,
                karma_total=(user.karma_link or 0) + (user.karma_comment or 0),
                account_age_days=user.account_age_days,
                already_lead=user.username in existing_lead_names,
            ))
            replies = d.get("replies")
            if isinstance(replies, dict):
                stack.extend(replies.get("data", {}).get("children", []) or [])
    db.commit()
    return cards


def _existing_lead_names(db: "Session", organization_id: int | None) -> set[str]:
    from backend.modules.reddit.models import RedditLead

    rows = (
        db.query(RedditLead.username)
        .filter(RedditLead.organization_id == organization_id)
        .all()
    )
    return {r[0] for r in rows if r[0]}


def ingest_as_leads(
    db: "Session",
    organization_id: int | None,
    owner_account_id: int | None,
    cards: list[ProspectCard],
) -> int:
    """Создать RedditLead-записи из списка ProspectCard. Возвращает число добавленных."""
    from backend.modules.reddit.models import RedditLead, RedditUserProfile

    added = 0
    for card in cards:
        existing = (
            db.query(RedditLead)
            .filter(
                RedditLead.organization_id == organization_id,
                RedditLead.username == card.username,
            )
            .one_or_none()
        )
        if existing is not None:
            continue
        user = (
            db.query(RedditUserProfile)
            .filter(
                RedditUserProfile.organization_id == organization_id,
                RedditUserProfile.username == card.username,
            )
            .one_or_none()
        )
        lead = RedditLead(
            organization_id=organization_id,
            username=card.username,
            user_profile_id=user.id if user else None,
            source_type=card.source_kind,
            source_ref=card.source_ref,
            activity_timestamp=card.activity_at,
            owner_account_id=owner_account_id,
            topic_tags_json=[card.subreddit] if card.subreddit else None,
            notes=(card.title_excerpt or "")[:400],
            status="new",
        )
        db.add(lead)
        added += 1
    db.commit()
    return added


__all__ = [
    "ProspectCard",
    "ingest_as_leads",
    "search_by_post_permalink",
    "search_by_subreddit",
]
