"""Раздел «Система» — скорость, паузы, журнал задач и выгрузки."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import Integer, cast, func, or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    Conversation,
    Donor,
    FBAccount,
    ImportBatch,
    Job,
    JobEvent,
    OutreachCampaign,
    Person,
    SequenceCampaign,
    Setting,
    WarmupCampaign,
)
from backend.services.job_journal_narrative import (
    build_journal_context_by_job_id,
    collect_journal_lookup_ids,
    label_job_event_type,
)
from backend.services.cabinet_settings import build_settings_page_context, save_settings_from_form
from backend.services.content_disposition import attachment_content_disposition
from backend.services.messenger_settings import (
    set_messenger_auto_inbox_sync_interval_sec,
    set_messenger_poll_interval_sec,
)
from backend.services.sequence_timezone import COMMON_TIMEZONES, normalize_sequence_tz_name, raw_sequence_timezone_name
from backend.services.throttle import (
    MIN_FLOORS,
    PRESET_LABELS,
    PRESET_MULTIPLIERS,
    REFERENCE_DELAYS,
    TIME_BUDGET_PRESETS,
    get_all_delays_for_preset,
)

router = APIRouter(prefix="/system", tags=["system"])


def _get_setting(db: Session, key: str, default=None):
    row = db.query(Setting).get(key)
    return row.value if row else default


def _set_setting(db: Session, key: str, value):
    row = db.query(Setting).get(key)
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
        db.add(row)
    db.commit()


@router.get("/speed")
async def speed_page(request: Request, db: Session = Depends(get_db)):
    current_preset = _get_setting(db, "throttle_preset", "slow")
    custom_delays = _get_setting(db, "throttle_custom_delays", None)

    delays = get_all_delays_for_preset(current_preset)
    delays_by_preset = {
        p: {k: [v[0], v[1]] for k, v in get_all_delays_for_preset(p).items()}
        for p in PRESET_MULTIPLIERS
    }

    tz_saved = _get_setting(db, "sequence_timezone", None)
    tz_effective = normalize_sequence_tz_name(raw_sequence_timezone_name(db))

    templates = request.app.state.templates
    return templates.TemplateResponse("system/speed.html", {
        "request": request,
        "user": request.session.get("user"),
        "current_preset": current_preset,
        "preset_labels": PRESET_LABELS,
        "delays": delays,
        "delays_by_preset": delays_by_preset,
        "reference_delays": REFERENCE_DELAYS,
        "min_floors": MIN_FLOORS,
        "custom_delays": custom_delays,
        "time_budget_presets": TIME_BUDGET_PRESETS,
        "page_id": "system_speed",
        "sequence_timezone_saved": tz_saved if isinstance(tz_saved, str) else "",
        "sequence_timezone_effective": tz_effective,
        "common_timezones": COMMON_TIMEZONES,
    })


@router.post("/speed/save")
async def speed_save(
    request: Request,
    db: Session = Depends(get_db),
    preset: str = Form("slow"),
):
    if preset not in PRESET_MULTIPLIERS:
        preset = "slow"
    _set_setting(db, "throttle_preset", preset)
    return RedirectResponse("/system/speed", status_code=303)


@router.post("/speed/sequence-timezone")
async def speed_sequence_timezone_save(
    request: Request,
    db: Session = Depends(get_db),
    sequence_timezone: str = Form(""),
):
    from zoneinfo import ZoneInfo

    raw = normalize_sequence_tz_name((sequence_timezone or "").strip())
    if not raw:
        row = db.query(Setting).get("sequence_timezone")
        if row:
            db.delete(row)
            db.commit()
        return RedirectResponse("/system/speed?tz=reset", status_code=303)
    try:
        ZoneInfo(raw)
    except Exception:
        return RedirectResponse("/system/speed?tz=bad", status_code=303)
    _set_setting(db, "sequence_timezone", raw)
    return RedirectResponse("/system/speed?tz=saved", status_code=303)


@router.post("/speed/messenger-poll")
async def speed_messenger_poll_save(
    request: Request,
    db: Session = Depends(get_db),
    messenger_auto_inbox_sync_interval_sec: str = Form("300"),
):
    """Один интервал фоновой синхронизации мессенджера; вкл/выкл — в разделе «Мессенджер»."""
    try:
        inbox_iv = int((messenger_auto_inbox_sync_interval_sec or "300").strip())
    except ValueError:
        inbox_iv = 300
    set_messenger_auto_inbox_sync_interval_sec(db, inbox_iv)
    set_messenger_poll_interval_sec(db, inbox_iv)
    return RedirectResponse("/system/speed?messenger_poll=saved", status_code=303)


@router.post("/speed/reset")
async def speed_reset(request: Request, db: Session = Depends(get_db)):
    _set_setting(db, "throttle_preset", "slow")
    row = db.query(Setting).get("throttle_custom_delays")
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse("/system/speed", status_code=303)


@router.get("/settings")
async def system_settings_page(request: Request, db: Session = Depends(get_db)):
    templates = request.app.state.templates
    ctx = build_settings_page_context(request, db)
    ctx["request"] = request
    ctx["user"] = request.session.get("user")
    ctx["page_id"] = "system_settings"
    ctx["saved"] = request.query_params.get("saved") == "1"
    return templates.TemplateResponse("system/settings.html", ctx)


@router.post("/settings/save")
async def system_settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_partial = form.get("save_partial")
    sp = str(raw_partial).strip() if raw_partial is not None else ""
    only = sp if sp in ("openai", "google_llm", "clear_openai", "clear_google_llm") else None
    save_settings_from_form(db, form, only=only, request=request)
    return RedirectResponse("/system/settings?saved=1", status_code=303)


ACTION_TYPE_LABELS = {
    "navigate_profile": "Переход на профиль",
    "scroll_page": "Прокрутка страницы",
    "like_post": "Лайк поста",
    "like_video": "Лайк видео",
    "comment_post": "Комментарий к посту",
    "comment_video": "Комментарий к видео",
    "like_and_comment": "Лайк + комментарий",
    "like_comment_friend_request": "Лайк + комментарий + заявка в друзья",
    "like_comment_friend_request_send_dm": "Лайк + комментарий + заявка в друзья + личное сообщение",
    "friend_request": "Заявка в друзья",
    "open_dm": "Открытие диалога",
    "send_dm": "Отправка сообщения",
    "next_person": "Переход к следующему",
    "group_action": "Действие в группе",
    "warmup_like": "Прогрев: лайк",
    "warmup_comment": "Прогрев: комментарий",
    "sequence_like_post": "Сценарий: лайк",
    "sequence_comment_post": "Сценарий: комментарий",
    "sequence_like_and_comment": "Сценарий: лайк и комментарий",
    "sequence_like_comment_friend_request": "Сценарий: лайк + комментарий + заявка в друзья",
    "sequence_like_comment_friend_request_send_dm": "Сценарий: лайк + комментарий + заявка в друзья + личное сообщение",
    "sequence_friend_request": "Сценарий: заявка в друзья",
    "sequence_send_dm": "Сценарий: сообщение",
    "sequence_open_dm": "Сценарий: диалог",
    "outreach_dm": "Рассылка: сообщение",
}

JOB_TYPE_LABELS = {
    "donor_friends_parse": "Парсер доноров",
    "discovery": "Автопоиск аудитории",
    "messenger_sync": "Мессенджер",
    "outreach": "Рассылка",
    "sequence": "Сценарий",
    "warmup": "Прогрев",
}

JOB_STATUS_LABELS = {
    "queued": "В очереди",
    "running": "В работе",
    "success": "Успешно",
    "failed": "Ошибка",
    "cancelled": "Отменено",
}

LOG_DAYS_OPTIONS: list[tuple[str, str]] = [
    ("1", "24 часа"),
    ("3", "3 дня"),
    ("7", "7 дней"),
    ("30", "30 дней"),
    ("90", "90 дней"),
    ("all", "За всё время"),
]

LOG_STATUS_OPTIONS: list[tuple[str, str]] = [
    ("", "Все статусы"),
    ("queued", "В очереди"),
    ("running", "В работе"),
    ("success", "Успешно"),
    ("failed", "Ошибка"),
    ("cancelled", "Отменено"),
]

LOG_SEVERITY_OPTIONS: list[tuple[str, str]] = [
    ("problem", "Только проблемы"),
    ("error", "Только error"),
    ("warning", "Только warning"),
    ("info", "Только info"),
    ("all", "Все события"),
]


def _normalize_logs_tab(raw: str | None) -> str:
    tab = (raw or "jobs").strip().lower()
    if tab not in ("jobs", "errors", "reports"):
        return "jobs"
    return tab


def _normalize_logs_days(raw: str | None) -> str:
    val = (raw or "7").strip().lower()
    allowed = {k for k, _ in LOG_DAYS_OPTIONS}
    return val if val in allowed else "7"


def _scenario_id_int(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if s.isdigit() and int(s) > 0:
        return int(s)
    return None


def _logs_since(days: str) -> datetime | None:
    if days == "all":
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 7
    return datetime.now(timezone.utc) - timedelta(days=max(1, n))


def _logs_query_string(
    *,
    tab: str,
    q: str,
    days: str,
    job_type: str,
    status: str,
    severity: str,
    scenario_id: str = "",
) -> str:
    params: dict[str, str] = {"tab": _normalize_logs_tab(tab)}
    if (q or "").strip():
        params["q"] = q.strip()
    if days and days != "7":
        params["days"] = days
    if (job_type or "").strip():
        params["job_type"] = job_type.strip()
    if (status or "").strip():
        params["status"] = status.strip()
    if severity and severity != "problem":
        params["severity"] = severity
    sid = (scenario_id or "").strip()
    if sid.isdigit() and int(sid) > 0:
        params["scenario_id"] = sid
    return "?" + urlencode(params) if params else ""


def _job_type_label(job_type: str | None) -> str:
    jt = (job_type or "").strip()
    return JOB_TYPE_LABELS.get(jt, jt or "—")


def _status_badge_class(status: str | None) -> str:
    st = (status or "").strip().lower()
    if st == "success":
        return "badge-success"
    if st == "failed":
        return "badge-error"
    if st == "running":
        return "badge-warning"
    return "badge-default"


def _severity_badge_class(severity: str | None) -> str:
    sev = (severity or "").strip().lower()
    if sev == "error":
        return "badge-error"
    if sev == "warning":
        return "badge-warning"
    if sev == "info":
        return "badge-success"
    return "badge-default"


def _job_duration_sec(job: Job) -> int | None:
    start = job.started_at or job.created_at
    end = job.ended_at
    if not start:
        return None
    if not end and (job.status or "").strip().lower() == "running":
        end = datetime.now(timezone.utc)
    if not end:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds()))


def _json_dump(value) -> str:
    if value in (None, "", [], {}):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return str(value)


def _apply_job_filters(
    query,
    *,
    since: datetime | None,
    job_type: str,
    status: str,
    q: str,
    scenario_id: int | None = None,
):
    if since is not None:
        query = query.filter(
            or_(
                Job.created_at >= since,
                Job.started_at >= since,
                Job.ended_at >= since,
            )
        )
    if (job_type or "").strip():
        query = query.filter(Job.job_type == job_type.strip())
    if (status or "").strip():
        query = query.filter(Job.status == status.strip())
    if scenario_id is not None:
        query = query.filter(
            cast(func.json_extract(Job.config_snapshot, "$.campaign_id"), Integer) == scenario_id
        )
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        conds = [
            Job.job_type.ilike(like),
            Job.correlation_id.ilike(like),
            Job.error_summary.ilike(like),
        ]
        if needle.isdigit():
            conds.append(Job.id == int(needle))
        query = query.filter(or_(*conds))
    return query


def _apply_problem_event_filters(
    query,
    *,
    since: datetime | None,
    job_type: str,
    status: str,
    severity: str,
    q: str,
    scenario_id: int | None = None,
):
    if since is not None:
        query = query.filter(JobEvent.ts >= since)
    if (job_type or "").strip():
        query = query.filter(Job.job_type == job_type.strip())
    if (status or "").strip():
        query = query.filter(Job.status == status.strip())
    if scenario_id is not None:
        query = query.filter(
            cast(func.json_extract(Job.config_snapshot, "$.campaign_id"), Integer) == scenario_id
        )

    sev = (severity or "problem").strip().lower()
    if sev == "problem":
        query = query.filter(
            or_(JobEvent.severity.in_(("warning", "error")), JobEvent.outcome == "fail")
        )
    elif sev in ("info", "warning", "error"):
        query = query.filter(JobEvent.severity == sev)

    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        conds = [
            Job.job_type.ilike(like),
            Job.error_summary.ilike(like),
            JobEvent.event_type.ilike(like),
            JobEvent.outcome.ilike(like),
        ]
        if needle.isdigit():
            n = int(needle)
            conds.extend([Job.id == n, JobEvent.person_id == n, JobEvent.fb_account_id == n])
        query = query.filter(or_(*conds))
    return query


def _load_logs_filters(request: Request) -> dict[str, str]:
    return {
        "tab": _normalize_logs_tab(request.query_params.get("tab")),
        "q": (request.query_params.get("q") or "").strip(),
        "days": _normalize_logs_days(request.query_params.get("days")),
        "job_type": (request.query_params.get("job_type") or "").strip(),
        "status": (request.query_params.get("status") or "").strip(),
        "severity": (request.query_params.get("severity") or "problem").strip().lower() or "problem",
        "scenario_id": (request.query_params.get("scenario_id") or "").strip(),
    }


@router.get("/logs")
async def system_logs_page(request: Request, db: Session = Depends(get_db)):
    filters = _load_logs_filters(request)
    since = _logs_since(filters["days"])
    scenario_id_filter = _scenario_id_int(filters["scenario_id"])
    scenario_filter_name: str | None = None
    if scenario_id_filter is not None:
        sc_row = db.get(SequenceCampaign, scenario_id_filter)
        label = ((sc_row.name or "").strip() if sc_row else "") or f"#{scenario_id_filter}"
        scenario_filter_name = label

    distinct_job_types = [r[0] for r in db.query(Job.job_type).distinct().order_by(Job.job_type.asc()).all() if r[0]]
    job_type_options = [(jt, _job_type_label(jt)) for jt in distinct_job_types]

    jobs_q = _apply_job_filters(
        db.query(Job),
        since=since,
        job_type=filters["job_type"],
        status=filters["status"],
        q=filters["q"],
        scenario_id=scenario_id_filter,
    )
    jobs_total = jobs_q.count()
    jobs = jobs_q.order_by(Job.created_at.desc()).limit(100).all()

    failed_jobs_q = _apply_job_filters(
        db.query(Job).filter(Job.status == "failed"),
        since=since,
        job_type=filters["job_type"],
        status=filters["status"],
        q=filters["q"],
        scenario_id=scenario_id_filter,
    )
    failed_jobs_total = failed_jobs_q.count()
    failed_jobs = failed_jobs_q.order_by(Job.created_at.desc()).limit(60).all()

    problem_events_q = _apply_problem_event_filters(
        db.query(JobEvent, Job).join(Job, Job.id == JobEvent.job_id),
        since=since,
        job_type=filters["job_type"],
        status=filters["status"],
        severity=filters["severity"],
        q=filters["q"],
        scenario_id=scenario_id_filter,
    )
    problem_events_total = problem_events_q.count()
    problem_events = problem_events_q.order_by(JobEvent.ts.desc()).limit(120).all()

    job_ids = {job.id for job in jobs}
    job_ids.update(job.id for job in failed_jobs)
    job_ids.update(job.id for _, job in problem_events)
    job_event_counts: dict[int, int] = {}
    if job_ids:
        job_event_counts = {
            int(job_id): int(cnt)
            for job_id, cnt in (
                db.query(JobEvent.job_id, func.count(JobEvent.id))
                .filter(JobEvent.job_id.in_(job_ids))
                .group_by(JobEvent.job_id)
                .all()
            )
        }
    job_durations = {job_id: _job_duration_sec(job) for job_id, job in {j.id: j for j in [*jobs, *failed_jobs]}.items()}
    for _evt, job in problem_events:
        job_durations.setdefault(job.id, _job_duration_sec(job))

    person_ids = {
        evt.person_id
        for evt, _job in problem_events
        if evt.person_id is not None
    }
    person_map = {}
    if person_ids:
        person_map = {
            p.id: {
                "name": (p.display_name or p.canonical_url or f"#{p.id}"),
                "url": p.canonical_url or "",
            }
            for p in db.query(Person).filter(Person.id.in_(person_ids)).all()
        }

    account_map = {
        acc.id: acc.label or f"Аккаунт #{acc.id}"
        for acc in db.query(FBAccount).order_by(FBAccount.id.asc()).all()
    }

    summary = {
        "total_jobs": int(db.query(func.count(Job.id)).scalar() or 0),
        "running_jobs": int(db.query(func.count(Job.id)).filter(Job.status == "running").scalar() or 0),
        "failed_jobs": int(db.query(func.count(Job.id)).filter(Job.status == "failed").scalar() or 0),
        "problem_events": int(
            db.query(func.count(JobEvent.id))
            .filter(or_(JobEvent.severity.in_(("warning", "error")), JobEvent.outcome == "fail"))
            .scalar()
            or 0
        ),
        "last_job_at": db.query(func.max(Job.created_at)).scalar(),
        "last_event_at": db.query(func.max(JobEvent.ts)).scalar(),
    }
    summary["last_activity_at"] = max(
        [x for x in (summary["last_job_at"], summary["last_event_at"]) if x is not None],
        default=None,
    )

    recent_batches = db.query(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(12).all()
    donor_name_map = {
        donor.id: donor.name or f"Донор #{donor.id}"
        for donor in db.query(Donor).all()
    }

    _jobs_for_journal = list(
        {
            j.id: j
            for j in (
                *jobs,
                *failed_jobs,
                *[jb for _evt, jb in problem_events],
            )
        }.values()
    )
    _jl_ids = collect_journal_lookup_ids(_jobs_for_journal)
    conv_peer_names: dict[int, str] = {}
    if _jl_ids["conversation"]:
        for row in db.query(Conversation).filter(Conversation.id.in_(_jl_ids["conversation"])).all():
            conv_peer_names[row.id] = (row.peer_name or "Диалог").strip() or f"Чат #{row.id}"
    outreach_names: dict[int, str] = {}
    if _jl_ids["outreach"]:
        for row in db.query(OutreachCampaign).filter(OutreachCampaign.id.in_(_jl_ids["outreach"])).all():
            outreach_names[row.id] = (row.name or f"Кампания #{row.id}").strip()
    sequence_names: dict[int, str] = {}
    if _jl_ids["sequence"]:
        for row in db.query(SequenceCampaign).filter(SequenceCampaign.id.in_(_jl_ids["sequence"])).all():
            sequence_names[row.id] = (row.name or f"Сценарий #{row.id}").strip()
    warmup_names: dict[int, str] = {}
    if _jl_ids["warmup"]:
        for row in db.query(WarmupCampaign).filter(WarmupCampaign.id.in_(_jl_ids["warmup"])).all():
            warmup_names[row.id] = (row.name or f"Прогрев #{row.id}").strip()
    job_journal_context = build_journal_context_by_job_id(
        _jobs_for_journal,
        conv_peer_names=conv_peer_names,
        outreach_names=outreach_names,
        sequence_names=sequence_names,
        warmup_names=warmup_names,
        donor_name_map=donor_name_map,
        account_map=account_map,
    )

    donor_people_counts = {
        int(donor_id): int(cnt)
        for donor_id, cnt in (
            db.query(Person.donor_id, func.count(Person.id))
            .filter(Person.donor_id.isnot(None))
            .group_by(Person.donor_id)
            .all()
        )
    }
    recent_donor_reports = [
        donor for donor in db.query(Donor).order_by(Donor.id.desc()).limit(12).all()
        if donor_people_counts.get(donor.id, 0) > 0
    ]

    tab_urls = {
        tab: "/system/logs"
        + _logs_query_string(
            tab=tab,
            q=filters["q"],
            days=filters["days"],
            job_type=filters["job_type"],
            status=filters["status"],
            severity=filters["severity"],
            scenario_id=filters["scenario_id"],
        )
        for tab in ("jobs", "errors", "reports")
    }

    jobs_export_url = "/system/logs/jobs.csv" + _logs_query_string(
        tab=filters["tab"],
        q=filters["q"],
        days=filters["days"],
        job_type=filters["job_type"],
        status=filters["status"],
        severity=filters["severity"],
        scenario_id=filters["scenario_id"],
    )
    errors_export_url = "/system/logs/errors.csv" + _logs_query_string(
        tab=filters["tab"],
        q=filters["q"],
        days=filters["days"],
        job_type=filters["job_type"],
        status=filters["status"],
        severity=filters["severity"],
        scenario_id=filters["scenario_id"],
    )

    templates = request.app.state.templates
    return templates.TemplateResponse("system/logs.html", {
        "request": request,
        "user": request.session.get("user"),
        "page_id": "system_logs",
        "active_tab": filters["tab"],
        "search_q": filters["q"],
        "days": filters["days"],
        "job_type": filters["job_type"],
        "status": filters["status"],
        "severity": filters["severity"],
        "scenario_id": filters["scenario_id"],
        "scenario_filter_name": scenario_filter_name,
        "days_options": LOG_DAYS_OPTIONS,
        "status_options": LOG_STATUS_OPTIONS,
        "severity_options": LOG_SEVERITY_OPTIONS,
        "job_type_options": job_type_options,
        "tab_urls": tab_urls,
        "jobs": jobs,
        "jobs_total": jobs_total,
        "failed_jobs": failed_jobs,
        "failed_jobs_total": failed_jobs_total,
        "problem_events": problem_events,
        "problem_events_total": problem_events_total,
        "job_event_counts": job_event_counts,
        "job_durations": job_durations,
        "person_map": person_map,
        "account_map": account_map,
        "summary": summary,
        "job_status_labels": JOB_STATUS_LABELS,
        "job_type_labels": JOB_TYPE_LABELS,
        "jobs_export_url": jobs_export_url,
        "errors_export_url": errors_export_url,
        "recent_batches": recent_batches,
        "recent_donor_reports": recent_donor_reports,
        "donor_name_map": donor_name_map,
        "donor_people_counts": donor_people_counts,
        "job_journal_context": job_journal_context,
        "job_event_type_label": label_job_event_type,
    })


@router.get("/logs/jobs.csv")
async def system_logs_jobs_csv(request: Request, db: Session = Depends(get_db)):
    filters = _load_logs_filters(request)
    since = _logs_since(filters["days"])
    scenario_id_filter = _scenario_id_int(filters["scenario_id"])
    jobs = (
        _apply_job_filters(
            db.query(Job),
            since=since,
            job_type=filters["job_type"],
            status=filters["status"],
            q=filters["q"],
            scenario_id=scenario_id_filter,
        )
        .order_by(Job.created_at.desc())
        .all()
    )
    event_counts = {
        int(job_id): int(cnt)
        for job_id, cnt in (
            db.query(JobEvent.job_id, func.count(JobEvent.id))
            .group_by(JobEvent.job_id)
            .all()
        )
    }

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID задачи",
        "Тип",
        "Тип (название)",
        "Статус",
        "Создано",
        "Старт",
        "Финиш",
        "Длительность (сек)",
        "Correlation ID",
        "Событий",
        "Ошибка",
        "Config JSON",
        "Progress JSON",
    ])
    for job in jobs:
        writer.writerow([
            job.id,
            job.job_type or "",
            _job_type_label(job.job_type),
            JOB_STATUS_LABELS.get(job.status or "", job.status or ""),
            job.created_at.isoformat() if job.created_at else "",
            job.started_at.isoformat() if job.started_at else "",
            job.ended_at.isoformat() if job.ended_at else "",
            _job_duration_sec(job) or "",
            job.correlation_id or "",
            event_counts.get(job.id, 0),
            (job.error_summary or "").replace("\r\n", "\n").replace("\n", " | "),
            _json_dump(job.config_snapshot),
            _json_dump(job.progress_json),
        ])

    data = buf.getvalue().encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    cd = attachment_content_disposition(
        f"Журнал_задач_{ts}.csv",
        ascii_filename=f"system_jobs_{ts}.csv",
    )
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": cd},
    )


@router.get("/logs/errors.csv")
async def system_logs_errors_csv(request: Request, db: Session = Depends(get_db)):
    filters = _load_logs_filters(request)
    since = _logs_since(filters["days"])
    scenario_id_filter = _scenario_id_int(filters["scenario_id"])

    failed_jobs = (
        _apply_job_filters(
            db.query(Job).filter(Job.status == "failed"),
            since=since,
            job_type=filters["job_type"],
            status=filters["status"],
            q=filters["q"],
            scenario_id=scenario_id_filter,
        )
        .order_by(Job.created_at.desc())
        .all()
    )
    problem_events = (
        _apply_problem_event_filters(
            db.query(JobEvent, Job).join(Job, Job.id == JobEvent.job_id),
            since=since,
            job_type=filters["job_type"],
            status=filters["status"],
            severity=filters["severity"],
            q=filters["q"],
            scenario_id=scenario_id_filter,
        )
        .order_by(JobEvent.ts.desc())
        .all()
    )

    account_map = {
        acc.id: acc.label or f"Аккаунт #{acc.id}"
        for acc in db.query(FBAccount).order_by(FBAccount.id.asc()).all()
    }
    person_ids = {
        evt.person_id
        for evt, _job in problem_events
        if evt.person_id is not None
    }
    person_map = {
        p.id: (p.display_name or p.canonical_url or f"#{p.id}")
        for p in db.query(Person).filter(Person.id.in_(person_ids)).all()
    } if person_ids else {}

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Источник",
        "Job ID",
        "Тип задачи",
        "Статус задачи",
        "Severity",
        "Event type",
        "Outcome",
        "Person ID",
        "Контакт",
        "FB account ID",
        "Аккаунт",
        "Создано",
        "Финиш",
        "Event ts",
        "Длительность (сек)",
        "Сообщение",
        "Payload JSON",
        "Correlation ID",
    ])
    for job in failed_jobs:
        writer.writerow([
            "job",
            job.id,
            _job_type_label(job.job_type),
            JOB_STATUS_LABELS.get(job.status or "", job.status or ""),
            "error",
            "",
            "fail",
            "",
            "",
            "",
            "",
            job.created_at.isoformat() if job.created_at else "",
            job.ended_at.isoformat() if job.ended_at else "",
            "",
            _job_duration_sec(job) or "",
            (job.error_summary or "").replace("\r\n", "\n").replace("\n", " | "),
            "",
            job.correlation_id or "",
        ])
    for evt, job in problem_events:
        writer.writerow([
            "event",
            job.id,
            _job_type_label(job.job_type),
            JOB_STATUS_LABELS.get(job.status or "", job.status or ""),
            evt.severity or "",
            evt.event_type or "",
            evt.outcome or "",
            evt.person_id or "",
            person_map.get(evt.person_id, ""),
            evt.fb_account_id or "",
            account_map.get(evt.fb_account_id, "") if evt.fb_account_id else "",
            job.created_at.isoformat() if job.created_at else "",
            job.ended_at.isoformat() if job.ended_at else "",
            evt.ts.isoformat() if evt.ts else "",
            (evt.duration_ms // 1000) if evt.duration_ms else "",
            (job.error_summary or "").replace("\r\n", "\n").replace("\n", " | "),
            _json_dump(evt.payload),
            job.correlation_id or "",
        ])

    data = buf.getvalue().encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    cd = attachment_content_disposition(
        f"Ошибки_и_события_{ts}.csv",
        ascii_filename=f"system_errors_{ts}.csv",
    )
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": cd},
    )
