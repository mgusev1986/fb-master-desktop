"""Каскадное удаление донора с сохранением истории «уже писали».

Симптом без этого сервиса: handler `/donors/{id}/delete` делает только `db.delete(donor)`,
оставляя orphan'ы в `import_batches` и `people` (FK не каскадирует автоматически).
В выпадающем списке источников рассылки продолжают висеть партии удалённого донора
(№6, №9, №10, …) — клиенту это «не убрать».

Этот сервис каскадно удаляет:
  * все ImportBatch с donor_id == donor.id (и всех Person, привязанных к ним)
  * orphan'ы Person с donor_id == donor.id без import_batch_id
  * сам Donor

При этом ПЕРЕД удалением Person'ов через mirror_installation_contacted_urls_for_person_ids
(после 2.66-фикса — фильтрует только тех, у кого реальный след контакта) URL'ы тех
получателей, кому реально отправляли ЛС/комментарий, попадают в глобальный реестр
`installation_contacted_profile_urls`. На повторной загрузке тех же Facebook-профилей
они автоматически отфильтровываются скипом «уже писали» и кампания не дублирует.
Никогда не контактированные карточки в реестр НЕ уходят — их можно загружать заново.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.models import (
    AIAgentRun,
    Conversation,
    ContactedPerson,
    CRMActivity,
    Donor,
    ImportBatch,
    JobEvent,
    OrganizationContactedPerson,
    OutreachCampaign,
    OutreachQueue,
    Person,
    SequenceEnrollment,
    WarmupCampaign,
    WarmupQueue,
)

logger = logging.getLogger(__name__)


def _detach_person_ids_from_campaigns(db: Session, person_ids: set[int]) -> None:
    if not person_ids:
        return
    for camp in db.query(OutreachCampaign).all():
        ids = list(camp.person_ids or [])
        new_ids = [i for i in ids if i not in person_ids]
        if new_ids != ids:
            camp.person_ids = new_ids
    for camp in db.query(WarmupCampaign).all():
        ids = list(camp.person_ids or [])
        new_ids = [i for i in ids if i not in person_ids]
        if new_ids != ids:
            camp.person_ids = new_ids


def _wipe_people_keeping_contacted_history(db: Session, person_ids: list[int]) -> int:
    """
    Удаляет Person'ы и все их per-account записи, **сохраняя** URL'ы тех, кому реально
    отправляли ЛС/комментарий, в `installation_contacted_profile_urls`.

    Соблюдает 2.66-логику mirror_installation_contacted_urls_for_person_ids: зеркалятся
    URL только тех person_id, у кого есть реальный след исходящего касания (ContactedPerson,
    OrganizationContactedPerson, OutreachQueue.done, CRM outreach_dm/comment, JobEvent
    outreach_row_done OK, исходящие сообщения Messenger, sent-очередь). Для никогда не
    контактированных карточек URL в глобальный реестр не уйдёт — на новой загрузке они
    снова доступны для рассылки.
    """
    if not person_ids:
        return 0

    from backend.services.contacted_registry import (
        mirror_installation_contacted_urls_for_person_ids,
    )

    pids = [int(x) for x in person_ids]
    pid_set = set(pids)

    mirror_installation_contacted_urls_for_person_ids(db, pids)

    db.query(ContactedPerson).filter(ContactedPerson.person_id.in_(pids)).delete(
        synchronize_session=False
    )
    db.query(OrganizationContactedPerson).filter(
        OrganizationContactedPerson.person_id.in_(pids)
    ).delete(synchronize_session=False)
    db.query(CRMActivity).filter(CRMActivity.person_id.in_(pids)).delete(
        synchronize_session=False
    )
    db.query(OutreachQueue).filter(OutreachQueue.person_id.in_(pids)).delete(
        synchronize_session=False
    )
    db.query(WarmupQueue).filter(WarmupQueue.person_id.in_(pids)).delete(
        synchronize_session=False
    )
    db.query(SequenceEnrollment).filter(SequenceEnrollment.person_id.in_(pids)).delete(
        synchronize_session=False
    )
    db.query(JobEvent).filter(JobEvent.person_id.in_(pids)).update(
        {JobEvent.person_id: None},
        synchronize_session=False,
    )
    db.query(Conversation).filter(Conversation.person_id.in_(pids)).update(
        {Conversation.person_id: None},
        synchronize_session=False,
    )
    db.query(AIAgentRun).filter(AIAgentRun.person_id.in_(pids)).update(
        {AIAgentRun.person_id: None},
        synchronize_session=False,
    )

    _detach_person_ids_from_campaigns(db, pid_set)

    db.query(Person).filter(Person.id.in_(pids)).delete(synchronize_session=False)
    db.flush()
    return len(pids)


def delete_donor_cascading_keeping_contacted_history(
    db: Session,
    *,
    donor_id: int,
    organization_id: int,
) -> dict[str, int]:
    """
    Удаляет донора + все его партии импорта + всех Person'ов; сохраняет URL'ы реально
    контактированных в глобальном реестре.

    Возвращает счётчики (batches_deleted, people_deleted, donor_deleted).
    """
    out = {"batches_deleted": 0, "people_deleted": 0, "donor_deleted": 0}

    donor = (
        db.query(Donor)
        .filter(Donor.id == int(donor_id), Donor.organization_id == int(organization_id))
        .first()
    )
    if donor is None:
        return out

    batches = (
        db.query(ImportBatch)
        .filter(
            ImportBatch.organization_id == int(organization_id),
            ImportBatch.donor_id == int(donor_id),
        )
        .all()
    )

    # 1) Все партии этого донора (с правильным mirror в глобальный URL-реестр).
    for batch in batches:
        rows = (
            db.query(Person.id).filter(Person.import_batch_id == int(batch.id)).all()
        )
        person_ids = [r[0] for r in rows]
        if person_ids:
            removed = _wipe_people_keeping_contacted_history(db, person_ids)
            out["people_deleted"] += removed
        db.delete(batch)
        db.flush()
        out["batches_deleted"] += 1

    # 2) Orphan'ы: Person привязан к донору напрямую (Person.donor_id), но не через ImportBatch.
    orphan_pids = [
        r[0]
        for r in db.query(Person.id)
        .filter(
            Person.organization_id == int(organization_id),
            Person.donor_id == int(donor_id),
        )
        .all()
    ]
    if orphan_pids:
        removed = _wipe_people_keeping_contacted_history(db, orphan_pids)
        out["people_deleted"] += removed

    # 3) Сам донор.
    db.delete(donor)
    db.flush()
    out["donor_deleted"] = 1

    db.commit()
    logger.info(
        "donor cascade delete: donor_id=%s org=%s batches=%s people=%s",
        donor_id,
        organization_id,
        out["batches_deleted"],
        out["people_deleted"],
    )
    return out
