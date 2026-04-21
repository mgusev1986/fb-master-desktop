"""Периодический запуск сессий натурального прогрева (1–7 дней, максимальные паузы)."""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone

from backend.database import SessionLocal
from backend.models import FBAccount, Job
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.job_logging import create_job
from backend.services.proxy_health_guard import account_proxy_tunnel_blocked
from backend.services.natural_warmup_llm import natural_warmup_commenting_llm_configured
from backend.services.natural_warmup_presets import NaturalWarmupPreset, get_preset
from backend.services.natural_warmup_time import (
    is_natural_warmup_active_local_hours,
    natural_warmup_local_date_iso,
)
from backend.services.natural_warmup_worker import JOB_TYPE, spawn_natural_warmup_worker

logger = logging.getLogger(__name__)


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


def phase_for_timeline(
    started_at: datetime,
    ends_at: datetime,
    now: datetime,
    preset: NaturalWarmupPreset,
) -> str | None:
    """«Прогулочная» фаза по доле срока: группы → профили → лайки (границы из пресета)."""
    if now >= ends_at:
        return None
    span = (ends_at - started_at).total_seconds()
    if span <= 0:
        return "likes"
    r = (now - started_at).total_seconds() / span
    g = float(preset.timeline_groups_until)
    p2 = float(preset.timeline_profiles_until)
    if r < g:
        return "groups"
    if r < p2:
        return "profiles"
    return "likes"


def _pick_phase(
    *,
    browse: str | None,
    progress: float,
    comments_today: int,
    friends_today: int,
    last_comment_at: datetime | None,
    last_friend_at: datetime | None,
    now: datetime,
    preset: NaturalWarmupPreset,
    allow_ai_comment: bool,
) -> str | None:
    if browse is None:
        return None
    can_comment = (
        allow_ai_comment
        and progress >= preset.comment_after_progress
        and comments_today < preset.max_comments_per_day
        and (
            last_comment_at is None
            or (now - last_comment_at).total_seconds() >= preset.min_seconds_between_comments
        )
    )
    can_friend = (
        progress >= preset.friend_after_progress
        and friends_today < preset.max_friends_per_day
        and (
            last_friend_at is None
            or (now - last_friend_at).total_seconds() >= preset.min_seconds_between_friends
        )
    )
    options: list[tuple[str, float]] = [(browse, float(preset.weight_browse))]
    # Фаза «browse» (глубокая прогулка) доступна всегда
    options.append(("browse", float(preset.weight_browse_deep)))
    if can_comment:
        options.append(("ai_comment", float(preset.weight_ai_comment)))
    if can_friend:
        options.append(("add_friend", float(preset.weight_add_friend)))
    total = sum(w for _, w in options)
    r = random.uniform(0, total)
    acc = 0.0
    for ph, w in options:
        acc += w
        if r <= acc:
            return ph
    return browse


def try_natural_warmup_scheduler_tick() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        occupied: set[int] = set()
        for j in (
            db.query(Job)
            .filter(
                Job.job_type == JOB_TYPE,
                Job.status.in_(("queued", "running")),
            )
            .all()
        ):
            snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
            raw = snap.get("fb_account_id")
            if raw is not None and str(raw).strip().isdigit():
                occupied.add(int(raw))

        accs = db.query(FBAccount).filter(FBAccount.readiness_status == "warming").all()
        llm_for_comments = natural_warmup_commenting_llm_configured(db)
        for acc in accs:
            if acc.id in occupied:
                continue
            st = acc.natural_warmup_json
            if not isinstance(st, dict):
                continue
            started = _parse_iso_utc(str(st.get("started_at") or ""))
            ends = _parse_iso_utc(str(st.get("ends_at") or ""))
            if not started or not ends:
                continue
            preset = get_preset(int(st.get("duration_days") or 3))
            if not is_natural_warmup_active_local_hours(db, now_utc=now):
                continue
            if now >= ends:
                acc.readiness_status = "ready"
                acc.natural_warmup_json = None
                db.commit()
                logger.info("natural_warmup: account %s period ended → ready", acc.id)
                continue
            if not account_in_active_slot(acc):
                continue
            if account_proxy_tunnel_blocked(acc):
                continue
            busy_msg = account_automation_conflict_message(
                db,
                [acc.id],
                ignore_natural_warmup_account_id=acc.id,
            )
            if busy_msg:
                logger.info(
                    "natural_warmup: skip account %s because %s",
                    acc.id,
                    busy_msg,
                )
                continue
            last = _parse_iso_utc(str(st.get("last_tick_at") or ""))
            if last and (now - last).total_seconds() < preset.min_seconds_between_ticks:
                continue
            dk = natural_warmup_local_date_iso(db, now_utc=now)
            if st.get("day_key") == dk and int(st.get("ticks_calendar_day") or 0) >= preset.max_ticks_per_calendar_day:
                continue

            browse = phase_for_timeline(started, ends, now, preset)
            span = (ends - started).total_seconds()
            progress = (now - started).total_seconds() / span if span > 0 else 1.0

            cdk = str(st.get("comments_day_key") or "")
            comments_today = int(st.get("comments_today") or 0) if cdk == dk else 0
            fdk = str(st.get("friends_day_key") or "")
            friends_today = int(st.get("friends_today") or 0) if fdk == dk else 0
            last_comment_at = _parse_iso_utc(str(st.get("last_comment_at") or ""))
            last_friend_at = _parse_iso_utc(str(st.get("last_friend_at") or ""))

            phase = _pick_phase(
                browse=browse,
                progress=progress,
                comments_today=comments_today,
                friends_today=friends_today,
                last_comment_at=last_comment_at,
                last_friend_at=last_friend_at,
                now=now,
                preset=preset,
                allow_ai_comment=llm_for_comments,
            )
            if not phase:
                acc.readiness_status = "ready"
                acc.natural_warmup_json = None
                db.commit()
                continue
            job = create_job(
                db,
                organization_id=acc.organization_id,
                job_type=JOB_TYPE,
                config_snapshot={
                    "fb_account_id": acc.id,
                    "phase": phase,
                    "duration_days": preset.days,
                    "preset_title": preset.title,
                },
                admin_id=None,
            )
            spawn_natural_warmup_worker(job.id)
            logger.info(
                "natural_warmup: scheduled job %s account=%s phase=%s",
                job.id,
                acc.id,
                phase,
            )
    except Exception:
        logger.exception("natural_warmup scheduler tick")
    finally:
        db.close()


def start_natural_warmup_scheduler_thread() -> None:
    def _loop() -> None:
        time.sleep(120)
        while True:
            time.sleep(720)
            try:
                try_natural_warmup_scheduler_tick()
            except Exception:
                logger.exception("natural_warmup scheduler loop")

    threading.Thread(target=_loop, daemon=True, name="natural-warmup-scheduler").start()
