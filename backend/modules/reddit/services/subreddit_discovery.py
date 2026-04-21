"""Subreddit discovery — поиск сабреддитов через Reddit API.

Используем endpoints:
  - /subreddits/search?q=...&limit=...&sort=relevance|activity
  - /subreddits/popular
  - /r/{name}/about для детальной карточки

Результаты переиспользуются через RedditSubreddit (upsert по name).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import RedditSubreddit
from backend.modules.reddit.services.api_client import RedditApiError, api_call

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.modules.reddit.models import RedditAccount


@dataclass(frozen=True)
class SubredditCard:
    name: str
    title: str
    subscribers: int | None
    active_users: int | None
    summary: str
    url: str
    is_nsfw: bool
    language: str | None
    suitability_score: float
    risk_score: float
    saved: bool


def _score_suitability(subscribers: int | None, active: int | None) -> float:
    """Простая эвристика: размер × активность даёт score 0..1."""
    sub = subscribers or 0
    act = active or 0
    if sub <= 0:
        return 0.0
    # слишком мелкие / слишком гигантские — менее удобны для targeted outreach
    mid = 1.0
    if sub < 500:
        mid *= sub / 500
    elif sub > 2_000_000:
        mid *= 0.4
    elif sub > 500_000:
        mid *= 0.75
    # активность относительно размера
    if act > 0:
        ratio = min(1.0, act / max(1, sub) * 100)
        mid *= 0.5 + ratio / 2
    return round(max(0.0, min(1.0, mid)), 2)


def _score_risk(nsfw: bool, subscribers: int | None) -> float:
    """Простая эвристика риска: NSFW + слишком мелкий сабреддит = больше риск."""
    risk = 0.0
    if nsfw:
        risk += 0.5
    if (subscribers or 0) < 1000:
        risk += 0.3
    return round(min(1.0, risk), 2)


def _upsert_subreddit(db: "Session", organization_id: int | None, data: dict[str, Any]) -> RedditSubreddit:
    name = (data.get("display_name") or data.get("name") or "").lstrip("r/").strip()
    if not name:
        raise ValueError("subreddit name required")
    row = (
        db.query(RedditSubreddit)
        .filter(RedditSubreddit.organization_id == organization_id, RedditSubreddit.name == name)
        .one_or_none()
    )
    if row is None:
        row = RedditSubreddit(organization_id=organization_id, name=name)
        db.add(row)
        db.flush()
    row.display_name = data.get("title") or row.display_name
    row.subreddit_id = data.get("name") or row.subreddit_id  # API "name" = t5_xxx
    row.subscribers = data.get("subscribers") if data.get("subscribers") is not None else row.subscribers
    row.active_users_estimate = (
        data.get("active_user_count")
        if data.get("active_user_count") is not None
        else row.active_users_estimate
    )
    row.is_nsfw = bool(data.get("over18", row.is_nsfw))
    row.summary = data.get("public_description") or data.get("description") or row.summary or ""
    row.rules_summary = row.rules_summary  # не трогаем, если не пришло
    row.language_guess = data.get("lang") or row.language_guess
    row.suitability_score = _score_suitability(row.subscribers, row.active_users_estimate)
    row.risk_score = _score_risk(bool(row.is_nsfw), row.subscribers)
    row.last_refreshed_at = datetime.now(timezone.utc)
    return row


def _api_subreddit_search(
    db: "Session",
    account: "RedditAccount",
    query: str,
    limit: int = 25,
    sort: str = "relevance",
) -> list[dict[str, Any]]:
    params = {"q": query, "limit": str(min(100, max(1, limit))), "sort": sort, "include_over_18": "true"}
    resp = api_call(db, account, "GET", "/subreddits/search", params=params)
    out: list[dict[str, Any]] = []
    data = resp.data or {}
    if isinstance(data, dict):
        listing = data.get("data", {}).get("children", [])
        for child in listing:
            d = (child or {}).get("data") or {}
            if d:
                out.append(d)
    return out


def _api_subreddit_popular(
    db: "Session", account: "RedditAccount", limit: int = 25
) -> list[dict[str, Any]]:
    params = {"limit": str(min(100, max(1, limit)))}
    resp = api_call(db, account, "GET", "/subreddits/popular", params=params)
    out: list[dict[str, Any]] = []
    data = resp.data or {}
    if isinstance(data, dict):
        listing = data.get("data", {}).get("children", [])
        for child in listing:
            d = (child or {}).get("data") or {}
            if d:
                out.append(d)
    return out


def search_and_persist(
    db: "Session",
    account: "RedditAccount",
    organization_id: int | None,
    query: str,
    *,
    limit: int = 25,
    sort: str = "relevance",
) -> list[SubredditCard]:
    """Поиск + upsert в БД + сбор карточек."""
    if not query.strip():
        raw = _api_subreddit_popular(db, account, limit=limit)
    else:
        raw = _api_subreddit_search(db, account, query.strip(), limit=limit, sort=sort)

    saved_names = {
        r.name for r in db.query(RedditSubreddit).filter(
            RedditSubreddit.organization_id == organization_id
        ).all()
    }

    cards: list[SubredditCard] = []
    for d in raw:
        row = _upsert_subreddit(db, organization_id, d)
        cards.append(
            SubredditCard(
                name=row.name,
                title=row.display_name or row.name,
                subscribers=row.subscribers,
                active_users=row.active_users_estimate,
                summary=(row.summary or "")[:360],
                url=f"https://www.reddit.com/r/{row.name}",
                is_nsfw=bool(row.is_nsfw),
                language=row.language_guess,
                suitability_score=row.suitability_score or 0.0,
                risk_score=row.risk_score or 0.0,
                saved=row.name in saved_names,
            )
        )
    # commit для persistent upsert (search-страница может вернуть уже сохранённые)
    db.commit()
    return cards


def list_saved(db: "Session", organization_id: int | None) -> list[RedditSubreddit]:
    return (
        db.query(RedditSubreddit)
        .filter(RedditSubreddit.organization_id == organization_id)
        .order_by(RedditSubreddit.subscribers.desc().nullslast())
        .all()
    )


def delete(db: "Session", organization_id: int | None, subreddit_id: int) -> None:
    row = (
        db.query(RedditSubreddit)
        .filter(
            RedditSubreddit.id == subreddit_id,
            RedditSubreddit.organization_id == organization_id,
        )
        .one_or_none()
    )
    if row is not None:
        db.delete(row)
        db.commit()


def card_for(row: RedditSubreddit) -> SubredditCard:
    return SubredditCard(
        name=row.name,
        title=row.display_name or row.name,
        subscribers=row.subscribers,
        active_users=row.active_users_estimate,
        summary=(row.summary or "")[:360],
        url=f"https://www.reddit.com/r/{row.name}",
        is_nsfw=bool(row.is_nsfw),
        language=row.language_guess,
        suitability_score=row.suitability_score or 0.0,
        risk_score=row.risk_score or 0.0,
        saved=True,
    )


__all__ = [
    "SubredditCard",
    "card_for",
    "delete",
    "list_saved",
    "search_and_persist",
]
