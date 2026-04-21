"""Служебная логика для раздела «Режим агента» поверх sequence_campaigns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.models import Person, SequenceCampaign, SequenceEnrollment, SequenceStep
from backend.services.contacted_registry import installation_global_skip_person_ids
from backend.services.person_language import apply_person_language_filter, normalize_person_language_filter


def campaign_person_filter(campaign: SequenceCampaign | None) -> dict[str, Any]:
    pf = getattr(campaign, "person_filter", None)
    return dict(pf) if isinstance(pf, dict) else {}


def normalize_wait_unit(raw: str | None) -> str:
    unit = (raw or "").strip().lower()
    if unit in {"minute", "minutes", "min", "mins"}:
        return "minutes"
    if unit in {"hour", "hours", "hr", "hrs"}:
        return "hours"
    return "days"


def normalize_step_wait_unit(raw: str | None) -> str:
    """Единица интервала между шагами сценария (партии используют normalize_wait_unit — дни/часы/минуты)."""
    unit = (raw or "").strip().lower()
    if unit in {"minute", "minutes", "min", "mins"}:
        return "minutes"
    if unit in {"hour", "hours", "hr", "hrs"}:
        return "hours"
    return "days"


def max_step_wait_value(unit: str) -> int:
    u = normalize_step_wait_unit(unit)
    if u == "minutes":
        return 60 * 24 * 365
    if u == "hours":
        return 24 * 365
    return 3650


def normalize_positive_int(raw: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def step_wait_settings(step: SequenceStep) -> tuple[int, str]:
    cfg = step.config if isinstance(step.config, dict) else {}
    unit = normalize_step_wait_unit(cfg.get("wait_unit"))
    raw_value = cfg.get("wait_value")
    if raw_value is None:
        return max(0, int(step.day_offset or 0)), "days"
    max_value = max_step_wait_value(unit)
    value = normalize_positive_int(raw_value, default=0, min_value=0, max_value=max_value)
    return value, unit


def wait_timedelta(value: int, unit: str) -> timedelta:
    u = normalize_step_wait_unit(unit)
    if u == "minutes":
        return timedelta(minutes=max(0, value))
    if u == "hours":
        return timedelta(hours=max(0, value))
    return timedelta(days=max(0, value))


def parse_dt_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def agent_automation_settings(campaign: SequenceCampaign) -> dict[str, Any]:
    pf = campaign_person_filter(campaign)
    batch_unit = normalize_wait_unit(str(pf.get("batch_wait_unit") or "days"))
    if batch_unit == "minutes":
        max_batch_wait = 60 * 24 * 365
        default_batch_wait = 30
    elif batch_unit == "hours":
        max_batch_wait = 24 * 365
        default_batch_wait = 24
    else:
        max_batch_wait = 3650
        default_batch_wait = 1
    return {
        "enabled": bool(pf.get("auto_batch_enabled")),
        "batch_size": normalize_positive_int(pf.get("batch_size"), default=20, min_value=1, max_value=1000),
        "batch_wait_value": normalize_positive_int(
            pf.get("batch_wait_value"),
            default=default_batch_wait,
            min_value=0,
            max_value=max_batch_wait,
        ),
        "batch_wait_unit": batch_unit,
        "last_batch_at": parse_dt_utc(pf.get("last_batch_at")),
        "crm_stage": (pf.get("crm_stage") or "").strip(),
        "extra_person_ids": [
            int(x) for x in (pf.get("extra_person_ids") or []) if str(x).strip().isdigit()
        ],
    }


def _positive_int_id(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    s = str(raw).strip()
    if s.isdigit() and int(s) > 0:
        return int(s)
    return None


def source_person_ids_for_campaign(db: Session, campaign: SequenceCampaign) -> list[int]:
    pf = campaign_person_filter(campaign)
    org_id = int(campaign.organization_id)
    donor_id_raw = pf.get("ui_donor_id")
    crm_stage = (pf.get("crm_stage") or "").strip()
    language_filter = normalize_person_language_filter(pf.get("language_filter"))
    all_people = bool(pf.get("all_people"))
    import_batch_id = _positive_int_id(pf.get("ui_import_batch_id"))
    ids: list[int] = []
    if import_batch_id is not None:
        query = db.query(Person.id).filter(
            Person.import_batch_id == import_batch_id,
            Person.organization_id == org_id,
        )
        if crm_stage:
            query = query.filter(Person.crm_stage == crm_stage)
        query = apply_person_language_filter(query, language_filter)
        ids.extend(r[0] for r in query.order_by(Person.id.asc()).all())
    elif all_people:
        query = db.query(Person.id).filter(Person.organization_id == org_id)
        if crm_stage:
            query = query.filter(Person.crm_stage == crm_stage)
        query = apply_person_language_filter(query, language_filter)
        ids.extend(r[0] for r in query.order_by(Person.id.asc()).all())
    elif isinstance(donor_id_raw, int):
        query = db.query(Person.id).filter(
            Person.donor_id == donor_id_raw,
            Person.organization_id == org_id,
        )
        if crm_stage:
            query = query.filter(Person.crm_stage == crm_stage)
        query = apply_person_language_filter(query, language_filter)
        ids.extend(r[0] for r in query.order_by(Person.id.asc()).all())
    for raw in pf.get("extra_person_ids") or []:
        if str(raw).strip().isdigit():
            ids.append(int(raw))
    uniq = list(dict.fromkeys(ids))
    if not uniq:
        return []
    ok = {
        r[0]
        for r in db.query(Person.id)
        .filter(Person.id.in_(uniq), Person.organization_id == org_id)
        .all()
    }
    return [i for i in uniq if i in ok]


def enrolled_person_ids_for_campaign(db: Session, campaign_id: int) -> set[int]:
    return {
        pid
        for (pid,) in db.query(SequenceEnrollment.person_id)
        .filter(SequenceEnrollment.sequence_id == campaign_id)
        .all()
    }


def remaining_person_ids_for_campaign(db: Session, campaign: SequenceCampaign) -> list[int]:
    existing = enrolled_person_ids_for_campaign(db, campaign.id)
    skip = installation_global_skip_person_ids(db)
    return [
        pid
        for pid in source_person_ids_for_campaign(db, campaign)
        if pid not in existing and pid not in skip
    ]


def active_enrollment_count(db: Session, campaign_id: int) -> int:
    return (
        db.query(SequenceEnrollment)
        .filter(
            SequenceEnrollment.sequence_id == campaign_id,
            SequenceEnrollment.status == "active",
        )
        .count()
    )


def unfinished_enrollment_count(db: Session, campaign_id: int) -> int:
    return (
        db.query(SequenceEnrollment)
        .filter(
            SequenceEnrollment.sequence_id == campaign_id,
            SequenceEnrollment.status != "completed",
        )
        .count()
    )


def next_batch_allowed_now(campaign: SequenceCampaign, *, now_utc: datetime | None = None) -> bool:
    settings = agent_automation_settings(campaign)
    last_batch_at = settings["last_batch_at"]
    if last_batch_at is None:
        return True
    now = now_utc or datetime.now(timezone.utc)
    return now >= last_batch_at + wait_timedelta(settings["batch_wait_value"], settings["batch_wait_unit"])


def enroll_next_batch(db: Session, campaign: SequenceCampaign, *, force: bool = False) -> int:
    if getattr(campaign, "status", None) != "active":
        return 0
    settings = agent_automation_settings(campaign)
    if not force and not settings["enabled"]:
        return 0
    if unfinished_enrollment_count(db, campaign.id) > 0:
        return 0
    if not force and not next_batch_allowed_now(campaign):
        return 0

    remaining = remaining_person_ids_for_campaign(db, campaign)
    if not remaining:
        return 0

    batch_size = settings["batch_size"]
    picked = remaining[:batch_size]
    account_ids = list(campaign.fb_account_ids or [])
    now = datetime.now(timezone.utc)
    for idx, person_id in enumerate(picked):
        fb_account_id = account_ids[idx % len(account_ids)] if account_ids else None
        db.add(
            SequenceEnrollment(
                sequence_id=campaign.id,
                person_id=person_id,
                fb_account_id=fb_account_id,
                current_step_index=0,
                status="active",
                error=None,
                last_run_at=None,
                created_at=now,
            )
        )

    pf = campaign_person_filter(campaign)
    pf["last_batch_at"] = now.isoformat()
    campaign.person_filter = pf
    db.add(campaign)
    db.commit()
    return len(picked)
