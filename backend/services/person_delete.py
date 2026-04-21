"""Каскадное удаление контакта (Person) и связанных записей."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models import (
    AIAgentRun,
    ContactedPerson,
    Conversation,
    OrganizationContactedPerson,
    CRMActivity,
    JobEvent,
    OutreachCampaign,
    OutreachQueue,
    Person,
    SequenceEnrollment,
    WarmupCampaign,
    WarmupQueue,
)


def delete_person_cascade(
    db: Session, person_id: int, *, organization_id: int | None = None
) -> bool:
    """Удаляет человека и связи. Возвращает False, если записи не было."""
    q = db.query(Person).filter(Person.id == person_id)
    if organization_id is not None:
        q = q.filter(Person.organization_id == organization_id)
    person = q.first()
    if not person:
        return False

    from backend.services.contacted_registry import mirror_installation_contacted_urls_for_person_ids

    mirror_installation_contacted_urls_for_person_ids(db, [int(person_id)])

    db.query(ContactedPerson).filter(ContactedPerson.person_id == person_id).delete(
        synchronize_session=False
    )
    db.query(OrganizationContactedPerson).filter(
        OrganizationContactedPerson.person_id == person_id
    ).delete(synchronize_session=False)
    db.query(CRMActivity).filter(CRMActivity.person_id == person_id).delete(
        synchronize_session=False
    )
    db.query(OutreachQueue).filter(OutreachQueue.person_id == person_id).delete(
        synchronize_session=False
    )
    db.query(WarmupQueue).filter(WarmupQueue.person_id == person_id).delete(
        synchronize_session=False
    )
    db.query(SequenceEnrollment).filter(SequenceEnrollment.person_id == person_id).delete(
        synchronize_session=False
    )
    db.query(JobEvent).filter(JobEvent.person_id == person_id).update(
        {JobEvent.person_id: None},
        synchronize_session=False,
    )
    db.query(Conversation).filter(Conversation.person_id == person_id).update(
        {Conversation.person_id: None},
        synchronize_session=False,
    )
    db.query(AIAgentRun).filter(AIAgentRun.person_id == person_id).update(
        {AIAgentRun.person_id: None},
        synchronize_session=False,
    )

    for camp in db.query(OutreachCampaign).all():
        ids = list(camp.person_ids or [])
        if person_id in ids:
            camp.person_ids = [i for i in ids if i != person_id]
    for camp in db.query(WarmupCampaign).all():
        ids = list(camp.person_ids or [])
        if person_id in ids:
            camp.person_ids = [i for i in ids if i != person_id]

    db.delete(person)
    db.commit()
    return True
