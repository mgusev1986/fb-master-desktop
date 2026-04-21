"""Прямая ссылка на тред Messenger (facebook.com / messenger.com) для webview /messenger2."""

from __future__ import annotations

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.models import Conversation, FBAccount, Message, Person


def is_safe_facebook_messenger_nav_url(url: str) -> bool:
    s = (url or "").strip()
    if not s.lower().startswith("https://"):
        return False
    low = s.lower()
    if "facebook.com/messages/" in low:
        return True
    if "messenger.com/" in low and "/t/" in low:
        return True
    if "business.facebook.com/messages/" in low:
        return True
    return False


def inbox_search_hint_for_person(person: Person) -> str:
    display = (person.display_name or "").strip()
    if display:
        return display
    canonical = (person.canonical_url or "").strip()
    if canonical:
        return canonical.replace("https://www.facebook.com/", "").strip("/")
    return str(person.id)


def resolve_facebook_messenger_thread_url_for_person(
    db: Session,
    org_id: int,
    person: Person,
) -> tuple[str | None, str]:
    """
    (https_url_треда_или_None, подсказка_для_поиска).
    URL берётся из последнего активного Conversation.peer_url или строится из canonical_url профиля.
    """
    from backend.services.sequence_actions import dm_direct_thread_url_for_person

    msg_max_subq = (
        db.query(
            Message.conversation_id.label("_mcid"),
            func.max(Message.created_at).label("_mmsg"),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    _la = Conversation.last_at
    _ma = msg_max_subq.c._mmsg
    _activity = case(
        (_la.is_(None), _ma),
        (_ma.is_(None), _la),
        (_la > _ma, _la),
        else_=_ma,
    )
    conv = (
        db.query(Conversation)
        .join(FBAccount, Conversation.fb_account_id == FBAccount.id)
        .outerjoin(msg_max_subq, msg_max_subq.c._mcid == Conversation.id)
        .filter(
            Conversation.person_id == person.id,
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(_activity.desc().nullslast(), Conversation.id.desc())
        .first()
    )
    hint = inbox_search_hint_for_person(person)
    if conv and (conv.peer_url or "").strip():
        u = conv.peer_url.strip().split("?")[0].split("#")[0]
        if is_safe_facebook_messenger_nav_url(u):
            return u[:2048], hint
    dm = dm_direct_thread_url_for_person(
        (person.canonical_url or "").strip(),
        person.raw_meta if isinstance(person.raw_meta, dict) else None,
    )
    if dm:
        u = dm.strip().split("?")[0].split("#")[0]
        if is_safe_facebook_messenger_nav_url(u):
            return u[:2048], hint
    return None, hint
