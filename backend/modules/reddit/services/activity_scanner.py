"""Recent visible activity scanner.

Для одного пользователя читает `/user/{name}/overview?limit=25` и
отдаёт последние посты+комментарии. Также обновляет
`RedditUserProfile.last_visible_action_at`, `last_post_at`,
`last_comment_at` и создаёт snapshot в
`reddit_recent_activity_snapshots` для timeline.

ВАЖНО: это не online-status. Reddit не даёт online-сигнал.
Мы используем только видимые действия.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import (
    RedditRecentActivitySnapshot,
    RedditUserProfile,
)
from backend.modules.reddit.services.api_client import RedditApiError, api_call

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.modules.reddit.models import RedditAccount


_WINDOW_TO_HOURS: dict[str, int] = {
    "1h": 1,
    "24h": 24,
    "3d": 72,
    "7d": 168,
    "30d": 720,
}


@dataclass(frozen=True)
class ActivityItem:
    kind: str  # "post" | "comment"
    at: datetime
    subreddit: str
    permalink: str
    excerpt: str


@dataclass(frozen=True)
class UserActivityReport:
    username: str
    karma_total: int | None
    account_age_days: int | None
    last_post_at: datetime | None
    last_comment_at: datetime | None
    posts_count: int
    comments_count: int
    subreddits_seen: tuple[str, ...]
    latest_items: tuple[ActivityItem, ...]
    error: str | None = None


def _window_start(window: str, custom_hours: int | None = None) -> datetime | None:
    if window == "custom":
        hours = max(1, int(custom_hours or 24))
    else:
        hours = _WINDOW_TO_HOURS.get(window, 24)
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _apply_about_payload(user: RedditUserProfile, about: dict[str, Any]) -> None:
    user.karma_link = int(about.get("link_karma") or 0)
    user.karma_comment = int(about.get("comment_karma") or 0)
    created = about.get("created_utc")
    if created:
        try:
            d = datetime.fromtimestamp(float(created), tz=timezone.utc)
            user.account_age_days = max(0, int((datetime.now(timezone.utc) - d).days))
        except (TypeError, ValueError):
            pass


def scan_user(
    db: "Session",
    account: "RedditAccount",
    organization_id: int | None,
    username: str,
    *,
    window: str = "24h",
    custom_hours: int | None = None,
    limit_overview: int = 50,
) -> UserActivityReport:
    """Собрать отчёт по одному пользователю за окно времени."""
    username = (username or "").lstrip("u/").strip()
    if not username:
        return UserActivityReport(
            username="", karma_total=None, account_age_days=None,
            last_post_at=None, last_comment_at=None,
            posts_count=0, comments_count=0, subreddits_seen=(),
            latest_items=(), error="empty username",
        )

    # about
    try:
        about_resp = api_call(db, account, "GET", f"/user/{username}/about")
    except RedditApiError as exc:
        return UserActivityReport(
            username=username, karma_total=None, account_age_days=None,
            last_post_at=None, last_comment_at=None,
            posts_count=0, comments_count=0, subreddits_seen=(),
            latest_items=(), error=str(exc),
        )

    about_data = {}
    if isinstance(about_resp.data, dict):
        about_data = about_resp.data.get("data") or {}

    user = (
        db.query(RedditUserProfile)
        .filter(
            RedditUserProfile.organization_id == organization_id,
            RedditUserProfile.username == username,
        )
        .one_or_none()
    )
    if user is None:
        user = RedditUserProfile(organization_id=organization_id, username=username)
        db.add(user)
        db.flush()
    if about_data:
        _apply_about_payload(user, about_data)

    # overview
    try:
        resp = api_call(
            db, account, "GET", f"/user/{username}/overview",
            params={"limit": str(min(100, max(1, limit_overview)))},
        )
    except RedditApiError as exc:
        db.commit()
        return UserActivityReport(
            username=username,
            karma_total=(user.karma_link or 0) + (user.karma_comment or 0),
            account_age_days=user.account_age_days,
            last_post_at=user.last_post_at,
            last_comment_at=user.last_comment_at,
            posts_count=0,
            comments_count=0,
            subreddits_seen=(),
            latest_items=(),
            error=str(exc),
        )

    data = resp.data or {}
    listing = data.get("data", {}).get("children", []) if isinstance(data, dict) else []

    window_start = _window_start(window, custom_hours)
    posts_count = 0
    comments_count = 0
    subreddits_set: set[str] = set()
    items: list[ActivityItem] = []
    latest_post: datetime | None = None
    latest_comment: datetime | None = None

    for child in listing:
        kind_code = (child or {}).get("kind")  # "t1"=comment, "t3"=post
        d = (child or {}).get("data") or {}
        if not d:
            continue
        created = d.get("created_utc")
        try:
            at = datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None
        except (TypeError, ValueError):
            at = None
        if at is None:
            continue

        sub = (d.get("subreddit") or "").strip()
        if sub:
            subreddits_set.add(f"r/{sub}")

        if kind_code == "t3":
            if latest_post is None or at > latest_post:
                latest_post = at
            if window_start and at >= window_start:
                posts_count += 1
                items.append(ActivityItem(
                    kind="post",
                    at=at,
                    subreddit=sub,
                    permalink=d.get("permalink") or "",
                    excerpt=(d.get("title") or "")[:200],
                ))
        elif kind_code == "t1":
            if latest_comment is None or at > latest_comment:
                latest_comment = at
            if window_start and at >= window_start:
                comments_count += 1
                items.append(ActivityItem(
                    kind="comment",
                    at=at,
                    subreddit=sub,
                    permalink=d.get("permalink") or "",
                    excerpt=(d.get("body") or "")[:200],
                ))

    user.last_post_at = latest_post or user.last_post_at
    user.last_comment_at = latest_comment or user.last_comment_at
    user.last_visible_action_at = (
        max(filter(None, (latest_post, latest_comment))) if (latest_post or latest_comment) else user.last_visible_action_at
    )
    user.subreddits_seen_json = sorted(subreddits_set) or user.subreddits_seen_json
    user.last_refreshed_at = datetime.now(timezone.utc)

    # snapshot
    db.add(RedditRecentActivitySnapshot(
        user_profile_id=user.id,
        window_label=window if window != "custom" else f"custom:{custom_hours}h",
        latest_post_at=latest_post,
        latest_comment_at=latest_comment,
        posts_count=posts_count,
        comments_count=comments_count,
        subreddits_json=sorted(subreddits_set),
    ))
    db.commit()

    items.sort(key=lambda it: it.at, reverse=True)

    return UserActivityReport(
        username=username,
        karma_total=(user.karma_link or 0) + (user.karma_comment or 0),
        account_age_days=user.account_age_days,
        last_post_at=latest_post,
        last_comment_at=latest_comment,
        posts_count=posts_count,
        comments_count=comments_count,
        subreddits_seen=tuple(sorted(subreddits_set)),
        latest_items=tuple(items[:20]),
        error=None,
    )


def scan_usernames(
    db: "Session",
    account: "RedditAccount",
    organization_id: int | None,
    usernames: list[str],
    *,
    window: str = "24h",
    custom_hours: int | None = None,
) -> list[UserActivityReport]:
    out: list[UserActivityReport] = []
    for raw in usernames:
        name = raw.strip()
        if not name:
            continue
        out.append(scan_user(db, account, organization_id, name, window=window, custom_hours=custom_hours))
    return out


__all__ = [
    "ActivityItem",
    "UserActivityReport",
    "scan_user",
    "scan_usernames",
]
