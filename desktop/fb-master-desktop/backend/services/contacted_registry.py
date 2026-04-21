"""Общий реестр факта контакта: рассылка и переписка в Messenger."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, union

from backend.models import (
    ContactedPerson,
    Conversation,
    FBAccount,
    OrganizationContactedPerson,
    OutreachQueue,
    Person,
)


def _resolve_organization_id_for_contact(
    db,
    *,
    person_id: int,
    fb_account_id: int,
) -> int | None:
    acc = db.get(FBAccount, int(fb_account_id))
    if acc is not None:
        return int(acc.organization_id)
    person = db.get(Person, int(person_id))
    if person is not None:
        return int(person.organization_id)
    return None


def upsert_organization_contacted(
    db,
    *,
    organization_id: int,
    person_id: int,
    canonical_url: str,
    last_fb_account_id: int | None,
    touched_at: datetime | None = None,
) -> None:
    now = touched_at or datetime.now(timezone.utc)
    url = (canonical_url or "").strip()[:512] or "https://www.facebook.com/"
    row = (
        db.query(OrganizationContactedPerson)
        .filter(
            OrganizationContactedPerson.organization_id == int(organization_id),
            OrganizationContactedPerson.person_id == int(person_id),
        )
        .first()
    )
    if row:
        row.last_message_at = now
        row.canonical_url = url
        if last_fb_account_id is not None:
            row.last_fb_account_id = int(last_fb_account_id)
        if row.first_contacted_at is None:
            row.first_contacted_at = now
    else:
        db.add(
            OrganizationContactedPerson(
                organization_id=int(organization_id),
                person_id=int(person_id),
                canonical_url=url,
                first_contacted_at=now,
                last_message_at=now,
                last_fb_account_id=int(last_fb_account_id) if last_fb_account_id is not None else None,
            )
        )
    db.flush()


def upsert_contacted(
    db,
    *,
    person_id: int,
    fb_account_id: int,
    canonical_url: str,
    touched_at: datetime | None = None,
) -> None:
    now = touched_at or datetime.now(timezone.utc)
    url = (canonical_url or "").strip()[:512] or "https://www.facebook.com/"
    row = (
        db.query(ContactedPerson)
        .filter(
            ContactedPerson.person_id == int(person_id),
            ContactedPerson.fb_account_id == int(fb_account_id),
        )
        .first()
    )
    if row:
        row.last_message_at = now
        row.canonical_url = url
        if row.first_contacted_at is None:
            row.first_contacted_at = now
    else:
        db.add(
            ContactedPerson(
                person_id=int(person_id),
                fb_account_id=int(fb_account_id),
                canonical_url=url,
                first_contacted_at=now,
                last_message_at=now,
            )
        )
    db.flush()

    org_id = _resolve_organization_id_for_contact(
        db, person_id=int(person_id), fb_account_id=int(fb_account_id)
    )
    if org_id is not None:
        upsert_organization_contacted(
            db,
            organization_id=org_id,
            person_id=int(person_id),
            canonical_url=url,
            last_fb_account_id=int(fb_account_id),
            touched_at=now,
        )


def preserve_contacted_registry_before_fb_account_delete(
    db,
    *,
    fb_account_id: int,
    organization_id: int,
) -> None:
    """
    Переносит пары из contacted_people в organization_contacted_people и удаляет строки,
    привязанные к аккаунту, чтобы удаление fb_accounts не теряло учёт для кабинета.
    """
    rows = (
        db.query(ContactedPerson)
        .filter(ContactedPerson.fb_account_id == int(fb_account_id))
        .all()
    )
    now = datetime.now(timezone.utc)
    oid = int(organization_id)
    for cp in rows:
        upsert_organization_contacted(
            db,
            organization_id=oid,
            person_id=int(cp.person_id),
            canonical_url=cp.canonical_url,
            last_fb_account_id=int(fb_account_id),
            touched_at=cp.last_message_at or cp.first_contacted_at or now,
        )
    if rows:
        db.flush()
    db.query(ContactedPerson).filter(ContactedPerson.fb_account_id == int(fb_account_id)).delete(
        synchronize_session=False
    )
    db.flush()


def prune_organization_contacted_if_no_account_pairs(
    db,
    *,
    organization_id: int,
    person_ids: set[int],
) -> None:
    """
    Удаляет organization_contacted_people, если для человека в кабинете не осталось
    записей contacted_people с живым FB-аккаунтом этого кабинета (после снятия пары в UI / кампании).
    """
    if not person_ids:
        return
    oid = int(organization_id)
    for pid in person_ids:
        still = (
            db.query(ContactedPerson.id)
            .join(FBAccount, FBAccount.id == ContactedPerson.fb_account_id)
            .filter(
                FBAccount.organization_id == oid,
                ContactedPerson.person_id == int(pid),
            )
            .first()
        )
        if still is None:
            db.query(OrganizationContactedPerson).filter(
                OrganizationContactedPerson.organization_id == oid,
                OrganizationContactedPerson.person_id == int(pid),
            ).delete(synchronize_session=False)


def installation_global_skip_person_ids(db) -> set[int]:
    """
    Все person_id, кому уже уходило исходящее касание в любой организации:
    реестр «Уже контактировали», успешные строки рассылки (любая кампания), диалоги Messenger.
    Используется для режима skip_contacted_scope=global.
    """
    out: set[int] = set()
    for (pid,) in db.query(OrganizationContactedPerson.person_id).distinct():
        if pid is not None:
            out.add(int(pid))
    for (pid,) in db.query(ContactedPerson.person_id).distinct():
        if pid is not None:
            out.add(int(pid))
    for (pid,) in (
        db.query(OutreachQueue.person_id)
        .filter(OutreachQueue.status == "done")
        .distinct()
    ):
        if pid is not None:
            out.add(int(pid))
    for (pid,) in (
        db.query(Conversation.person_id)
        .filter(Conversation.person_id.isnot(None))
        .distinct()
    ):
        if pid is not None:
            out.add(int(pid))
    return out


def engaged_person_ids_subquery():
    """
    DISTINCT person_id для людей, с кем уже был контакт:
    1) через глобальный реестр contacted_people,
    2) через учёт на уровне кабинета organization_contacted_people,
    3) через связанный Messenger-диалог.
    """
    contacted_sel = (
        select(ContactedPerson.person_id.label("person_id"))
        .where(ContactedPerson.person_id.is_not(None))
    )
    org_sel = (
        select(OrganizationContactedPerson.person_id.label("person_id"))
        .where(OrganizationContactedPerson.person_id.is_not(None))
    )
    messenger_sel = (
        select(Conversation.person_id.label("person_id"))
        .where(Conversation.person_id.is_not(None))
    )
    return union(contacted_sel, org_sel, messenger_sel).subquery()
