"""Общий реестр факта контакта: рассылка и переписка в Messenger."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, union

from backend.models import (
    CRMActivity,
    ContactedPerson,
    Conversation,
    FBAccount,
    InstallationContactedProfileUrl,
    JobEvent,
    Message,
    MessengerSendQueue,
    OrganizationContactedPerson,
    OutreachCampaign,
    OutreachQueue,
    Person,
)
from backend.services.fb_url_normalize import normalize_facebook_profile_url
from backend.services.outreach_campaign_done import (
    outreach_campaign_kind_normalized,
    outreach_done_person_ids,
)

# Успешные исходящие касания рассылки (аудит в CRM / job_events переживает сбои contacted_people).
_OUTREACH_SKIP_CRM_TYPES = ("outreach_dm", "outreach_comment")


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


def _norm_profile_url_for_installation_registry(raw: str) -> str:
    """Нормализованный URL профиля для глобального реестра; пустая строка — невалидный ввод."""
    s = (raw or "").strip()
    if not s:
        return ""
    nu = normalize_facebook_profile_url(s)
    url = (nu or s).strip()[:512]
    if not url:
        return ""
    low = url.lower().rstrip("/")
    if low in (
        "https://www.facebook.com",
        "http://www.facebook.com",
        "https://facebook.com",
        "http://facebook.com",
    ):
        return ""
    return url


def upsert_installation_contacted_profile_url(
    db,
    *,
    canonical_url: str,
    touched_at: datetime | None = None,
) -> None:
    """Глобально по БД: URL профиля, которому уже писали/комментировали (переживает смену person_id)."""
    now = touched_at or datetime.now(timezone.utc)
    url = _norm_profile_url_for_installation_registry(canonical_url)
    if not url:
        return
    row = (
        db.query(InstallationContactedProfileUrl)
        .filter(InstallationContactedProfileUrl.canonical_url == url)
        .first()
    )
    if row:
        row.last_message_at = now
        if row.first_contacted_at is None:
            row.first_contacted_at = now
    else:
        db.add(
            InstallationContactedProfileUrl(
                canonical_url=url,
                first_contacted_at=now,
                last_message_at=now,
            )
        )
    db.flush()


def mirror_installation_contacted_urls_for_person_ids(db, person_ids: list[int] | set[int]) -> None:
    """
    Перед удалением строк people: зафиксировать их canonical_url в глобальном реестре,
    чтобы повторный импорт того же профиля не обходил пропуск «уже писали».
    """
    if not person_ids:
        return
    pids = [int(x) for x in person_ids]
    rows = db.query(Person.canonical_url).filter(Person.id.in_(pids)).all()
    for (curl,) in rows:
        if curl and str(curl).strip():
            upsert_installation_contacted_profile_url(db, canonical_url=str(curl))
    db.flush()


def sync_installation_contacted_urls_from_legacy_tables(db) -> None:
    """
    Идемпотентно: подтягивает URL из contacted_people / organization_contacted_people / успешной очереди рассылки,
    а также из CRM (успешные ЛС/комментарии), job_events (outreach_row_done), фактических исходящих в Messenger
    (messages.direction=out) и успешной очереди отправки из UI (messenger_send_queue.sent), если они ещё не попали
    в installation_contacted_profile_urls (восстановление после сбоев или старых версий).
    """
    seen: set[str] = set()
    for (u,) in db.query(InstallationContactedProfileUrl.canonical_url).all():
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if nu:
                seen.add(nu)
    for (u,) in db.query(ContactedPerson.canonical_url).distinct():
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))
    for (u,) in db.query(OrganizationContactedPerson.canonical_url).distinct():
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))
    done_rows = (
        db.query(Person.canonical_url)
        .join(OutreachQueue, OutreachQueue.person_id == Person.id)
        .filter(OutreachQueue.status == "done")
        .distinct()
    )
    for (u,) in done_rows:
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))

    crm_urls = (
        db.query(Person.canonical_url)
        .join(CRMActivity, CRMActivity.person_id == Person.id)
        .filter(CRMActivity.activity_type.in_(_OUTREACH_SKIP_CRM_TYPES))
        .distinct()
    )
    for (u,) in crm_urls:
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))

    je_urls = (
        db.query(Person.canonical_url)
        .join(JobEvent, JobEvent.person_id == Person.id)
        .filter(
            JobEvent.event_type == "outreach_row_done",
            JobEvent.outcome == "ok",
        )
        .distinct()
    )
    for (u,) in je_urls:
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))

    for camp in db.query(OutreachCampaign).yield_per(100):
        cfg = camp.config if isinstance(camp.config, dict) else {}
        if outreach_campaign_kind_normalized(cfg.get("campaign_kind")) not in ("dm", "comment"):
            continue
        for pid in outreach_done_person_ids(cfg):
            p = db.get(Person, int(pid))
            if not p or not (p.canonical_url or "").strip():
                continue
            nu = _norm_profile_url_for_installation_registry(str(p.canonical_url))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(p.canonical_url))

    send_urls = (
        db.query(Person.canonical_url)
        .join(Conversation, Conversation.person_id == Person.id)
        .join(MessengerSendQueue, MessengerSendQueue.conversation_id == Conversation.id)
        .filter(
            Conversation.person_id.isnot(None),
            MessengerSendQueue.status == "sent",
        )
        .distinct()
    )
    for (u,) in send_urls:
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))

    msg_urls = (
        db.query(Person.canonical_url)
        .join(Conversation, Conversation.person_id == Person.id)
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.person_id.isnot(None),
            func.lower(func.trim(Message.direction)) == "out",
        )
        .distinct()
    )
    for (u,) in msg_urls:
        if u and str(u).strip():
            nu = _norm_profile_url_for_installation_registry(str(u))
            if not nu:
                continue
            if nu not in seen:
                seen.add(nu)
                upsert_installation_contacted_profile_url(db, canonical_url=str(u))
    db.flush()


def installation_global_contacted_canonical_urls_set(db) -> set[str]:
    """Множество канонических URL (нормализованных), по которым нельзя снова ставить в очередь при включённом пропуске."""
    out: set[str] = set()
    for (u,) in db.query(InstallationContactedProfileUrl.canonical_url).all():
        if u:
            nu = _norm_profile_url_for_installation_registry(str(u))
            if nu:
                out.add(nu)
    return out


def canonical_url_in_global_contacted_set(
    canonical_url: str | None, frozen_norm_urls: set[str]
) -> bool:
    """True, если URL профиля совпадает с глобальным реестром (после нормализации)."""
    if not frozen_norm_urls:
        return False
    nu = _norm_profile_url_for_installation_registry(str(canonical_url or ""))
    return bool(nu) and nu in frozen_norm_urls


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
    upsert_installation_contacted_profile_url(db, canonical_url=url, touched_at=now)


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
    else:
        upsert_installation_contacted_profile_url(db, canonical_url=url, touched_at=now)


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
    реестр «Уже контактировали», успешные строки рассылки (любая кампания), диалоги Messenger,
    исходящие сообщения в Messenger и успешные отправки из очереди UI.
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
    for (pid,) in (
        db.query(CRMActivity.person_id)
        .filter(
            CRMActivity.activity_type.in_(_OUTREACH_SKIP_CRM_TYPES),
            CRMActivity.person_id.isnot(None),
        )
        .distinct()
    ):
        if pid is not None:
            out.add(int(pid))
    for (pid,) in (
        db.query(JobEvent.person_id)
        .filter(
            JobEvent.event_type == "outreach_row_done",
            JobEvent.outcome == "ok",
            JobEvent.person_id.isnot(None),
        )
        .distinct()
    ):
        if pid is not None:
            out.add(int(pid))
    for (pid,) in (
        db.query(Conversation.person_id)
        .join(MessengerSendQueue, MessengerSendQueue.conversation_id == Conversation.id)
        .filter(
            Conversation.person_id.isnot(None),
            MessengerSendQueue.status == "sent",
        )
        .distinct()
    ):
        if pid is not None:
            out.add(int(pid))
    for (pid,) in (
        db.query(Conversation.person_id)
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.person_id.isnot(None),
            func.lower(func.trim(Message.direction)) == "out",
        )
        .distinct()
    ):
        if pid is not None:
            out.add(int(pid))
    for camp in db.query(OutreachCampaign).yield_per(100):
        cfg = camp.config if isinstance(camp.config, dict) else {}
        if outreach_campaign_kind_normalized(cfg.get("campaign_kind")) not in ("dm", "comment"):
            continue
        for pid in outreach_done_person_ids(cfg):
            out.add(int(pid))
    return out


def engaged_person_ids_subquery():
    """
    DISTINCT person_id для людей, с кем уже был контакт:
    1) через глобальный реестр contacted_people,
    2) через учёт на уровне кабинета organization_contacted_people,
    3) через связанный Messenger-диалог,
    4) через глобальный URL-реестр installation_contacted_profile_urls (в т.ч. после переимпорта контакта).
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
    inst_url_sq = select(InstallationContactedProfileUrl.canonical_url)
    installation_sel = select(Person.id.label("person_id")).where(
        Person.canonical_url.in_(inst_url_sq)
    )
    crm_sel = (
        select(CRMActivity.person_id.label("person_id"))
        .where(
            CRMActivity.activity_type.in_(_OUTREACH_SKIP_CRM_TYPES),
            CRMActivity.person_id.is_not(None),
        )
    )
    job_done_sel = (
        select(JobEvent.person_id.label("person_id"))
        .where(
            JobEvent.event_type == "outreach_row_done",
            JobEvent.outcome == "ok",
            JobEvent.person_id.is_not(None),
        )
    )
    return union(
        contacted_sel,
        org_sel,
        messenger_sel,
        installation_sel,
        crm_sel,
        job_done_sel,
    ).subquery()
