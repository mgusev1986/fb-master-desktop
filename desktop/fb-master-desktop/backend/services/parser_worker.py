"""Фоновый парсинг списка друзей по донорам → автоматический импорт в people + ImportBatch."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from playwright.sync_api import sync_playwright
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from backend.config import effective_fb_local_chrome_cookies
from backend.database import SessionLocal
from backend.models import Donor, FBAccount, ImportBatch, Job, Person
from backend.services.cabinet_settings import effective_base_fb_dir
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.fb_playwright import (
    inject_local_chrome_facebook_cookies_and_reload,
    login_window_busy,
    page_requires_facebook_login,
    resolve_facebook_remembered_login_gate,
)
from backend.services.fb_url_normalize import normalize_facebook_profile_url
from backend.services.friends_list_scrape import (
    dismiss_facebook_dom_overlays,
    donor_friends_page_url,
    flush_friends_workbook,
    live_xlsx_path_for_donor,
    looks_like_fb_restricted_display_name,
    owner_display_name,
    parse_expected_friends_count,
    scroll_friends_page,
)
from backend.services.parallel_automation_slots import parser_worker_slots
from backend.services.playwright_resource import playwright_run_slot
from backend.services.job_logging import (
    finish_job,
    log_job_event,
    start_job,
    update_job_progress,
)
from backend.services.messenger_person_link import relink_messenger_conversations_to_people
from backend.services.person_language import (
    LANG_SEGMENT_UNKNOWN,
    apply_language_result_to_person,
    classify_person_language,
    classify_person_language_fast,
    extract_profile_language_context,
    normalize_person_language_filter,
    normalize_person_language_segment,
    person_matches_language_filter,
    should_profile_scan_for_language,
)
from backend.services.warmup_actions import parse_storage_state, profile_url_from_person
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral

logger = logging.getLogger(__name__)


def normalize_parser_language_mode(raw: Any) -> str:
    """Режим языка при парсинге: none — не трогать метки; fast — быстро; thorough — с заходом в профили."""
    v = str(raw or "fast").strip().lower()
    if v in ("none", "off", "skip"):
        return "none"
    if v in ("thorough", "accurate", "slow", "full"):
        return "thorough"
    return "fast"


def _empty_parser_language_result() -> dict[str, Any]:
    return {
        "segment": LANG_SEGMENT_UNKNOWN,
        "confidence": 0,
        "reasons": [],
        "components": {},
        "intro_excerpt": "",
        "post_excerpts": [],
    }


JOB_TYPE = "donor_friends_parse"


class FacebookSessionRequiredError(RuntimeError):
    """На странице Facebook отображается вход — сессия в профиле недействительна."""


_parser_events_lock = threading.Lock()
_parser_job_cancel_events: dict[int, threading.Event] = {}
_last_progress_mon: float = 0.0


def release_parser_worker_lock_for_job(job_id: int | None) -> None:
    if job_id is None:
        return
    parser_worker_slots.end(int(job_id))


def _release_stale_parser_slots() -> None:
    for jid in list(parser_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = (
                not job
                or job.job_type != JOB_TYPE
                or job.status not in ("running", "queued")
            )
            if stale:
                release_parser_worker_lock_for_job(jid)
        finally:
            db.close()


def parser_worker_busy() -> bool:
    _release_stale_parser_slots()
    return parser_worker_slots.has_any()


def parser_worker_at_capacity() -> bool:
    _release_stale_parser_slots()
    return parser_worker_slots.at_capacity()


def active_parser_job_id() -> int | None:
    """Первый из параллельно идущих job_id (для обратной совместимости)."""
    _release_stale_parser_slots()
    ids = parser_worker_slots.running_ids()
    return ids[0] if ids else None


def parser_org_live_job(db, org_id: int) -> Job | None:
    """Задача парсера для организации, для которой сейчас жив поток."""
    _release_stale_parser_slots()
    alive = parser_worker_slots.running_ids()
    if not alive:
        return None
    return (
        db.query(Job)
        .filter(
            Job.id.in_(alive),
            Job.job_type == JOB_TYPE,
            Job.organization_id == int(org_id),
        )
        .order_by(Job.id.desc())
        .first()
    )


def parser_worker_busy_for_org(db, org_id: int) -> bool:
    return parser_org_live_job(db, org_id) is not None


def request_parser_cancel(job_id: int | None = None) -> bool:
    """Остановить парсер: один job_id или все активные, если job_id не задан."""
    with _parser_events_lock:
        if job_id is not None:
            ev = _parser_job_cancel_events.get(int(job_id))
            if not ev:
                return False
            ev.set()
            return True
        if not _parser_job_cancel_events:
            return False
        for ev in _parser_job_cancel_events.values():
            ev.set()
        return True


def request_parser_cancel_for_account(db, account_id: int) -> bool:
    """Отменить все активные парсеры, привязанные к fb_account_id (из config_snapshot)."""
    aid = int(account_id)
    hit = False
    for jid in list(parser_worker_slots.running_ids()):
        job = db.get(Job, jid)
        if not job or job.job_type != JOB_TYPE:
            continue
        snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
        try:
            ja = int(snap.get("fb_account_id") or 0)
        except (TypeError, ValueError):
            ja = 0
        if ja == aid:
            request_parser_cancel(jid)
            hit = True
    return hit


def finish_stale_parser_jobs(db, *, force: bool = False) -> int:
    """
    Помечаем задачи donor_friends_parse со статусом running/queued как отменённые.

    force=False (дефолт): только если поток парсера не активен — после
        перезапуска сервера/сбоя воркера.
    force=True: даже для живых потоков — клиент нажал «Остановить», а
        Playwright завис на старте Chromium / page.evaluate и не проверяет
        cancel-event. Помечаем в БД как cancelled; зависший thread пусть
        доумирает сам (daemon=True), DB больше не считает job активным.
        Без этого UI висит в статусе «running», даже если cancel-event set.
    """
    alive = set(parser_worker_slots.running_ids())
    rows = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"]))
        .all()
    )
    n = 0
    for j in rows:
        if j.id in alive and not force:
            continue
        finish_job(
            db,
            j,
            status="cancelled",
            error=(
                "Задача остановлена клиентом (force-cancel: Chromium или Playwright завис на старте)."
                if force and j.id in alive
                else "Задача сброшена: активный процесс парсера не найден "
                     "(перезапуск сервера или зависание). Выполните вход в Facebook в разделе «Аккаунты» и запустите снова."
            ),
            clear_progress=True,
        )
        # Выбрасываем job_id из active slots: новый запуск не будет ждать этот thread.
        try:
            parser_worker_slots.release(j.id)
        except Exception:
            pass
        # Ставим cancel-event, если он ещё есть — может быть Playwright
        # всё-таки проснётся и выйдет сам.
        request_parser_cancel(j.id)
        n += 1
    return n


def _is_blank_or_ntp_url(url: str | None) -> bool:
    u = (url or "").strip().lower()
    if not u or u == "about:blank":
        return True
    if u.startswith("chrome://") and "newtab" in u:
        return True
    if "new-tab-page" in u or "new_tab" in u:
        return True
    if u.startswith("edge://") and "newtab" in u:
        return True
    return False


def _parser_pick_work_page(ctx: Any) -> Any:
    """
    Вкладка, где уже открыт facebook.com, иначе новая.
    Не брать «первую попавшуюся» NTP — на ней часто пустой экран, а переход к друзьям остаётся незаметным.
    """
    try:
        for pg in list(ctx.pages):
            try:
                if pg.is_closed():
                    continue
                if "facebook.com" in (pg.url or "").lower():
                    logger.info("parser: вкладка с Facebook — %s", (pg.url or "")[:120])
                    return pg
            except Exception:
                continue
    except Exception:
        pass
    try:
        pg = ctx.new_page()
        logger.info("parser: открыта новая вкладка для парсинга")
        return pg
    except Exception:
        logger.exception("parser: new_page failed, fallback на существующую вкладку")
        for pg in list(ctx.pages):
            try:
                if not pg.is_closed():
                    return pg
            except Exception:
                continue
        return ctx.new_page()


def _parser_warm_facebook_home(ctx: Any, page: Any, *, timeout_ms: int = 120_000) -> None:
    """С пустой/NTP вкладки перейти на facebook.com (сессия из профиля; при FB_LOCAL_CHROME_COOKIES — догрузка cookies)."""
    try:
        cur = (page.url or "").strip().lower()
        if "facebook.com" in cur and not _is_blank_or_ntp_url(page.url):
            return
    except Exception:
        pass
    page.goto(
        "https://www.facebook.com/",
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    page.wait_for_timeout(int(2000))
    dismiss_facebook_dom_overlays(page)
    if effective_fb_local_chrome_cookies():
        try:
            inject_local_chrome_facebook_cookies_and_reload(
                ctx, page, timeout_ms=timeout_ms, log_warning_if_zero=False
            )
        except Exception:
            logger.exception("parser: inject Chrome cookies after warm")


def _parser_settle_facebook_login_gate(page: Any) -> bool:
    clicked = resolve_facebook_remembered_login_gate(page, attempts=3, settle_ms=1800)
    if clicked:
        try:
            dismiss_facebook_dom_overlays(page)
        except Exception:
            pass
    return clicked


def _throttled_progress_update(db, job_id: int, data: dict[str, Any], *, force: bool = False) -> None:
    global _last_progress_mon
    now = time.monotonic()
    if not force and now - _last_progress_mon < 1.25:
        return
    _last_progress_mon = now
    update_job_progress(db, job_id, data)


def _people_state_snapshot(db) -> dict[str, tuple[str | None, bool, int | None]]:
    """Снимок people до прогона парсера по донору (для новых / обновлено / пропущено)."""
    out: dict[str, tuple[str | None, bool, int | None]] = {}
    q = db.query(
        Person.canonical_url,
        Person.display_name,
        Person.fb_profile_restricted,
        Person.donor_id,
    )
    for canonical, dn, r, did in q:
        out[canonical] = (dn, bool(r), did)
    return out


def _parser_batch_for_donor(db, donor: Donor) -> ImportBatch:
    """
    Одна партия импорта на донора для парсера: не плодим строки в истории при каждом запуске.
    Подхватываем старые партии без donor_id с тем же именем файла (parser:… / parser::…).
    Дубликаты сливаются в партию с минимальным id.
    """
    label = (donor.name or "").strip() or f"donor_{donor.id}"
    want_name = f"parser:{label}"
    legacy_double = f"parser::{label}"
    parser_name = or_(
        ImportBatch.filename.startswith("parser:"),
        ImportBatch.filename.startswith("parser::"),
    )

    tied = (
        db.query(ImportBatch)
        .filter(
            ImportBatch.donor_id == donor.id,
            ImportBatch.organization_id == donor.organization_id,
            parser_name,
        )
        .order_by(ImportBatch.id.asc())
        .all()
    )
    orphans = (
        db.query(ImportBatch)
        .filter(
            ImportBatch.donor_id.is_(None),
            ImportBatch.organization_id == donor.organization_id,
            ImportBatch.filename.in_({want_name, legacy_double}),
        )
        .order_by(ImportBatch.id.asc())
        .all()
    )
    seen: set[int] = set()
    batches: list[ImportBatch] = []
    for b in tied + orphans:
        if b.id not in seen:
            seen.add(b.id)
            batches.append(b)
    batches.sort(key=lambda x: x.id)

    if not batches:
        b = ImportBatch(
            organization_id=donor.organization_id,
            filename=want_name,
            donor_id=donor.id,
            rows_total=0,
            rows_new=0,
            rows_updated=0,
            rows_skipped=0,
        )
        db.add(b)
        db.flush()
        return b

    keep = batches[0]
    keep.donor_id = donor.id
    keep.filename = want_name
    for obsolete in batches[1:]:
        db.query(Person).filter(Person.import_batch_id == obsolete.id).update(
            {Person.import_batch_id: keep.id},
            synchronize_session=False,
        )
        db.delete(obsolete)
    db.flush()
    return keep


def _parser_donor_run_counts(
    db,
    donor: Donor,
    final_by_url: dict[str, str],
    start_snap: dict[str, tuple[str | None, bool, int | None]],
) -> tuple[int, int, int]:
    """Сравнение состояния до прогона и после финального merge по каждому URL друга."""
    donor_canon = normalize_facebook_profile_url(donor.url)
    new_c = 0
    upd_c = 0
    skip_c = 0
    for href, _name_raw in final_by_url.items():
        canonical = normalize_facebook_profile_url(href)
        if not canonical or (donor_canon and canonical == donor_canon):
            continue
        person = (
            db.query(Person)
            .filter(
                Person.canonical_url == canonical,
                Person.organization_id == donor.organization_id,
            )
            .first()
        )
        if not person:
            continue
        after = (
            person.display_name,
            bool(person.fb_profile_restricted),
            person.donor_id,
        )
        before = start_snap.get(canonical)
        if before is None:
            new_c += 1
        else:
            b = (before[0], before[1], before[2])
            if b == after:
                skip_c += 1
            else:
                upd_c += 1
    return new_c, upd_c, skip_c


def _assign_parser_batch_to_donor_people(
    db,
    donor: Donor,
    batch: ImportBatch,
    by_url: dict[str, str],
) -> None:
    """
    Привязываем к партии только реально принятые контакты этого запуска.

    Это важно для языковой сегментации на этапе парсинга: если собирать только RU/EN,
    нельзя заново массово привязывать к текущей партии старые записи донора, которые
    не попали в выбранный языковой сегмент.
    """
    canonicals = [
        c
        for c in (normalize_facebook_profile_url(href) for href in by_url.keys())
        if c
    ]
    if not canonicals:
        return
    db.query(Person).filter(
        Person.organization_id == donor.organization_id,
        Person.donor_id == donor.id,
        Person.canonical_url.in_(canonicals),
    ).update(
        {Person.import_batch_id: batch.id},
        synchronize_session=False,
    )


def _load_friends_seed_for_donor(
    db,
    donor: Donor,
    *,
    language_filter: str = "all",
) -> tuple[dict[str, str], dict[str, bool], dict[str, dict[str, Any]]]:
    """Уже сохранённые друзья этого донора — чтобы продолжить парсинг и не терять прогресс."""
    flt = normalize_person_language_filter(language_filter)
    seed: dict[str, str] = {}
    restricted: dict[str, bool] = {}
    lang_cache: dict[str, dict[str, Any]] = {}
    for p in db.query(Person).filter(Person.donor_id == donor.id).all():
        if not p.canonical_url:
            continue
        nm = (p.display_name or "").strip()
        seg = normalize_person_language_segment(p.language_segment)
        if flt == "all" or person_matches_language_filter(seg, flt):
            seed[p.canonical_url] = nm
            restricted[p.canonical_url] = bool(p.fb_profile_restricted)
        if seg != "unknown":
            lang_cache[p.canonical_url] = {
                "segment": seg,
                "confidence": int(p.language_confidence or 0),
            }
    return seed, restricted, lang_cache


def _fb_restricted_flag(
    href_key: str,
    name_raw: str,
    by_restricted: dict[str, bool] | None,
) -> bool:
    br = by_restricted or {}
    return bool(br.get(href_key)) or looks_like_fb_restricted_display_name(name_raw or "")


def _classify_parser_friend_language(
    scan_page: Any | None,
    *,
    canonical_url: str,
    display_name: str,
    language_filter: str,
    language_mode: str = "fast",
) -> dict[str, Any]:
    mode = normalize_parser_language_mode(language_mode)
    if mode == "none":
        return _empty_parser_language_result()

    quick = classify_person_language_fast(display_name or "")
    flt_norm = normalize_person_language_filter(language_filter)

    if mode == "thorough" and scan_page is not None:
        profile_url = profile_url_from_person(canonical_url)
        if profile_url:
            try:
                scan_page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
                scan_page.wait_for_timeout(1400)
                dismiss_facebook_dom_overlays(scan_page)
                if _parser_settle_facebook_login_gate(scan_page):
                    scan_page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
                    scan_page.wait_for_timeout(1200)
                    dismiss_facebook_dom_overlays(scan_page)
                need_login, login_msg = page_requires_facebook_login(scan_page)
                if need_login:
                    raise FacebookSessionRequiredError(
                        login_msg
                        or "Сессия Facebook недействительна во время языкового сканирования."
                    )
                ctx = extract_profile_language_context(scan_page, max_posts=3)
                return classify_person_language(
                    display_name=display_name or "",
                    intro_text=str(ctx.get("intro_text") or ""),
                    post_texts=list(ctx.get("post_texts") or []),
                )
            except FacebookSessionRequiredError:
                raise
            except Exception:
                logger.debug(
                    "parser language scan failed canonical=%s (thorough)",
                    canonical_url,
                    exc_info=True,
                )
                return quick
        return quick

    if flt_norm == "all":
        return quick
    if not should_profile_scan_for_language(quick, language_filter=language_filter):
        return quick
    profile_url = profile_url_from_person(canonical_url)
    if not profile_url or scan_page is None:
        return quick
    try:
        scan_page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        scan_page.wait_for_timeout(1400)
        dismiss_facebook_dom_overlays(scan_page)
        if _parser_settle_facebook_login_gate(scan_page):
            scan_page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
            scan_page.wait_for_timeout(1200)
            dismiss_facebook_dom_overlays(scan_page)
        need_login, login_msg = page_requires_facebook_login(scan_page)
        if need_login:
            raise FacebookSessionRequiredError(
                login_msg
                or "Сессия Facebook недействительна во время языкового сканирования."
            )
        ctx = extract_profile_language_context(scan_page, max_posts=3)
    except FacebookSessionRequiredError:
        raise
    except Exception:
        logger.debug(
            "parser language scan failed canonical=%s",
            canonical_url,
            exc_info=True,
        )
        return quick
    return classify_person_language(
        display_name=display_name or "",
        intro_text=str(ctx.get("intro_text") or ""),
        post_texts=list(ctx.get("post_texts") or []),
    )


def _filter_snapshot_by_language(
    scan_page: Any | None,
    *,
    by_url: dict[str, str],
    by_restricted: dict[str, bool] | None,
    language_filter: str,
    lang_cache: dict[str, dict[str, Any]],
    parser_language_mode: str = "fast",
) -> tuple[dict[str, str], dict[str, bool], dict[str, dict[str, Any]]]:
    flt = normalize_person_language_filter(language_filter)
    mode = normalize_parser_language_mode(parser_language_mode)
    if mode == "none":
        return dict(by_url), dict(by_restricted or {}), {}

    accepted_by_url: dict[str, str] = {}
    accepted_restricted: dict[str, bool] = {}
    language_results: dict[str, dict[str, Any]] = {}

    for href, name_raw in by_url.items():
        canonical = normalize_facebook_profile_url(href)
        if not canonical:
            continue
        result = lang_cache.get(canonical)
        if mode == "thorough" and scan_page is not None:
            result = _classify_parser_friend_language(
                scan_page,
                canonical_url=canonical,
                display_name=name_raw or "",
                language_filter=flt,
                language_mode="thorough",
            )
            lang_cache[canonical] = dict(result)
        elif result is None or (
            flt != "all"
            and (
                normalize_person_language_segment(result.get("segment")) == "unknown"
                or int(result.get("confidence") or 0) < 78
            )
        ):
            result = _classify_parser_friend_language(
                scan_page,
                canonical_url=canonical,
                display_name=name_raw or "",
                language_filter=flt,
                language_mode="fast",
            )
            lang_cache[canonical] = dict(result)
        else:
            result = dict(result)
        language_results[canonical] = dict(result)
        if person_matches_language_filter(result.get("segment"), flt):
            accepted_by_url[href] = name_raw
            if by_restricted and href in by_restricted:
                accepted_restricted[href] = bool(by_restricted[href])
    return accepted_by_url, accepted_restricted, language_results


def _merge_friends_into_parser_batch(
    db,
    *,
    donor: Donor,
    by_url: dict[str, str],
    batch: ImportBatch,
    now: datetime,
    by_restricted: dict[str, bool] | None = None,
    by_language_result: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Идемпотентно вливает словарь в people; без commit (вызывающий делает commit)."""
    donor_canon = normalize_facebook_profile_url(donor.url)
    skipped_no_canon = 0
    for href, name_raw in by_url.items():
        canonical = normalize_facebook_profile_url(href)
        if not canonical:
            skipped_no_canon += 1
            continue
        if donor_canon and canonical == donor_canon:
            continue

        r_flag = _fb_restricted_flag(href, name_raw, by_restricted)
        existing = (
            db.query(Person)
            .filter(
                Person.canonical_url == canonical,
                Person.organization_id == donor.organization_id,
            )
            .first()
        )
        if existing:
            changed = False
            if name_raw and (
                not existing.display_name or len(name_raw) > len(existing.display_name or "")
            ):
                existing.display_name = name_raw
                changed = True
            if existing.donor_id is None:
                existing.donor_id = donor.id
                changed = True
            if bool(existing.fb_profile_restricted) != r_flag:
                existing.fb_profile_restricted = r_flag
                changed = True
            lang_payload = (
                dict(by_language_result.get(canonical) or {})
                if by_language_result and canonical in by_language_result
                else None
            )
            if lang_payload and apply_language_result_to_person(existing, lang_payload):
                changed = True
            if changed:
                existing.updated_at = now
                existing.parsed_at = now
        else:
            person = Person(
                organization_id=donor.organization_id,
                canonical_url=canonical,
                display_name=name_raw or None,
                donor_id=donor.id,
                import_batch_id=batch.id,
                parsed_at=now,
                fb_profile_restricted=r_flag,
            )
            lang_payload = (
                dict(by_language_result.get(canonical) or {})
                if by_language_result and canonical in by_language_result
                else None
            )
            if lang_payload:
                apply_language_result_to_person(person, lang_payload)
            db.add(person)
    if skipped_no_canon and len(by_url) >= 8 and skipped_no_canon * 2 >= len(by_url):
        logger.warning(
            "parser: донор %s — %s из %s ссылок не нормализуются в canonical (в CRM не попадут). "
            "Проверьте формат href в списке FB (часто /profile/<id>).",
            donor.id,
            skipped_no_canon,
            len(by_url),
        )
    elif skipped_no_canon and len(by_url) >= 1:
        logger.debug(
            "parser merge донор %s: пропущено %s ссылок без canonical из %s",
            donor.id,
            skipped_no_canon,
            len(by_url),
        )

    try:
        relink_messenger_conversations_to_people(db)
    except Exception:
        logger.exception("parser: relink messenger conversations after people merge")


