"""Фоновая сессия «натурального» прогрева аккаунта (один заход Playwright на тик)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import FBAccount, Job
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.job_logging import finish_job, log_job_event, start_job
from backend.services.natural_warmup_actions import NaturalWarmupPhase, run_natural_warmup_session
from backend.services.natural_warmup_llm import natural_warmup_commenting_llm_configured
from backend.services.natural_warmup_presets import get_preset
from backend.services.natural_warmup_time import natural_warmup_local_date_iso
from backend.services.playwright_resource import playwright_run_slot
from backend.services.warmup_actions import parse_storage_state
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral

logger = logging.getLogger(__name__)

JOB_TYPE = "natural_warmup"


def _parse_iso_utc(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _finalize_if_period_ended(db: Session, acc: FBAccount, state: dict[str, Any]) -> bool:
    """True если прогрев завершён и статус обновлён на ready."""
    ends = _parse_iso_utc(str(state.get("ends_at") or ""))
    if not ends:
        return False
    now = datetime.now(timezone.utc)
    if now < ends:
        return False
    acc.readiness_status = "ready"
    acc.natural_warmup_json = None
    db.commit()
    return True


def _bump_tick_state(db: Session, state: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    dk = natural_warmup_local_date_iso(db, now_utc=now)
    out = dict(state)
    out["last_tick_at"] = now.isoformat()
    out["ticks_total"] = int(out.get("ticks_total") or 0) + 1
    if out.get("day_key") != dk:
        out["day_key"] = dk
        out["ticks_calendar_day"] = 1
    else:
        out["ticks_calendar_day"] = int(out.get("ticks_calendar_day") or 0) + 1
    return out


def _bump_comment_count(db: Session, state: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    dk = natural_warmup_local_date_iso(db, now_utc=now)
    out = dict(state)
    if out.get("comments_day_key") != dk:
        out["comments_day_key"] = dk
        out["comments_today"] = 0
    out["comments_today"] = int(out.get("comments_today") or 0) + 1
    out["last_comment_at"] = now.isoformat()
    return out


def _bump_friend_count(db: Session, state: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    dk = natural_warmup_local_date_iso(db, now_utc=now)
    out = dict(state)
    if out.get("friends_day_key") != dk:
        out["friends_day_key"] = dk
        out["friends_today"] = 0
    out["friends_today"] = int(out.get("friends_today") or 0) + 1
    out["last_friend_at"] = now.isoformat()
    return out


def process_natural_warmup_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job or job.job_type != JOB_TYPE:
            logger.error("natural_warmup job %s missing", job_id)
            return
        snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
        raw_aid = snap.get("fb_account_id")
        phase_raw = (snap.get("phase") or "groups").strip().lower()
        if phase_raw not in ("groups", "profiles", "likes", "ai_comment", "add_friend", "browse"):
            phase_raw = "groups"
        phase: NaturalWarmupPhase = phase_raw  # type: ignore[assignment]
        if raw_aid is None or not str(raw_aid).strip().isdigit():
            finish_job(db, job, status="failed", error="Нет fb_account_id")
            db.commit()
            return
        aid = int(raw_aid)

        if job.status == "queued":
            start_job(db, job)

        acc = db.get(FBAccount, aid)
        if not acc or acc.organization_id != job.organization_id:
            finish_job(db, job, status="failed", error="Аккаунт не найден")
            db.commit()
            return
        if acc.readiness_status != "warming":
            finish_job(db, job, status="cancelled", error="Прогрев остановлен в интерфейсе")
            db.commit()
            return
        if not account_in_active_slot(acc):
            finish_job(
                db,
                job,
                status="failed",
                error="Назначьте аккаунту слот 1–3 в «Аккаунты»",
            )
            db.commit()
            return

        state = acc.natural_warmup_json if isinstance(acc.natural_warmup_json, dict) else {}
        if _finalize_if_period_ended(db, acc, state):
            finish_job(db, job, status="cancelled", error="Срок прогрева уже завершён — статус «Готов»")
            db.commit()
            return

        if phase == "ai_comment" and not natural_warmup_commenting_llm_configured(db):
            log_job_event(
                db,
                job_id=job_id,
                event_type="natural_warmup_ai_comment_skipped_no_llm",
                severity="info",
                fb_account_id=aid,
                outcome="skip",
                payload={
                    "message": "Нейрокомментарий пропущен: в настройках нет ключа OpenAI или Google AI.",
                },
            )
            db.commit()
            finish_job(db, job, status="success", error=None)
            db.commit()
            return

        preset = get_preset(int(state.get("duration_days") or 3))
        sc = float(preset.session_scale)

        def _emit(event_type: str, payload: dict) -> None:
            log_job_event(
                db,
                job_id=job_id,
                event_type=event_type,
                severity="info",
                fb_account_id=aid,
                outcome="ok",
                payload=payload,
            )

        storage = parse_storage_state(acc.session_state_json)
        ok = False
        detail = ""
        metrics: dict[str, int] = {}
        t0 = time.perf_counter()
        try:
            with sync_playwright() as p:
                with playwright_run_slot(fb_account_id=acc.id):
                    with fb_account_playwright_profile_ephemeral(
                        p,
                        acc,
                        storage_state=storage,
                        db=db,
                    ) as ctx:
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                        if phase == "ai_comment":
                            lo, hi = int(1400 * sc), int(3200 * sc)
                        elif phase == "add_friend":
                            lo, hi = int(900 * sc), int(1800 * sc)
                        elif phase == "browse":
                            # Минимум 15 минут (900с), максимум 30 минут (1800с)
                            lo, hi = int(900 * sc), int(1800 * sc)
                        else:
                            # groups / profiles / likes: минимум 15 минут
                            lo, hi = int(900 * sc), int(1800 * sc)
                        ok, detail, metrics = run_natural_warmup_session(
                            page,
                            db,
                            phase,
                            session_seconds_min=lo,
                            session_seconds_max=hi,
                            groups_scroll_rounds_cap=preset.groups_scroll_rounds_cap,
                            emit=_emit,
                        )
        except Exception as e:
            logger.exception("natural_warmup job %s account %s", job_id, aid)
            detail = str(e)[:900]
            ok = False
            metrics = {}

        ms = int((time.perf_counter() - t0) * 1000)
        db.refresh(acc)
        if acc.readiness_status != "warming":
            finish_job(db, job, status="cancelled", error="Прогрев остановлен во время сессии")
            db.commit()
            return

        st = acc.natural_warmup_json if isinstance(acc.natural_warmup_json, dict) else {}
        if ok:
            st = _bump_tick_state(db, st)
            totals = st.setdefault("activity_totals", {})
            if isinstance(totals, dict):
                for k, v in (metrics or {}).items():
                    ks = str(k)
                    totals[ks] = int(totals.get(ks) or 0) + int(v)
            if phase == "ai_comment" and "submitted:" in (detail or ""):
                st = _bump_comment_count(db, st)
            if phase == "add_friend" and (detail or "").startswith("friend_ok:"):
                st = _bump_friend_count(db, st)
            acc.natural_warmup_json = st
            db.commit()
            log_job_event(
                db,
                job_id=job_id,
                event_type="natural_warmup_session_ok",
                severity="info",
                fb_account_id=aid,
                outcome="ok",
                duration_ms=ms,
                payload={
                    "phase": phase,
                    "detail": detail[:400],
                    "metrics": metrics,
                    "preset_days": preset.days,
                },
            )
            db.commit()
            db.refresh(acc)
            st2 = acc.natural_warmup_json if isinstance(acc.natural_warmup_json, dict) else {}
            if _finalize_if_period_ended(db, acc, st2):
                log_job_event(
                    db,
                    job_id=job_id,
                    event_type="natural_warmup_completed",
                    severity="info",
                    fb_account_id=aid,
                    outcome="ok",
                    payload={"message": "Период завершён, статус «Готов к работе»"},
                )
                db.commit()
            finish_job(db, job, status="success")
        else:
            log_job_event(
                db,
                job_id=job_id,
                event_type="natural_warmup_session_fail",
                severity="warning",
                fb_account_id=aid,
                outcome="fail",
                duration_ms=ms,
                payload={"phase": phase, "detail": detail[:400]},
            )
            db.commit()
            finish_job(db, job, status="failed", error=detail[:2000])
        db.commit()
    finally:
        db.close()


def spawn_natural_warmup_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_natural_warmup_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_natural_warmup_job,
        args=(job_id,),
        daemon=True,
        name=f"natural-warmup-{job_id}",
    )
    t.start()
