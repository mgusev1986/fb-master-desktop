"""Единая проверка занятости FB-аккаунта между автоматизациями."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.models import FBAccount, Job, OutreachCampaign, SequenceCampaign, WarmupCampaign

_RUNNING_JOB_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class AccountAutomationConflict:
    fb_account_id: int
    code: str
    message: str


def _account_label(db: Session, fb_account_id: int) -> str:
    acc = db.get(FBAccount, fb_account_id)
    if acc and (acc.label or "").strip():
        return acc.label.strip()
    return f"#{fb_account_id}"


def _normalized_ids(raw_ids: Iterable[int | str] | None) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in raw_ids or []:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid in seen:
            continue
        seen.add(aid)
        out.append(aid)
    return out


def _campaign_uses_account(raw_ids: object, fb_account_id: int) -> bool:
    return fb_account_id in _normalized_ids(raw_ids if isinstance(raw_ids, list) else [])


def _job_account_id(job: Job) -> int | None:
    snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
    raw = snap.get("fb_account_id")
    try:
        aid = int(raw)
    except (TypeError, ValueError):
        return None
    return aid if aid > 0 else None


def find_account_automation_conflict(
    db: Session,
    fb_account_ids: Iterable[int | str] | None,
    *,
    ignore_outreach_campaign_id: int | None = None,
    ignore_sequence_campaign_id: int | None = None,
    ignore_warmup_campaign_id: int | None = None,
    ignore_natural_warmup_account_id: int | None = None,
) -> AccountAutomationConflict | None:
    """Возвращает первый найденный конфликт автоматизаций для выбранных аккаунтов.

    Мессенджер сюда намеренно не входит: он разрешён параллельно.
    """

    for fb_account_id in _normalized_ids(fb_account_ids):
        label = _account_label(db, fb_account_id)
        acc = db.get(FBAccount, fb_account_id)
        if (
            acc
            and acc.id != ignore_natural_warmup_account_id
            and acc.readiness_status == "warming"
            and isinstance(acc.natural_warmup_json, dict)
        ):
            return AccountAutomationConflict(
                fb_account_id=fb_account_id,
                code="natural_warmup",
                message=f"Аккаунт «{label}» уже занят прогревом аккаунта.",
            )

        for camp in (
            db.query(OutreachCampaign)
            .filter(OutreachCampaign.status == "running")
            .all()
        ):
            if camp.id == ignore_outreach_campaign_id:
                continue
            if _campaign_uses_account(camp.fb_account_ids, fb_account_id):
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="outreach",
                    message=f"Аккаунт «{label}» уже занят рассылкой.",
                )

        for camp in (
            db.query(SequenceCampaign)
            .filter(SequenceCampaign.status == "active")
            .all()
        ):
            if camp.id == ignore_sequence_campaign_id:
                continue
            if _campaign_uses_account(camp.fb_account_ids, fb_account_id):
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="sequence",
                    message=f"Аккаунт «{label}» уже занят режимом агента.",
                )

        for camp in (
            db.query(WarmupCampaign)
            .filter(WarmupCampaign.status == "running")
            .all()
        ):
            if camp.id == ignore_warmup_campaign_id:
                continue
            if _campaign_uses_account(camp.fb_account_ids, fb_account_id):
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="warmup",
                    message=f"Аккаунт «{label}» уже занят прогревом по людям.",
                )

        for job in (
            db.query(Job)
            .filter(
                Job.status.in_(_RUNNING_JOB_STATUSES),
                Job.job_type.in_(
                    (
                        "donor_friends_parse",
                        "discovery",
                        "people_language_segment",
                        "natural_warmup",
                        "account_branding",
                    )
                ),
            )
            .all()
        ):
            if _job_account_id(job) != fb_account_id:
                continue
            if job.job_type == "donor_friends_parse":
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="parser",
                    message=f"Аккаунт «{label}» уже занят парсером.",
                )
            if job.job_type == "discovery":
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="discovery",
                    message=f"Аккаунт «{label}» уже занят автопоиском аудитории.",
                )
            if job.job_type == "people_language_segment":
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="people_language_segment",
                    message=f"Аккаунт «{label}» уже занят сегментацией базы по языку.",
                )
            if job.job_type == "natural_warmup" and fb_account_id != ignore_natural_warmup_account_id:
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="natural_warmup",
                    message=f"Аккаунт «{label}» уже занят прогревом аккаунта.",
                )
            if job.job_type == "account_branding":
                return AccountAutomationConflict(
                    fb_account_id=fb_account_id,
                    code="account_branding",
                    message=f"Аккаунт «{label}» уже занят оформлением профиля.",
                )

    return None


def account_automation_conflict_message(
    db: Session,
    fb_account_ids: Iterable[int | str] | None,
    **kwargs,
) -> str | None:
    conflict = find_account_automation_conflict(db, fb_account_ids, **kwargs)
    return conflict.message if conflict else None
