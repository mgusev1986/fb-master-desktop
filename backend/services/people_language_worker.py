"""Фоновая сегментация контактов по языку."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from backend.database import SessionLocal
from backend.models import FBAccount, Job, Person
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.fb_playwright import login_window_busy, page_requires_facebook_login
from backend.services.friends_list_scrape import dismiss_facebook_dom_overlays
from backend.services.job_logging import finish_job, start_job, update_job_progress
from backend.services.person_language import (
    apply_language_result_to_person,
    classify_person_language,
    classify_person_language_fast,
    extract_profile_language_context,
    should_profile_scan_for_language,
)
from backend.services.parallel_automation_slots import people_language_worker_slots
from backend.services.playwright_resource import playwright_run_slot
from backend.services.warmup_actions import parse_storage_state, profile_url_from_person
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral

logger = logging.getLogger(__name__)

JOB_TYPE = "people_language_segment"


def _release_stale_people_language_slots() -> None:
    for jid in list(people_language_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = (
                not job
                or job.job_type != JOB_TYPE
                or job.status not in ("running", "queued")
            )
            if stale:
                people_language_worker_slots.end(jid)
        finally:
            db.close()


def people_language_worker_busy() -> bool:
    _release_stale_people_language_slots()
    return people_language_worker_slots.has_any()


def people_language_worker_at_capacity() -> bool:
    _release_stale_people_language_slots()
    return people_language_worker_slots.at_capacity()


def active_people_language_job_id_for_org(db, org_id: int) -> int | None:
    _release_stale_people_language_slots()
    alive = people_language_worker_slots.running_ids()
    if not alive:
        return None
    row = (
        db.query(Job.id)
        .filter(
            Job.id.in_(alive),
            Job.organization_id == int(org_id),
            Job.job_type == JOB_TYPE,
        )
        .order_by(Job.id.desc())
        .first()
    )
    return int(row[0]) if row else None


def people_language_worker_busy_for_org(db, org_id: int) -> bool:
    return active_people_language_job_id_for_org(db, org_id) is not None


def finish_stale_people_language_jobs(db) -> int:
    alive = set(people_language_worker_slots.running_ids())
    rows = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"]))
        .all()
    )
    n = 0
    for job in rows:
        if job.id in alive:
            continue
        finish_job(
            db,
            job,
            status="cancelled",
            error="Задача сегментации сброшена: активный процесс не найден.",
            clear_progress=True,
        )
        n += 1
    return n


def spawn_people_language_worker(job_id: int) -> None:
    t = threading.Thread(
        target=process_people_language_job,
        args=(job_id,),
        daemon=True,
        name=f"people-language-{job_id}",
    )
    t.start()


def _update_progress(
    db,
    job_id: int,
    *,
    total: int,
    done: int,
    current_name: str = "",
    ru_n: int = 0,
    en_n: int = 0,
    foreign_n: int = 0,
    unknown_n: int = 0,
) -> None:
    update_job_progress(
        db,
        job_id,
        {
            "total": total,
            "done": done,
            "current_name": current_name,
            "ru_count": ru_n,
            "en_count": en_n,
            "foreign_count": foreign_n,
            "unknown_count": unknown_n,
        },
    )


def _classify_person(page, person: Person) -> dict:
    display = (person.display_name or person.first_name or "").strip()
    quick = classify_person_language_fast(display)
    if not should_profile_scan_for_language(quick, language_filter="all"):
        return quick

    url = profile_url_from_person(person.canonical_url or "")
    if not url:
        return quick
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(1500)
        dismiss_facebook_dom_overlays(page)
        need_login, login_msg = page_requires_facebook_login(page)
        if need_login:
            raise RuntimeError(login_msg or "Требуется вход в Facebook")
        ctx = extract_profile_language_context(page, max_posts=3)
    except Exception:
        logger.exception("people-language: profile scan failed person=%s", getattr(person, "id", None))
        return quick
    return classify_person_language(
        display_name=display,
        intro_text=str(ctx.get("intro_text") or ""),
        post_texts=list(ctx.get("post_texts") or []),
    )


def process_people_language_job(job_id: int) -> None:
    err = people_language_worker_slots.try_begin(job_id)
    if err == "duplicate":
        logger.warning("Duplicate people_language thread for job %s", job_id)
        return
    if err == "capacity":
        logger.warning(
            "people_language job %s skipped: лимит параллельных задач (%s)",
            job_id,
            people_language_worker_slots.cap,
        )
        return

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                return
            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            fb_account_id = int(snap.get("fb_account_id") or 0)
            person_ids = [
                int(x) for x in (snap.get("person_ids") or []) if str(x).strip().isdigit()
            ]
            if fb_account_id <= 0 or not person_ids:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Не выбраны аккаунт Facebook или контакты для сегментации.",
                    clear_progress=True,
                )
                return
            account = db.get(FBAccount, fb_account_id)
            if not account:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Аккаунт Facebook для сегментации не найден.",
                    clear_progress=True,
                )
                return
            if account.organization_id != job.organization_id:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Аккаунт не принадлежит организации задачи.",
                    clear_progress=True,
                )
                return
            if not account_in_active_slot(account):
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Аккаунт не в активном слоте 1–3. Назначьте слот в разделе «Аккаунты».",
                    clear_progress=True,
                )
                return

            for _ in range(10):
                if not login_window_busy(account.id):
                    break
                time.sleep(0.6)
            if login_window_busy(account.id):
                finish_job(
                    db,
                    job,
                    status="failed",
                    error=(
                        "Профиль этого аккаунта занят окном входа Facebook. "
                        "Закройте его и запустите сегментацию снова."
                    ),
                    clear_progress=True,
                )
                return

            start_job(db, job)
            total = len(person_ids)
            done = 0
            ru_n = 0
            en_n = 0
            foreign_n = 0
            unknown_n = 0
            _update_progress(db, job_id, total=total, done=0)

            with sync_playwright() as p:
                with playwright_run_slot(fb_account_id=account.id):
                    with fb_account_playwright_profile_ephemeral(
                        p,
                        account,
                        storage_state=parse_storage_state(account.session_state_json),
                        db=db,
                    ) as ctx:
                        page = ctx.new_page()
                        try:
                            page.set_default_navigation_timeout(90_000)
                            page.set_default_timeout(90_000)
                        except Exception:
                            pass
                        for person_id in person_ids:
                            person = (
                                db.query(Person)
                                .filter(
                                    Person.id == person_id,
                                    Person.organization_id == job.organization_id,
                                )
                                .first()
                            )
                            if not person:
                                done += 1
                                _update_progress(db, job_id, total=total, done=done)
                                continue
                            result = _classify_person(page, person)
                            apply_language_result_to_person(person, result)
                            db.commit()

                            seg = result.get("segment")
                            if seg == "ru":
                                ru_n += 1
                            elif seg == "en":
                                en_n += 1
                            elif seg == "foreign":
                                foreign_n += 1
                            else:
                                unknown_n += 1
                            done += 1
                            _update_progress(
                                db,
                                job_id,
                                total=total,
                                done=done,
                                current_name=(person.display_name or person.canonical_url or "")[:200],
                                ru_n=ru_n,
                                en_n=en_n,
                                foreign_n=foreign_n,
                                unknown_n=unknown_n,
                            )

            finish_job(db, job, status="success")
        finally:
            db.close()
    except Exception:
        logger.exception("people-language worker failed job=%s", job_id)
        dbx = SessionLocal()
        try:
            job = dbx.get(Job, job_id)
            if job and job.job_type == JOB_TYPE:
                finish_job(
                    dbx,
                    job,
                    status="failed",
                    error="Сегментация базы завершилась с ошибкой. Смотрите журнал сервера.",
                    clear_progress=True,
                )
        finally:
            dbx.close()
    finally:
        people_language_worker_slots.end(job_id)