def _crm_ids_by_canonical_for_urls(
    db, by_url: dict[str, str], *, organization_id: int
) -> dict[str, int]:
    """Соответствие канонического URL друга → Person.id после вливания в CRM."""
    m: dict[str, int] = {}
    for href in by_url:
        c = normalize_facebook_profile_url(href)
        if not c:
            continue
        row = (
            db.query(Person.id)
            .filter(
                Person.canonical_url == c,
                Person.organization_id == organization_id,
            )
            .first()
        )
        if row:
            m[c] = row[0]
    return m


def _rewrite_friends_xlsx_crm_ids(
    db,
    live_path,
    by_url: dict[str, str] | None,
    *,
    organization_id: int,
) -> None:
    if not by_url or not live_path:
        return
    try:
        db.flush()
        id_map = _crm_ids_by_canonical_for_urls(db, by_url, organization_id=organization_id)
        flush_friends_workbook(
            live_path,
            by_url,
            crm_ids_by_canonical=id_map,
        )
    except Exception:
        logger.exception("parser: failed to rewrite friends xlsx with CRM ids")


def _valid_friend_urls_in_merge(donor: Donor, by_url: dict[str, str]) -> int:
    donor_canon = normalize_facebook_profile_url(donor.url)
    n = 0
    for href, _ in by_url.items():
        c = normalize_facebook_profile_url(href)
        if not c:
            continue
        if donor_canon and c == donor_canon:
            continue
        n += 1
    return n


