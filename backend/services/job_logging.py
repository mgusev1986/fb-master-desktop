"""Хелперы записи в таблицы jobs / job_events."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.models import CRMActivity, Job, JobEvent
from backend.services.job_failure_diagnostics import write_failure_diagnostic

logger = logging.getLogger(__name__)


def create_job(
    db: Session,
    *,
    organization_id: int,
    job_type: str,
    config_snapshot: dict[str, Any] | None = None,
    admin_id: int | None = None,
) -> Job:
    job = Job(
        organization_id=organization_id,
        job_type=job_type,
        status="queued",
        config_snapshot=config_snapshot,
        created_by_admin_id=admin_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("Job #%d (%s) created, correlation=%s", job.id, job_type, job.correlation_id)
    return job


def start_job(db: Session, job: Job) -> None:
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    db.commit()


def finish_job(
    db: Session,
    job: Job,
    *,
    status: str = "success",
    error: str | None = None,
    clear_progress: bool = False,
) -> None:
    job.status = status
    job.ended_at = datetime.now(timezone.utc)
    if error:
        job.error_summary = error[:2000]
    if clear_progress:
        job.progress_json = None
    db.commit()


def update_job_progress(db: Session, job_id: int, data: dict[str, Any]) -> None:
    j = db.get(Job, job_id)
    if not j:
        return
    prev = j.progress_json if isinstance(j.progress_json, dict) else {}
    j.progress_json = {**prev, **data}
    db.commit()


def log_job_event(
    db: Session,
    *,
    job_id: int,
    event_type: str,
    severity: str = "info",
    person_id: int | None = None,
    fb_account_id: int | None = None,
    outcome: str | None = None,
    duration_ms: int | None = None,
    payload: dict[str, Any] | None = None,
    diagnostic_context: dict[str, Any] | None = None,
) -> JobEvent:
    evt = JobEvent(
        job_id=job_id,
        event_type=event_type,
        severity=severity,
        person_id=person_id,
        fb_account_id=fb_account_id,
        outcome=outcome,
        duration_ms=duration_ms,
        payload=payload,
    )
    db.add(evt)
    db.commit()
    should_write_diagnostic = outcome == "fail" or severity == "error" or event_type.endswith("_error")
    if should_write_diagnostic:
        job = db.get(Job, job_id)
        diag_path, diag_summary = write_failure_diagnostic(
            job_id=job_id,
            event_id=evt.id,
            event_type=event_type,
            severity=severity,
            outcome=outcome,
            payload=payload,
            correlation_id=job.correlation_id if job else None,
            context=diagnostic_context,
        )
        if diag_path is not None:
            merged_payload = dict(payload or {})
            merged_payload["diagnostic_log_path"] = str(diag_path)
            if diag_summary:
                merged_payload["diagnostic_summary"] = diag_summary
            evt.payload = merged_payload
            db.commit()
    return evt


def log_crm_activity(
    db: Session,
    *,
    person_id: int,
    activity_type: str,
    fb_account_id: int | None = None,
    detail: str | None = None,
) -> CRMActivity:
    act = CRMActivity(
        person_id=person_id,
        fb_account_id=fb_account_id,
        activity_type=activity_type,
        detail=detail,
    )
    db.add(act)
    db.commit()
    return act