def _log_parser_donor_imported(
    db,
    *,
    job_id: int,
    fb_account_id: int,
    donor: Donor,
    by_url: dict[str, str],
    batch: ImportBatch,
    rows_new: int,
    rows_updated: int,
    rows_skipped: int,
) -> None:
    valid = _valid_friend_urls_in_merge(donor, by_url)
    batch.rows_total = valid
    batch.rows_new = rows_new
    batch.rows_updated = rows_updated
    batch.rows_skipped = rows_skipped
    batch.updated_at = datetime.now(timezone.utc)
    log_job_event(
        db,
        job_id=job_id,
        event_type="parser_donor_imported",
        fb_account_id=fb_account_id,
        outcome="ok",
        payload={
            "donor_id": donor.id,
            "rows_total": valid,
            "rows_new": rows_new,
            "rows_updated": rows_updated,
            "rows_skipped": rows_skipped,
            "import_batch_id": batch.id,
        },
    )


def process_parser_job(job_id: int) -> None:
    err = parser_worker_slots.try_begin(job_id)
    if err == "duplicate":
        logger.warning("Duplicate parser thread for job %s", job_id)
        return
    if err == "capacity":
        logger.warning(
            "Parser job %s skipped: лимит параллельных парсеров (%s)",
            job_id,
            parser_worker_slots.cap,
        )
        return

    cancel_ev = threading.Event()
    with _parser_events_lock:
        _parser_job_cancel_events[job_id] = cancel_ev

    def is_cancelled() -> bool:
        return cancel_ev.is_set()

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                return
            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            raw_ids = snap.get("donor_ids")
            fb_raw = snap.get("fb_account_id")
            language_filter = normalize_person_language_filter(
                snap.get("language_filter")
            )
            parser_language_mode = normalize_parser_language_mode(
                snap.get("parser_language_mode")
            )
            if parser_language_mode == "none" and language_filter != "all":
                language_filter = "all"
            if not str(fb_raw).strip().isdigit():
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Не выбран аккаунт Facebook",
                    clear_progress=True,
                )
                db.commit()
                return
            fb_account_id = int(fb_raw)
            account = db.get(FBAccount, fb_account_id)
            if not account:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Аккаунт Facebook не найден",
                    clear_progress=True,
                )
                db.commit()
                return
            if account.organization_id != job.organization_id:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Аккаунт не принадлежит организации задачи",
                    clear_progress=True,
                )
                db.commit()
                return
            if not account_in_active_slot(account):
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Аккаунт не в активном слоте (1–3). Назначьте слот в «Аккаунты».",
                    clear_progress=True,
                )
                db.commit()
                return

            donor_ids: list[int] = []
            if isinstance(raw_ids, list) and raw_ids:
                donor_ids = [int(x) for x in raw_ids if str(x).strip().isdigit()]
            if not donor_ids:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Не выбраны доноры",
                    clear_progress=True,
                )
                db.commit()
                return

            donors = (
                db.query(Donor)
                .filter(
                    Donor.id.in_(donor_ids),
                    Donor.organization_id == job.organization_id,
                )
                .order_by(Donor.id)
                .all()
            )
            if not donors or len(donors) != len(set(donor_ids)):
                finish_job(
                    db,
                    job,
                    status="failed",
                    error="Доноры не найдены",
                    clear_progress=True,
                )
                db.commit()
                return

            start_job(db, job)
            now = datetime.now(timezone.utc)
            total_d = len(donors)
            update_job_progress(
                db,
                job_id,
                {
                    "donors_total": total_d,
                    "donors_done": 0,
                    "current_donor": None,
                    "friends_found": 0,
                    "scroll_round": 0,
                    "phase": "starting",
                },
            )

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
                        "Профиль этого аккаунта занят окном «Войти в Facebook». "
                        "Закройте все окна Chromium, открытые через «Войти в Facebook» для этого аккаунта, "
                        "подождите несколько секунд и запустите парсер снова. "
                        "Если окна уже нет — перезапустите FB Master (флаги сессии сбросятся)."
                    ),
                    clear_progress=True,
                )
                db.commit()
                return

            try:
                with sync_playwright() as p:
                    try:
                        with playwright_run_slot(fb_account_id=account.id):
                            with fb_account_playwright_profile_ephemeral(
                                p,
                                account,
                                storage_state=parse_storage_state(account.session_state_json),
                                db=db,
                            ) as ctx:
                                page = _parser_pick_work_page(ctx)
                                # Speed-mode aware hot-path: turbo сжимает фиксированные паузы
                                # после goto FB ×0.1, чтобы парсер быстро доходил до scroll-цикла.
                                # wait_until="domcontentloaded" оставляем всегда: 'commit' оказался
                                # слишком ранним — DOM не успевает сложиться, parse_expected_friends_count
                                # и dismiss_overlays получают пустую страницу и парсер крутил впустую.
                                from backend.services.parser_speed import (
                                    get_parser_scroll_speed,
                                    scroll_wait_multiplier,
                                )
                                _speed_mode = get_parser_scroll_speed(db)
                                _wait_mult = scroll_wait_multiplier(_speed_mode)
                                _goto_wait_until = "domcontentloaded"
                                scan_page = None
                                if language_filter != "all" or parser_language_mode == "thorough":
                                    try:
                                        scan_page = ctx.new_page()
                                    except Exception:
                                        logger.debug("parser: failed to open scan page", exc_info=True)
                                try:
                                    page.set_default_navigation_timeout(120_000)
                                    page.set_default_timeout(120_000)
                                except Exception:
                                    pass
                                if scan_page is not None:
                                    try:
                                        scan_page.set_default_navigation_timeout(90_000)
                                        scan_page.set_default_timeout(90_000)
                                    except Exception:
                                        pass
                                try:
                                    page.bring_to_front()
                                except Exception:
                                    pass
                                _throttled_progress_update(
                                    db,
                                    job_id,
                                    {
                                        "donors_total": total_d,
                                        "donors_done": 0,
                                        "current_donor": None,
                                        "friends_found": 0,
                                        "scroll_round": 0,
                                        "phase": "browser_open",
                                    },
                                    force=True,
                                )

                                warm_failed = False
                                try:
                                    _parser_warm_facebook_home(ctx, page)
                                    _parser_settle_facebook_login_gate(page)
                                    try:
                                        page.bring_to_front()
                                    except Exception:
                                        pass
                                    need_login0, login_msg0 = page_requires_facebook_login(page)
                                    if need_login0:
                                        raise FacebookSessionRequiredError(
                                            f"{login_msg0} "
                                            "Откройте «Facebook-аккаунты», нажмите «Войти в Facebook» для этого профиля "
                                            "и войдите в модальном окне, затем «Проверить сессию». "
                                            "Закройте окно Chromium после входа и только потом запускайте парсер."
                                        )
                                except FacebookSessionRequiredError as e_warm:
                                    logger.warning("parser warm: %s", e_warm)
                                    finish_job(
                                        db,
                                        job,
                                        status="failed",
                                        error=str(e_warm)[:2000],
                                        clear_progress=True,
                                    )
                                    db.commit()
                                    warm_failed = True
                                except Exception as e:
                                    logger.warning("parser warm facebook.com: %s", e)

                                done_count = 0
                                if not warm_failed:
                                    for donor in donors:
                                        if is_cancelled():
                                            finish_job(
                                                db,
                                                job,
                                                status="cancelled",
                                                error="Остановлено пользователем",
                                                clear_progress=True,
                                            )
                                            db.commit()
                                            break

                                        label = donor.name or f"донор #{donor.id}"
                                        friends_url = donor_friends_page_url(donor.url)
                                        try:
                                            _throttled_progress_update(
                                                db,
                                                job_id,
                                                {
                                                    "donors_total": total_d,
                                                    "donors_done": done_count,
                                                    "current_donor": label,
                                                    "friends_found": 0,
                                                    "scroll_round": 0,
                                                    "phase": "navigating",
                                                },
                                                force=True,
                                            )
                                            try:
                                                page.bring_to_front()
                                            except Exception:
                                                pass
                                            page.goto(
                                                friends_url,
                                                wait_until=_goto_wait_until,
                                                timeout=120_000,
                                            )
                                            page.wait_for_timeout(max(200, int(2000 * _wait_mult)))
                                            dismiss_facebook_dom_overlays(page)
                                            if _parser_settle_facebook_login_gate(page):
                                                page.goto(
                                                    friends_url,
                                                    wait_until=_goto_wait_until,
                                                    timeout=120_000,
                                                )
                                                page.wait_for_timeout(max(200, int(1800 * _wait_mult)))
                                                dismiss_facebook_dom_overlays(page)
                                            need_login, login_msg = page_requires_facebook_login(page)
                                            if need_login:
                                                raise FacebookSessionRequiredError(
                                                    f"{login_msg} "
                                                    "Откройте «Facebook-аккаунты», нажмите «Войти в Facebook» для этого профиля "
                                                    "и войдите в модальном окне, затем «Проверить сессию». "
                                                    "Не запускайте парсер, пока окно входа видно в Chromium."
                                                )
                                            expected_friends_n = parse_expected_friends_count(page)
                                            _throttled_progress_update(
                                                db,
                                                job_id,
                                                {
                                                    "donors_total": total_d,
                                                    "donors_done": done_count,
                                                    "current_donor": label,
                                                    "friends_found": 0,
                                                    "scroll_round": 0,
                                                    "phase": "scrolling",
                                                    "expected_friends": expected_friends_n or 0,
                                                },
                                                force=True,
                                            )
                                            if is_cancelled():
                                                finish_job(
                                                    db,
                                                    job,
                                                    status="cancelled",
                                                    error="Остановлено пользователем",
                                                    clear_progress=True,
                                                )
                                                db.commit()
                                                break

                                            owner = owner_display_name(page)
                                            live_path = live_xlsx_path_for_donor(
                                                effective_base_fb_dir(db),
                                                donor.url,
                                                owner or (donor.name or ""),
                                            )

                                            seed, seed_restricted, seed_lang = _load_friends_seed_for_donor(
                                                db,
                                                donor,
                                                language_filter=language_filter,
                                            )
                                            start_snap = _people_state_snapshot(db)
                                            batch = _parser_batch_for_donor(db, donor)
                                            lang_cache = dict(seed_lang)

                                            def _on_round(round_i: int, n_friends: int) -> None:
                                                _throttled_progress_update(
                                                    db,
                                                    job_id,
                                                    {
                                                        "donors_total": total_d,
                                                        "donors_done": done_count,
                                                        "current_donor": label,
                                                        "friends_found": n_friends,
                                                        "scroll_round": round_i,
                                                        "phase": "scrolling",
                                                    },
                                                )

                                            def _flush_people(
                                                snap: dict[str, str], snap_r: dict[str, bool]
                                            ) -> None:
                                                try:
                                                    filtered_snap, filtered_snap_r, lang_results = _filter_snapshot_by_language(
                                                        scan_page,
                                                        by_url=snap,
                                                        by_restricted=snap_r,
                                                        language_filter=language_filter,
                                                        lang_cache=lang_cache,
                                                        parser_language_mode=parser_language_mode,
                                                    )
                                                    _merge_friends_into_parser_batch(
                                                        db,
                                                        donor=donor,
                                                        by_url=filtered_snap,
                                                        batch=batch,
                                                        now=now,
                                                        by_restricted=filtered_snap_r,
                                                        by_language_result=lang_results,
                                                    )
                                                    # История импортов: rows_total раньше обновлялся только в конце донора —
                                                    # при длинном скролле в UI казалось «0», хотя люди уже в CRM.
                                                    v_live = _valid_friend_urls_in_merge(donor, filtered_snap)
                                                    if v_live > int(batch.rows_total or 0):
                                                        batch.rows_total = v_live
                                                        batch.updated_at = now
                                                    db.commit()
                                                except IntegrityError as ie:
                                                    logger.exception(
                                                        "parser: ошибка уникальности при записи people "
                                                        "(донор %s). Возможен дубликат canonical_url: %s",
                                                        donor.id,
                                                        ie,
                                                    )
                                                    db.rollback()
                                                except Exception:
                                                    logger.exception(
                                                        "parser incremental DB flush donor %s", donor.id
                                                    )
                                                    db.rollback()

                                            by_url, by_restricted, _scroll_meta = scroll_friends_page(
                                                page,
                                                live_path=live_path,
                                                expected_total=expected_friends_n,
                                                cancelled=is_cancelled,
                                                on_round=_on_round,
                                                initial_merged=seed if seed else None,
                                                initial_restricted=seed_restricted
                                                if seed_restricted
                                                else None,
                                                on_merged_flush=_flush_people,
                                            )
                                            by_url, by_restricted, lang_results = _filter_snapshot_by_language(
                                                scan_page,
                                                by_url=by_url,
                                                by_restricted=by_restricted,
                                                language_filter=language_filter,
                                                lang_cache=lang_cache,
                                                parser_language_mode=parser_language_mode,
                                            )
                                            if is_cancelled():
                                                # Сразу закрываем Chromium — клиент нажал «Остановить»,
                                                # окно браузера должно исчезнуть мгновенно. Сохранение
                                                # XLSX/БД ниже работает только с in-memory словарями,
                                                # browser ему не нужен.
                                                try:
                                                    ctx.close()
                                                except Exception:
                                                    logger.debug("parser cancel: ctx.close() failed", exc_info=True)
                                                if by_url:
                                                    _throttled_progress_update(
                                                        db,
                                                        job_id,
                                                        {
                                                            "phase": "importing",
                                                            "friends_found": len(by_url),
                                                        },
                                                        force=True,
                                                    )
                                                    _merge_friends_into_parser_batch(
                                                        db,
                                                        donor=donor,
                                                        by_url=by_url,
                                                        batch=batch,
                                                        now=now,
                                                        by_restricted=by_restricted,
                                                        by_language_result=lang_results,
                                                    )
                                                    _assign_parser_batch_to_donor_people(db, donor, batch, by_url)
                                                    rn, ru, rs = _parser_donor_run_counts(
                                                        db, donor, by_url, start_snap
                                                    )
                                                    _log_parser_donor_imported(
                                                        db,
                                                        job_id=job_id,
                                                        fb_account_id=account.id,
                                                        donor=donor,
                                                        by_url=by_url,
                                                        batch=batch,
                                                        rows_new=rn,
                                                        rows_updated=ru,
                                                        rows_skipped=rs,
                                                    )
                                                    _rewrite_friends_xlsx_crm_ids(
                                                        db,
                                                        live_path,
                                                        by_url,
                                                        organization_id=donor.organization_id,
                                                    )
                                                finish_job(
                                                    db,
                                                    job,
                                                    status="cancelled",
                                                    error="Остановлено пользователем",
                                                    clear_progress=True,
                                                )
                                                db.commit()
                                                break

                                            _throttled_progress_update(
                                                db,
                                                job_id,
                                                {
                                                    "phase": "importing",
                                                    "friends_found": len(by_url),
                                                    "scroll_round": 0,
                                                },
                                                force=True,
                                            )
                                            _merge_friends_into_parser_batch(
                                                db,
                                                donor=donor,
                                                by_url=by_url,
                                                batch=batch,
                                                now=now,
                                                by_restricted=by_restricted,
                                                by_language_result=lang_results,
                                            )
                                            _assign_parser_batch_to_donor_people(db, donor, batch, by_url)
                                            rn, ru, rs = _parser_donor_run_counts(
                                                db, donor, by_url, start_snap
                                            )
                                            _log_parser_donor_imported(
                                                db,
                                                job_id=job_id,
                                                fb_account_id=account.id,
                                                donor=donor,
                                                by_url=by_url,
                                                batch=batch,
                                                rows_new=rn,
                                                rows_updated=ru,
                                                rows_skipped=rs,
                                            )
                                            _rewrite_friends_xlsx_crm_ids(
                                                db,
                                                live_path,
                                                by_url,
                                                organization_id=donor.organization_id,
                                            )
                                            done_count += 1
                                            _throttled_progress_update(
                                                db,
                                                job_id,
                                                {
                                                    "donors_done": done_count,
                                                    "current_donor": None,
                                                    "friends_found": 0,
                                                    "scroll_round": 0,
                                                    "phase": "idle",
                                                },
                                                force=True,
                                            )
                                        except FacebookSessionRequiredError as e:
                                            logger.warning("parser donor %s: %s", donor.id, e)
                                            log_job_event(
                                                db,
                                                job_id=job_id,
                                                event_type="parser_donor_error",
                                                severity="error",
                                                fb_account_id=account.id,
                                                outcome="fail",
                                                payload={
                                                    "donor_id": donor.id,
                                                    "error": str(e)[:400],
                                                },
                                            )
                                            finish_job(
                                                db,
                                                job,
                                                status="failed",
                                                error=str(e)[:2000],
                                                clear_progress=True,
                                            )
                                            db.commit()
                                            break
                                        except Exception as e:
                                            logger.exception("parser donor %s", donor.id)
                                            log_job_event(
                                                db,
                                                job_id=job_id,
                                                event_type="parser_donor_error",
                                                severity="error",
                                                fb_account_id=account.id,
                                                outcome="fail",
                                                payload={
                                                    "donor_id": donor.id,
                                                    "error": str(e)[:400],
                                                },
                                            )
                                            db.commit()
    
                                else:
                                    finish_job(db, job, status="success", clear_progress=True)
                                    db.commit()
                    except Exception:
                        raise
            except Exception as e:
                logger.exception("parser job %s playwright", job_id)
                if is_cancelled():
                    finish_job(
                        db,
                        job,
                        status="cancelled",
                        error="Остановлено пользователем",
                        clear_progress=True,
                    )
                else:
                    finish_job(
                        db,
                        job,
                        status="failed",
                        error=(f"Ошибка Playwright/браузера: {e!s}")[:2000],
                        clear_progress=True,
                    )
                db.commit()
        finally:
            db.close()
    finally:
        with _parser_events_lock:
            _parser_job_cancel_events.pop(job_id, None)
        parser_worker_slots.end(job_id)


def spawn_parser_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_parser_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_parser_job,
        args=(job_id,),
        name=f"parser-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned parser job %s", job_id)
