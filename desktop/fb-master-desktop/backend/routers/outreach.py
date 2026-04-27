"""Раздел «Рассылка»: кампании ЛС и комментинга."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, nullslast, or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    AIAgentRun,
    ContactedPerson,
    Donor,
    FBAccount,
    ImportBatch,
    Job,
    OrganizationContactedPerson,
    OutreachCampaign,
    OutreachQueue,
    Person,
    Template,
)
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key
from backend.services.ai_agent_service import (
    BATCH_MAX_PEOPLE,
    DEFAULT_ADDRESSING_COMMENT,
    DEFAULT_ADDRESSING_DM,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
    DEFAULT_PROMPT_COMMENT,
    DEFAULT_PROMPT_DM,
    SETTING_KEYS,
    ai_agent_busy,
    ai_batch_worker,
    end_ai_agent,
    load_ui_settings,
    set_setting,
    throttle_preset_for_ai,
    try_begin_ai_agent,
)
from backend.services.contacted_registry import (
    canonical_url_in_global_contacted_set,
    installation_global_contacted_canonical_urls_set,
    installation_global_skip_person_ids,
    prune_organization_contacted_if_no_account_pairs,
    upsert_contacted,
)
from backend.services.outreach_error_display import humanize_outreach_row_error
from backend.services.job_logging import create_job, finish_job
from backend.services.sending_window import merge_send_window_into_config, parse_send_window_form
from backend.services.tenancy import require_org_id
from backend.services.first_touch_templates import pick_first_touch_templates
from backend.services.message_template_render import (
    apply_template_placeholders,
    placeholder_kwargs_from_person,
    render_outreach_message_for_person,
)
from backend.services.outreach_campaign_done import (
    outreach_done_person_ids,
    outreach_merge_queue_done_into_cfg,
    outreach_strip_done_targets,
)
from backend.services.outreach_worker import (
    JOB_TYPE,
    force_kill_outreach_playwright_for_job,
    outreach_org_has_running_campaign,
    outreach_worker_at_capacity,
    release_outreach_worker_lock_for_job,
    spawn_outreach_worker,
)
from backend.services.person_language import (
    LANGUAGE_FILTER_LABELS,
    apply_person_language_filter,
    language_filter_label,
    normalize_person_language_filter,
)
from backend.services.proxy_health_guard import campaign_fb_account_ids_blocked_message
from backend.services.throttle import (
    PRESET_LABELS,
    TIME_BUDGET_PRESETS,
    check_time_budget_feasibility,
    is_valid_throttle_preset,
)

router = APIRouter(prefix="/outreach", tags=["outreach"])


def _form_bool_last_wins(form: Any, key: str, *, default: bool = True) -> bool:
    """Для hidden=0 + checkbox=1: Starlette отдаёт список значений; последнее побеждает."""
    try:
        vals = form.getlist(key)
    except Exception:
        v = form.get(key)
        vals = [v] if v is not None and str(v) != "" else []
    if not vals:
        return default
    return str(vals[-1]).strip() in ("1", "true", "on", "yes")


def _parse_skip_contacted_scope(_form: Any) -> str:
    """Всегда глобально по всей БД: один человек (один canonical_url) не получит повтор без ручного снятия."""
    return "global"


def _truthy_skip_contacted_enabled(cfg: Any) -> bool:
    """
    Галочка «пропуск уже писали» из JSON конфигурации кампании.
    По умолчанию True. Не использовать голый bool(x): в SQLite/JSON часто приходит 0/1,
    а bool(0) отключил бы пропуск по ошибке.
    """
    if not isinstance(cfg, dict) or "skip_contacted" not in cfg:
        return True
    raw = cfg["skip_contacted"]
    if raw is True:
        return True
    if raw is False:
        return False
    if raw is None:
        return True
    if isinstance(raw, (int, float)):
        try:
            return int(raw) != 0
        except (TypeError, ValueError):
            return True
    s = str(raw).strip().lower()
    if s in ("0", "false", "no", "off", "none", "нет"):
        return False
    if s in ("1", "true", "yes", "on", "да"):
        return True
    return True


def _outreach_message_mode(raw: Any) -> str:
    mode = str(raw or "template").strip().lower()
    return mode if mode in ("template", "ai_dm", "first_touch") else "template"


def _outreach_campaign_kind(raw: Any) -> str:
    kind = str(raw or "dm").strip().lower()
    if kind in ("comment", "commenting", "comments"):
        return "comment"
    return "dm"


def _outreach_comment_mode(raw: Any) -> str:
    mode = str(raw or "template").strip().lower()
    return mode if mode in ("template", "ai_comment") else "template"


def _parse_ai_provider(raw: Any) -> str:
    prov = str(raw or "").strip().lower()
    if prov == "ollama":
        return ""
    return prov if prov in ("openai", "gemini") else ""


def _safe_return_to(raw: str | None, *, fallback: str = "/outreach?focus=agent") -> str:
    s = (raw or "").strip()
    if not s:
        return fallback
    parts = urlsplit(s)
    if parts.scheme or parts.netloc or not parts.path.startswith("/") or parts.path.startswith("//"):
        return fallback
    return urlunsplit(("", "", parts.path, parts.query, parts.fragment))


def _with_query_flag(target: str, key: str, value: str) -> str:
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def _donor_select_is_all(raw: str) -> bool:
    return (raw or "").strip().lower() == "all"


def _is_import_batch_select(raw: str) -> bool:
    s = (raw or "").strip().lower()
    if not s.startswith("batch:"):
        return False
    return s[6:].strip().isdigit()


def _import_batch_id_from_select(raw: str) -> int | None:
    if not _is_import_batch_select(raw):
        return None
    return int((raw or "").strip().split(":", 1)[1].strip())


def _manual_import_batches_query(db: Session, org_id: int):
    """Партии ручного импорта CSV/XLSX (не parser:…)."""
    return (
        db.query(ImportBatch)
        .filter(
            ImportBatch.organization_id == org_id,
            ~ImportBatch.filename.startswith("parser:"),
            ~ImportBatch.filename.startswith("parser::"),
        )
        .order_by(ImportBatch.created_at.desc())
        .limit(120)
        .all()
    )


def _parser_import_batches_query(db: Session, org_id: int) -> list:
    """Партии автозагрузки друзей с парсера (имя файла parser:… / parser::…)."""
    return (
        db.query(ImportBatch)
        .filter(
            ImportBatch.organization_id == org_id,
            or_(
                ImportBatch.filename.startswith("parser:"),
                ImportBatch.filename.startswith("parser::"),
            ),
        )
        .order_by(ImportBatch.created_at.desc())
        .limit(120)
        .all()
    )


def _outreach_campaign_for_org(
    db: Session, campaign_id: int, org_id: int
) -> OutreachCampaign | None:
    return (
        db.query(OutreachCampaign)
        .filter(
            OutreachCampaign.id == campaign_id,
            OutreachCampaign.organization_id == org_id,
        )
        .first()
    )


def _fb_accounts_belong_to_org(
    db: Session,
    account_ids: list[int],
    org_id: int,
    *,
    require_active_slots: bool = False,
) -> bool:
    if not account_ids:
        return True
    uniq = list(dict.fromkeys(account_ids))
    q = db.query(FBAccount.id).filter(
        FBAccount.id.in_(uniq),
        FBAccount.organization_id == org_id,
    )
    if require_active_slots:
        q = q.filter(FBAccount.active_slot.isnot(None))
    n = q.count()
    return n == len(uniq)


def _outreach_accounts_for_form(db: Session, org_id: int) -> list[FBAccount]:
    return (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .order_by(nullslast(FBAccount.active_slot.asc()), FBAccount.label.asc())
        .all()
    )


def _template_belongs_to_org(
    db: Session, template_id: int | None, org_id: int
) -> bool:
    if template_id is None:
        return True
    t = db.get(Template, template_id)
    return bool(t and t.organization_id == org_id)


def _outreach_donor_field_allowed(raw: str) -> bool:
    """Источник в форме: только «вся база» или партия импорта batch:id (без выбора донора по id)."""
    s = (raw or "").strip()
    if _donor_select_is_all(s):
        return True
    return _is_import_batch_select(s) and _import_batch_id_from_select(s) is not None


def _batch_donor_scope_ok(donor_id_raw: str, batch_from_donor: bool) -> bool:
    if not batch_from_donor:
        return True
    s = (donor_id_raw or "").strip()
    if _is_import_batch_select(s):
        return _import_batch_id_from_select(s) is not None
    return _donor_select_is_all(s)


def _parse_like_mode(raw: Any) -> str:
    m = (str(raw or "first")).strip().lower()
    return m if m in ("first", "random") else "first"


def _parse_like_pool_size(raw: Any) -> int:
    try:
        v = int(str(raw or "5").strip())
    except (TypeError, ValueError):
        return 5
    return v if v in (2, 3, 5, 10) else 5


def _parse_like_count(raw: Any) -> int:
    try:
        v = int(str(raw or "1").strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(v, 10))


def _parse_comment_post_mode(raw: Any) -> str:
    mode = str(raw or "first").strip().lower()
    return mode if mode in ("first", "random") else "first"


def _parse_comment_pool_size(raw: Any) -> int:
    try:
        v = int(str(raw or "5").strip())
    except (TypeError, ValueError):
        return 5
    return max(1, min(v, 10))


def _merge_config(
    existing: dict[str, Any] | None,
    *,
    campaign_kind: str,
    ui_donor_id: str,
    language_filter: str,
    preset: str,
    time_budget_minutes: str,
    skip_contacted: bool,
    skip_contacted_scope: str = "global",
    skip_previously_failed: bool = True,
    record_contacted_after_any_dm_attempt: bool = False,
    like_first: bool,
    add_friend_first: bool,
    react_reel_first: bool,
    like_mode: str,
    like_pool_size: str,
    like_count: str,
    message_mode: str,
    ai_dm_example: str,
    ai_dm_hint: str,
    ai_dm_use_profile_post: bool,
    ai_dm_provider: str,
    comment_mode: str,
    ai_comment_hint: str,
    ai_comment_provider: str,
    comment_post_mode: str,
    comment_pool_size: str,
    account_rotation: bool = False,
    auto_slot_rotation: bool = False,
    send_window_24h: bool = False,
    send_window_start_hour: int = 8,
    send_window_end_hour: int = 22,
) -> dict[str, Any]:
    base = dict(existing) if isinstance(existing, dict) else {}
    kind = _outreach_campaign_kind(campaign_kind)
    did_raw = (ui_donor_id or "").strip()
    low_b = did_raw.lower()
    if low_b.startswith("batch:"):
        base["batch_all_donors"] = False
        base.pop("ui_donor_id", None)
        bid = did_raw.split(":", 1)[1].strip()
        if bid.isdigit():
            base["ui_import_batch_id"] = int(bid)
        else:
            base.pop("ui_import_batch_id", None)
    else:
        base.pop("ui_import_batch_id", None)
        if _donor_select_is_all(did_raw):
            base["batch_all_donors"] = True
            base.pop("ui_donor_id", None)
        else:
            base["batch_all_donors"] = False
            if did_raw.isdigit():
                base["ui_donor_id"] = int(did_raw)
            else:
                base.pop("ui_donor_id", None)
    lang = normalize_person_language_filter(language_filter)
    if lang != "all":
        base["language_filter"] = lang
    else:
        base.pop("language_filter", None)
    base["preset"] = preset if is_valid_throttle_preset(preset) else "slow"
    tb_raw = (time_budget_minutes or "").strip()
    if tb_raw:
        try:
            mins = float(tb_raw.replace(",", "."))
            if mins > 0:
                base["time_budget_seconds"] = round(mins * 60.0, 3)
            elif "time_budget_seconds" in base:
                del base["time_budget_seconds"]
        except ValueError:
            pass
    elif "time_budget_seconds" in base:
        del base["time_budget_seconds"]
    base["campaign_kind"] = kind
    if kind in ("dm", "comment"):
        base["skip_contacted"] = bool(skip_contacted)
        base["skip_contacted_scope"] = "global"
        base["skip_previously_failed"] = bool(skip_previously_failed)
        if kind == "dm":
            base["record_contacted_after_any_dm_attempt"] = bool(
                record_contacted_after_any_dm_attempt
            )
        else:
            base.pop("record_contacted_after_any_dm_attempt", None)
    else:
        base["skip_contacted"] = False
        base.pop("skip_contacted_scope", None)
        base["skip_previously_failed"] = bool(skip_previously_failed)
        base.pop("record_contacted_after_any_dm_attempt", None)
    base["batch_from_donor"] = True
    base.pop("dm_messenger_only", None)
    base["like_first"] = like_first
    base["add_friend_first"] = add_friend_first if kind == "dm" else False
    base["react_reel_first"] = bool(react_reel_first) if kind == "dm" else False
    base["like_mode"] = _parse_like_mode(like_mode)
    base["like_pool_size"] = _parse_like_pool_size(like_pool_size)
    base["like_count"] = _parse_like_count(like_count)
    base["message_mode"] = _outreach_message_mode(message_mode)
    base["comment_mode"] = _outreach_comment_mode(comment_mode)
    base["comment_post_mode"] = _parse_comment_post_mode(comment_post_mode)
    base["comment_pool_size"] = _parse_comment_pool_size(comment_pool_size)
    example = (ai_dm_example or "").strip()[:6000]
    if example:
        base["ai_dm_example"] = example
    else:
        base.pop("ai_dm_example", None)
    hint = (ai_dm_hint or "").strip()[:2000]
    if hint:
        base["ai_dm_hint"] = hint
    else:
        base.pop("ai_dm_hint", None)
    base["ai_dm_use_profile_post"] = bool(ai_dm_use_profile_post)
    provider = _parse_ai_provider(ai_dm_provider)
    if provider:
        base["ai_dm_provider"] = provider
    else:
        base.pop("ai_dm_provider", None)
    comment_hint = (ai_comment_hint or "").strip()[:2000]
    if comment_hint:
        base["ai_comment_hint"] = comment_hint
    else:
        base.pop("ai_comment_hint", None)
    comment_provider = _parse_ai_provider(ai_comment_provider)
    if comment_provider:
        base["ai_comment_provider"] = comment_provider
    else:
        base.pop("ai_comment_provider", None)
    if account_rotation:
        base["account_rotation"] = True
    else:
        base.pop("account_rotation", None)
    if auto_slot_rotation:
        base["auto_slot_rotation"] = True
    else:
        base.pop("auto_slot_rotation", None)
    base = merge_send_window_into_config(
        base,
        send_window_24h=send_window_24h,
        send_window_start_hour=send_window_start_hour,
        send_window_end_hour=send_window_end_hour,
    )
    return base


def _render_outreach_body(db: Session, template_id: int | None, person: Person) -> str:
    """Подсказка текста при сборке очереди; фактическая отправка снова выбирает вариант случайно."""
    return render_outreach_message_for_person(db, template_id, person)


def _render_first_touch_body(person: Person) -> str:
    variants = pick_first_touch_templates(1)
    if not variants:
        return ""
    return apply_template_placeholders(variants[0], **placeholder_kwargs_from_person(person)).strip()


def _render_outreach_queue_body(db: Session, camp: OutreachCampaign, person: Person) -> str:
    cfg = camp.config if isinstance(camp.config, dict) else {}
    if _outreach_campaign_kind(cfg.get("campaign_kind")) == "comment":
        if _outreach_comment_mode(cfg.get("comment_mode")) == "template":
            return _render_outreach_body(db, camp.template_id, person)
        return ""
    mode = _outreach_message_mode(cfg.get("message_mode"))
    if mode == "first_touch":
        return _render_first_touch_body(person)
    if mode == "template":
        return _render_outreach_body(db, camp.template_id, person)
    return ""


def _done_in_campaign_set(cfg: dict[str, Any] | None) -> set[int]:
    """Успехи в рамках этой кампании: для ЛС — только личные сообщения; для комментинга — только успешные комментарии."""
    return outreach_done_person_ids(cfg if isinstance(cfg, dict) else None)


def _remove_people_from_campaign_done(
    db: Session, camp: OutreachCampaign, person_ids: set[int]
) -> None:
    """Убрать несколько человек из учёта «уже отправлено в кампании» и снять contacted для аккаунтов кампании."""
    if not person_ids:
        return
    cfg = dict(camp.config) if isinstance(camp.config, dict) else {}
    camp.config = outreach_strip_done_targets(cfg, person_ids)

    acc_ids = list(camp.fb_account_ids or [])
    cfg_kind = _outreach_campaign_kind(((camp.config or {}) if isinstance(camp.config, dict) else {}).get("campaign_kind"))
    if acc_ids and cfg_kind == "dm":
        db.query(ContactedPerson).filter(
            ContactedPerson.person_id.in_(person_ids),
            ContactedPerson.fb_account_id.in_(acc_ids),
        ).delete(synchronize_session=False)
        prune_organization_contacted_if_no_account_pairs(
            db,
            organization_id=int(camp.organization_id),
            person_ids=set(int(x) for x in person_ids),
        )

    db.query(OutreachQueue).filter(
        OutreachQueue.outreach_campaign_id == camp.id,
        OutreachQueue.person_id.in_(person_ids),
        OutreachQueue.status == "done",
    ).delete(synchronize_session=False)


def _remove_person_from_campaign_done(db: Session, camp: OutreachCampaign, person_id: int) -> None:
    _remove_people_from_campaign_done(db, camp, {int(person_id)})


def _persist_contacted_from_dm_queue_before_campaign_delete(
    db: Session, camp: OutreachCampaign
) -> None:
    """
    При удалении кампании ЛС переносим в contacted_people пары из очереди:
    всегда для status=done; для failed — только если в настройках кампании включено
    «заносить после любой попытки» (record_contacted_after_any_dm_attempt).
    """
    cfg = camp.config if isinstance(camp.config, dict) else {}
    if _outreach_campaign_kind(cfg.get("campaign_kind")) != "dm":
        return
    record_any = bool(cfg.get("record_contacted_after_any_dm_attempt", False))
    rows = (
        db.query(OutreachQueue)
        .filter(
            OutreachQueue.outreach_campaign_id == camp.id,
            OutreachQueue.status.in_(("done", "failed")),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.status == "failed" and not record_any:
            continue
        person = db.get(Person, row.person_id)
        if not person:
            continue
        upsert_contacted(
            db,
            person_id=int(row.person_id),
            fb_account_id=int(row.fb_account_id),
            canonical_url=(person.canonical_url or "").strip() or "https://www.facebook.com/",
            touched_at=now,
        )
    if rows:
        db.flush()


def _persist_done_from_queue(db: Session, camp: OutreachCampaign) -> None:
    """Перед сбросом очереди переносим person_id со статусом done в config (не терять прогресс партий)."""
    rows = (
        db.query(OutreachQueue.person_id)
        .filter(
            OutreachQueue.outreach_campaign_id == camp.id,
            OutreachQueue.status == "done",
        )
        .all()
    )
    if not rows:
        return
    cfg = dict(camp.config) if isinstance(camp.config, dict) else {}
    pids = [int(pid) for (pid,) in rows if pid is not None]
    camp.config = outreach_merge_queue_done_into_cfg(cfg, pids)


def _candidate_person_ids(
    db: Session, camp: OutreachCampaign, *, org_id: int
) -> list[int]:
    """
    Кого ещё можно ставить в очередь: без уже успешно отправленных в этой кампании.
    Режим batch_from_donor — живой список из базы по источнику в настройках; person_ids — доп. id из старых кампаний (если были).
    """
    cfg = camp.config if isinstance(camp.config, dict) else {}
    done = _done_in_campaign_set(cfg)
    batch = bool(cfg.get("batch_from_donor"))
    all_donors = bool(cfg.get("batch_all_donors"))
    donor_id = cfg.get("ui_donor_id")
    language_filter = normalize_person_language_filter(cfg.get("language_filter"))
    extra = [pid for pid in (camp.person_ids or []) if pid not in done]

    ibid = cfg.get("ui_import_batch_id")
    if batch:
        if all_donors:
            query = db.query(Person.id).filter(Person.organization_id == org_id)
        elif ibid is not None:
            try:
                bid = int(ibid)
            except (TypeError, ValueError):
                bid = 0
            if bid <= 0:
                return []
            query = db.query(Person.id).filter(
                Person.import_batch_id == bid,
                Person.organization_id == org_id,
            )
        elif donor_id is not None:
            try:
                did = int(donor_id)
            except (TypeError, ValueError):
                did = 0
            if did <= 0:
                return []
            query = db.query(Person.id).filter(
                Person.donor_id == did,
                Person.organization_id == org_id,
            )
        else:
            return []
        query = apply_person_language_filter(query, language_filter)
        donor_part = [
            r[0]
            for r in query.order_by(Person.id).all()
            if r[0] not in done
        ]
        seen_d = set(donor_part)
        return donor_part + [e for e in extra if e not in seen_d]
    return [pid for pid in (camp.person_ids or []) if pid not in done]


def _peek_next_batch_ids(
    db: Session, camp: OutreachCampaign, *, org_id: int, limit: int = 40
) -> list[int]:
    """Превью ID для подсказки в форме (без учёта skip_contacted — это видно только при сборке)."""
    ids = _candidate_person_ids(db, camp, org_id=org_id)
    return ids[: max(0, limit)]


def _next_batch_capacity(camp: OutreachCampaign) -> int:
    """Максимум строк очереди за одну сборку: лимит на аккаунт × число аккаунтов."""
    acc_n = len(camp.fb_account_ids or [])
    cap = max(1, min(int(camp.messages_per_account or 20), 500))
    return cap * acc_n if acc_n else 0


def _previously_failed_person_ids(db: Session) -> set[int]:
    """person_id с хотя бы одной failed-строкой в OutreachQueue (из любой кампании)."""
    rows = (
        db.query(OutreachQueue.person_id)
        .filter(OutreachQueue.status == "failed")
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _truthy_skip_previously_failed(cfg: Any) -> bool:
    if not isinstance(cfg, dict) or "skip_previously_failed" not in cfg:
        return True
    raw = cfg["skip_previously_failed"]
    if raw is True:
        return True
    if raw is False:
        return False
    if raw is None:
        return True
    if isinstance(raw, (int, float)):
        try:
            return int(raw) != 0
        except (TypeError, ValueError):
            return True
    return bool(raw)


def _distribute_outreach_queue(
    db: Session, camp: OutreachCampaign, *, org_id: int
) -> tuple[int, int, int, str | None]:
    """Возвращает (создано, пропущено_contacted, пропущено_failed, код причины если создано=0)."""
    acc_ids: list[int] = []
    for x in camp.fb_account_ids or []:
        try:
            acc_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    person_ids = _candidate_person_ids(db, camp, org_id=org_id)
    cap = max(1, min(int(camp.messages_per_account or 20), 500))
    cfg = camp.config if isinstance(camp.config, dict) else {}
    kind = _outreach_campaign_kind(cfg.get("campaign_kind"))
    skip = _truthy_skip_contacted_enabled(cfg) if kind in ("dm", "comment") else False
    skip_failed = _truthy_skip_previously_failed(cfg) if kind in ("dm", "comment") else False
    like_first = bool(cfg.get("like_first", False))
    add_friend = bool(cfg.get("add_friend_first", False)) if kind == "dm" else False
    react_reel = bool(cfg.get("react_reel_first", False)) if kind == "dm" else False
    tid = camp.template_id
    message_mode = _outreach_message_mode(cfg.get("message_mode"))
    comment_mode = _outreach_comment_mode(cfg.get("comment_mode"))

    if not acc_ids:
        return 0, 0, 0, "no_accounts"
    if not person_ids:
        return 0, 0, 0, "no_people"
    if kind == "dm" and message_mode == "template" and not tid:
        return 0, 0, 0, "need_template"
    if kind == "comment" and comment_mode == "template" and not tid:
        return 0, 0, 0, "need_template"

    _persist_done_from_queue(db, camp)
    db.query(OutreachQueue).filter(OutreachQueue.outreach_campaign_id == camp.id).delete()
    db.commit()

    skip_person_ids: set[int] | None = (
        installation_global_skip_person_ids(db) if skip else None
    )
    skip_url_norms: set[str] | None = (
        installation_global_contacted_canonical_urls_set(db) if skip else None
    )
    failed_person_ids: set[int] | None = (
        _previously_failed_person_ids(db) if skip_failed else None
    )

    counts = {aid: 0 for aid in acc_ids}
    created = 0
    skipped = 0
    skipped_failed = 0
    for pid in person_ids:
        acc_sorted = sorted(acc_ids, key=lambda a: counts.get(a, 0))
        picked = None
        for aid in acc_sorted:
            if counts[aid] < cap:
                picked = aid
                break
        if picked is None:
            break
        person = db.get(Person, pid)
        if skip:
            hit = int(pid) in (skip_person_ids or set())
            if not hit and canonical_url_in_global_contacted_set(
                person.canonical_url if person else None,
                skip_url_norms or set(),
            ):
                hit = True
            if hit:
                skipped += 1
                continue
        if skip_failed and int(pid) in (failed_person_ids or set()):
            skipped_failed += 1
            continue
        counts[picked] += 1
        if not person:
            continue
        body = _render_outreach_queue_body(db, camp, person)
        if kind == "dm" and message_mode != "ai_dm" and not body.strip():
            continue
        if kind == "comment" and comment_mode != "ai_comment" and not body.strip():
            continue
        db.add(
            OutreachQueue(
                outreach_campaign_id=camp.id,
                campaign_name=camp.name,
                person_id=pid,
                fb_account_id=picked,
                template_id=tid,
                message_text=body or None,
                like_first=like_first,
                add_friend_first=add_friend,
                react_reel_first=react_reel,
                status="queued",
                job_id=None,
            )
        )
        created += 1
    db.commit()
    if created == 0:
        if skipped > 0:
            return 0, skipped, skipped_failed, "all_contacted"
        if skipped_failed > 0:
            return 0, skipped, skipped_failed, "all_previously_failed"
        if kind == "dm" and message_mode == "template" and tid:
            return 0, 0, 0, "empty_template_body"
        if kind == "comment" and comment_mode == "template" and tid:
            return 0, 0, 0, "empty_template_body"
        return 0, 0, 0, "no_valid_rows"
    return created, skipped, skipped_failed, None


def _norm_fb_account_id_set(ids: list[int]) -> set[int]:
    out: set[int] = set()
    for x in ids:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _campaign_fb_account_id_list(camp: OutreachCampaign) -> list[int]:
    out: list[int] = []
    if not isinstance(camp.fb_account_ids, list):
        return out
    for x in camp.fb_account_ids:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _redistribute_queued_outreach_rows(
    camp: OutreachCampaign,
    acc_ids: list[int],
    rows: list[OutreachQueue],
) -> None:
    """Перепривязать переданные queued-строки к пулу acc_ids (баланс как при сборке очереди)."""
    if not acc_ids or not rows:
        return
    cap = max(1, min(int(camp.messages_per_account or 20), 500))
    counts = {aid: 0 for aid in acc_ids}
    for idx, row in enumerate(rows):
        acc_sorted = sorted(acc_ids, key=lambda a: counts.get(a, 0))
        picked = None
        for aid in acc_sorted:
            if counts[aid] < cap:
                picked = aid
                break
        if picked is None:
            picked = acc_ids[idx % len(acc_ids)]
        row.fb_account_id = int(picked)
        counts[picked] = counts[picked] + 1


def _remap_queued_outreach_accounts_if_senders_changed(
    db: Session,
    camp: OutreachCampaign,
    new_account_ids: list[int],
    prev_account_ids: list[int],
) -> None:
    """Перепривязать queued-строки к новому пулу отправителей.

    При сохранении редактирования кампании обновляется ``OutreachCampaign.fb_account_ids``,
    но ``OutreachQueue.fb_account_id`` мог остаться прежним; без ротации воркер
    отправляет со старого аккаунта, хотя в форме уже выбран другой.
    """
    if _norm_fb_account_id_set(prev_account_ids) == _norm_fb_account_id_set(new_account_ids):
        return
    acc_ids = [int(x) for x in new_account_ids]
    if not acc_ids:
        return
    rows = (
        db.query(OutreachQueue)
        .filter(
            OutreachQueue.outreach_campaign_id == camp.id,
            OutreachQueue.status == "queued",
        )
        .order_by(OutreachQueue.id.asc())
        .all()
    )
    if not rows:
        return
    _redistribute_queued_outreach_rows(camp, acc_ids, rows)


def _remap_queued_outreach_orphan_row_accounts(db: Session, camp: OutreachCampaign) -> None:
    """Если в очереди остались аккаунты, которых уже нет в кампании — перераспределить (queued).

    Страховка при «Старт» / возобновлении после паузы: снимок в БД мог не совпасть с
    ``fb_account_ids`` (редкие гонки, старые строки после смены отправителей).
    """
    acc_ids = _campaign_fb_account_id_list(camp)
    if not acc_ids:
        return
    pool = set(acc_ids)
    rows = (
        db.query(OutreachQueue)
        .filter(
            OutreachQueue.outreach_campaign_id == camp.id,
            OutreachQueue.status == "queued",
        )
        .order_by(OutreachQueue.id.asc())
        .all()
    )
    if not rows:
        return
    if all(int(r.fb_account_id) in pool for r in rows):
        return
    _redistribute_queued_outreach_rows(camp, acc_ids, rows)


@router.get("")
async def outreach_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    items = (
        db.query(OutreachCampaign)
        .filter(OutreachCampaign.organization_id == org_id)
        .order_by(OutreachCampaign.id.desc())
        .limit(50)
        .all()
    )
    qp = request.query_params
    highlight_id = qp.get("highlight")
    highlight_run = None
    if highlight_id and highlight_id.isdigit():
        highlight_run = db.get(AIAgentRun, int(highlight_id))
    runs = (
        db.query(AIAgentRun)
        .outerjoin(Person, AIAgentRun.person_id == Person.id)
        .filter(
            or_(
                AIAgentRun.person_id.is_(None),
                Person.organization_id == org_id,
            )
        )
        .order_by(AIAgentRun.created_at.desc())
        .limit(80)
        .all()
    )
    person_ids = {r.person_id for r in runs if r.person_id}
    if highlight_run and highlight_run.person_id:
        person_ids.add(highlight_run.person_id)
    person_labels: dict[int, str] = {}
    if person_ids:
        for p in (
            db.query(Person)
            .filter(Person.id.in_(person_ids), Person.organization_id == org_id)
            .all()
        ):
            person_labels[p.id] = (p.display_name or p.canonical_url or "")[:80]
    agent_donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    throttle_preset = throttle_preset_for_ai(db)
    throttle_preset_label = PRESET_LABELS.get(throttle_preset, throttle_preset)
    has_openai = bool(effective_openai_api_key(db).strip())
    has_gemini = bool(effective_google_llm_api_key(db).strip())
    open_agent_panel = (
        qp.get("focus") == "agent"
        or bool(highlight_run)
        or bool(qp.get("err"))
        or qp.get("agent_saved") == "1"
        or qp.get("agent_done") == "1"
    )
    page_id = "outreach_magic" if qp.get("focus") == "agent" else "outreach"
    org_running = outreach_org_has_running_campaign(db, org_id)
    lock_busy = outreach_worker_at_capacity()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "outreach/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": page_id,
            "campaigns": items,
            "outreach_running": org_running,
            "worker_busy": lock_busy,
            "settings": load_ui_settings(db),
            "runs": runs,
            "donors": agent_donors,
            "person_labels": person_labels,
            "highlight_run": highlight_run,
            "has_openai": has_openai,
            "has_gemini": has_gemini,
            "throttle_preset": throttle_preset,
            "throttle_preset_label": throttle_preset_label,
            "err": qp.get("err"),
            "saved_agent_settings": qp.get("agent_saved") == "1",
            "agent_done": qp.get("agent_done") == "1",
            "batch_contacts": qp.get("batch_contacts"),
            "batch_rows": qp.get("batch_rows"),
            "open_agent_panel": open_agent_panel,
        },
    )


@router.get("/api/donor-summary")
async def outreach_api_donor_summary(
    request: Request,
    donor_id: str = Query(...),
    lang: str = Query("all"),
    db: Session = Depends(get_db),
):
    """Число людей у донора, по партии импорта или у всех — для формы рассылки (fetch из браузера)."""
    org_id = require_org_id(request, db)
    language_filter = normalize_person_language_filter(lang)
    raw_full = (donor_id or "").strip()
    raw = raw_full.lower()
    if raw == "all":
        query = db.query(Person.id).filter(Person.organization_id == org_id)
        query = apply_person_language_filter(query, language_filter)
        n = query.count()
        return {
            "donor_id": "all",
            "name": "Вся база людей",
            "people_count": int(n),
            "language_filter": language_filter,
            "language_label": language_filter_label(language_filter),
        }
    if raw.startswith("batch:"):
        bid_s = raw_full.split(":", 1)[1].strip()
        if not bid_s.isdigit():
            return JSONResponse({"error": "bad_batch_id"}, status_code=400)
        bid = int(bid_s)
        batch = (
            db.query(ImportBatch)
            .filter(ImportBatch.id == bid, ImportBatch.organization_id == org_id)
            .first()
        )
        if not batch:
            return JSONResponse({"error": "batch_not_found"}, status_code=404)
        query = db.query(Person.id).filter(
            Person.import_batch_id == bid,
            Person.organization_id == org_id,
        )
        query = apply_person_language_filter(query, language_filter)
        n = query.count()
        return {
            "donor_id": raw_full,
            "name": (batch.filename or "")[:200],
            "people_count": int(n),
            "language_filter": language_filter,
            "language_label": language_filter_label(language_filter),
        }
    if not donor_id.strip().isdigit():
        return JSONResponse({"error": "bad_donor_id"}, status_code=400)
    did = int(donor_id.strip())
    donor = (
        db.query(Donor)
        .filter(Donor.id == did, Donor.organization_id == org_id)
        .first()
    )
    if not donor:
        return JSONResponse({"error": "donor_not_found"}, status_code=404)
    query = db.query(Person.id).filter(Person.donor_id == did, Person.organization_id == org_id)
    query = apply_person_language_filter(query, language_filter)
    n = query.count()
    return {
        "donor_id": did,
        "name": (donor.name or donor.url or "")[:200],
        "people_count": int(n),
        "language_filter": language_filter,
        "language_label": language_filter_label(language_filter),
    }


@router.get("/api/donor-person-ids")
async def outreach_api_donor_person_ids(
    request: Request,
    donor_id: str = Query(...),
    lang: str = Query("all"),
    db: Session = Depends(get_db),
):
    """Все ID людей донора, партии импорта или всех — подставить в поле вручную (режим без партий)."""
    org_id = require_org_id(request, db)
    language_filter = normalize_person_language_filter(lang)
    raw_full = (donor_id or "").strip()
    raw = raw_full.lower()
    if raw == "all":
        query = db.query(Person.id).filter(Person.organization_id == org_id)
        query = apply_person_language_filter(query, language_filter)
        ids = [
            r[0]
            for r in query.order_by(Person.id).all()
        ]
        return {"donor_id": "all", "ids": ids, "count": len(ids)}
    if raw.startswith("batch:"):
        bid_s = raw_full.split(":", 1)[1].strip()
        if not bid_s.isdigit():
            return JSONResponse({"error": "bad_batch_id"}, status_code=400)
        bid = int(bid_s)
        if not (
            db.query(ImportBatch)
            .filter(ImportBatch.id == bid, ImportBatch.organization_id == org_id)
            .first()
        ):
            return JSONResponse({"error": "batch_not_found"}, status_code=404)
        query = db.query(Person.id).filter(
            Person.import_batch_id == bid,
            Person.organization_id == org_id,
        )
        query = apply_person_language_filter(query, language_filter)
        ids = [
            r[0]
            for r in query.order_by(Person.id).all()
        ]
        return {"donor_id": raw_full, "ids": ids, "count": len(ids)}
    if not donor_id.strip().isdigit():
        return JSONResponse({"error": "bad_donor_id"}, status_code=400)
    did = int(donor_id.strip())
    donor = (
        db.query(Donor)
        .filter(Donor.id == did, Donor.organization_id == org_id)
        .first()
    )
    if not donor:
        return JSONResponse({"error": "donor_not_found"}, status_code=404)
    query = db.query(Person.id).filter(
        Person.donor_id == did,
        Person.organization_id == org_id,
    )
    query = apply_person_language_filter(query, language_filter)
    ids = [
        r[0]
        for r in query.order_by(Person.id).all()
    ]
    return {"donor_id": did, "ids": ids, "count": len(ids)}


@router.get("/new")
async def outreach_new_form(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    campaign_kind = _outreach_campaign_kind(request.query_params.get("kind"))
    accounts = _outreach_accounts_for_form(db, org_id)
    import_batches = _manual_import_batches_query(db, org_id)
    parser_import_batches = _parser_import_batches_query(db, org_id)
    tpls = (
        db.query(Template)
        .filter(Template.organization_id == org_id)
        .order_by(Template.name)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "outreach/campaign_form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "outreach",
            "campaign": None,
            "accounts": accounts,
            "donors": [],
            "import_batches": import_batches,
            "parser_import_batches": parser_import_batches,
            "templates": tpls,
            "preset_labels": PRESET_LABELS,
            "time_budget_presets": TIME_BUDGET_PRESETS,
            "batch_preview_ids": [],
            "campaign_kind": campaign_kind,
            "agent_settings": load_ui_settings(db),
            "has_openai": bool((effective_openai_api_key(db) or "").strip()),
            "has_gemini": bool((effective_google_llm_api_key(db) or "").strip()),
            "language_filter_labels": {
                key: label
                for key, label in LANGUAGE_FILTER_LABELS.items()
                if key != "unknown"
            },
        },
    )


@router.post("/new")
async def outreach_new_save(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    campaign_kind = _outreach_campaign_kind(form.get("campaign_kind"))
    default_name = "Комментинг без названия" if campaign_kind == "comment" else "Рассылка без названия"
    name = (form.get("name") or "").strip() or default_name
    try:
        cap = max(1, min(int(form.get("messages_per_account") or 20), 500))
    except (TypeError, ValueError):
        cap = 20
    tid_raw = (form.get("template_id") or "").strip()
    template_id = int(tid_raw) if tid_raw.isdigit() else None
    if not _template_belongs_to_org(db, template_id, org_id):
        return RedirectResponse("/outreach/new?err=template", status_code=303)

    sel = [int(x) for x in form.getlist("fb_account_id") if str(x).strip().isdigit()]
    sel = list(dict.fromkeys(sel))
    account_rotation = form.get("account_rotation") == "1"
    auto_slot_rotation = form.get("auto_slot_rotation") == "1"
    if not _fb_accounts_belong_to_org(
        db, sel, org_id, require_active_slots=not account_rotation
    ):
        return RedirectResponse("/outreach/new?err=accounts", status_code=303)
    if not sel:
        return RedirectResponse("/outreach/new?err=no_senders", status_code=303)

    donor_id = (form.get("donor_id") or "").strip()
    if not donor_id or not _outreach_donor_field_allowed(donor_id):
        return RedirectResponse("/outreach/new?err=no_source", status_code=303)
    if not _batch_donor_scope_ok(donor_id, True):
        return RedirectResponse("/outreach/new?err=batch_scope", status_code=303)
    if _is_import_batch_select(donor_id):
        bid = _import_batch_id_from_select(donor_id) or 0
        if bid <= 0 or not (
            db.query(ImportBatch)
            .filter(ImportBatch.id == bid, ImportBatch.organization_id == org_id)
            .first()
        ):
            return RedirectResponse("/outreach/new?err=batch", status_code=303)
    person_ids: list[int] = []

    sw24, sw_s, sw_e = parse_send_window_form(form)
    record_any = _form_bool_last_wins(form, "record_contacted_after_any_dm_attempt", default=False)
    cfg = _merge_config(
        None,
        campaign_kind=campaign_kind,
        ui_donor_id=donor_id,
        language_filter=str(form.get("language_filter") or "all"),
        preset=str(form.get("preset") or "slow"),
        time_budget_minutes=str(form.get("time_budget_minutes") or ""),
        skip_contacted=_form_bool_last_wins(form, "skip_contacted", default=True),
        skip_contacted_scope=_parse_skip_contacted_scope(form),
        skip_previously_failed=_form_bool_last_wins(form, "skip_previously_failed", default=True),
        record_contacted_after_any_dm_attempt=record_any,
        like_first=bool(form.get("like_first")),
        add_friend_first=bool(form.get("add_friend_first")),
        react_reel_first=bool(form.get("react_reel_first")),
        like_mode=str(form.get("like_mode") or "first"),
        like_pool_size=str(form.get("like_pool_size") or "5"),
        like_count=str(form.get("like_count") or "1"),
        message_mode=str(form.get("message_mode") or "template"),
        ai_dm_example=str(form.get("ai_dm_example") or ""),
        ai_dm_hint=str(form.get("ai_dm_hint") or ""),
        ai_dm_use_profile_post=bool(form.get("ai_dm_use_profile_post")),
        ai_dm_provider=str(form.get("ai_dm_provider") or ""),
        comment_mode=str(form.get("comment_mode") or "template"),
        ai_comment_hint=str(form.get("ai_comment_hint") or ""),
        ai_comment_provider=str(form.get("ai_comment_provider") or ""),
        comment_post_mode=str(form.get("comment_post_mode") or "first"),
        comment_pool_size=str(form.get("comment_pool_size") or "5"),
        account_rotation=account_rotation,
        auto_slot_rotation=auto_slot_rotation and account_rotation,
        send_window_24h=sw24,
        send_window_start_hour=sw_s,
        send_window_end_hour=sw_e,
    )
    status = (form.get("status") or "draft").strip()
    if status not in ("draft", "active"):
        status = "draft"

    camp = OutreachCampaign(
        organization_id=org_id,
        name=name,
        status=status,
        fb_account_ids=sel,
        person_ids=person_ids,
        template_id=template_id,
        messages_per_account=cap,
        config=cfg,
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return RedirectResponse(f"/outreach/{camp.id}", status_code=303)


@router.post("/agent/settings")
async def outreach_agent_save_settings(
    request: Request,
    db: Session = Depends(get_db),
    default_provider: str = Form("gemini"),
    model_openai: str = Form(""),
    model_gemini: str = Form(""),
    prompt_dm: str = Form(""),
    prompt_comment: str = Form(""),
    addressing_dm: str = Form(""),
    addressing_comment: str = Form(""),
    return_to: str = Form(""),
):
    dp = (default_provider or "gemini").lower().strip()
    if dp == "ollama":
        dp = "gemini"
    if dp not in ("openai", "gemini"):
        dp = "gemini"
    set_setting(db, SETTING_KEYS["default_provider"], dp)
    set_setting(
        db,
        SETTING_KEYS["model_openai"],
        (model_openai or DEFAULT_MODEL_OPENAI).strip() or DEFAULT_MODEL_OPENAI,
    )
    set_setting(
        db,
        SETTING_KEYS["model_gemini"],
        (model_gemini or DEFAULT_MODEL_GEMINI).strip() or DEFAULT_MODEL_GEMINI,
    )
    set_setting(
        db,
        SETTING_KEYS["prompt_dm"],
        (prompt_dm or DEFAULT_PROMPT_DM).strip() or DEFAULT_PROMPT_DM,
    )
    set_setting(
        db,
        SETTING_KEYS["prompt_comment"],
        (prompt_comment or DEFAULT_PROMPT_COMMENT).strip() or DEFAULT_PROMPT_COMMENT,
    )
    set_setting(
        db,
        SETTING_KEYS["addressing_dm"],
        (addressing_dm or DEFAULT_ADDRESSING_DM).strip() or DEFAULT_ADDRESSING_DM,
    )
    set_setting(
        db,
        SETTING_KEYS["addressing_comment"],
        (addressing_comment or DEFAULT_ADDRESSING_COMMENT).strip() or DEFAULT_ADDRESSING_COMMENT,
    )
    db.commit()
    target = _safe_return_to(return_to, fallback="/outreach?focus=agent")
    return RedirectResponse(_with_query_flag(target, "agent_saved", "1"), status_code=303)


def _agent_redirect_err(code: str) -> RedirectResponse:
    return RedirectResponse(f"/outreach?focus=agent&err={code}", status_code=303)


@router.post("/agent/batch-generate")
async def outreach_agent_batch_generate(
    request: Request,
    db: Session = Depends(get_db),
    context_type: str = Form("dm"),
    provider: str = Form(""),
    batch_context: str = Form(""),
    donor_id: str = Form(""),
    batch_count: str = Form("10"),
    dm_mode: str = Form("custom"),
    variants_per_person: str = Form("3"),
):
    org_id = require_org_id(request, db)
    ctx = (context_type or "dm").lower().strip()
    if ctx not in ("dm", "comment"):
        return _agent_redirect_err("bad_context")

    mode = (dm_mode or "custom").strip().lower()
    if ctx == "comment":
        mode = "custom"

    try:
        vpp = int((variants_per_person or "3").strip())
    except ValueError:
        vpp = 3
    vpp = max(1, min(vpp, 10))

    tpl = (batch_context or "").strip()
    llm_provider_for_job = (provider or "").strip().lower()
    if mode == "first_touch":
        if ctx != "dm":
            return _agent_redirect_err("first_touch_dm_only")
        tpl = ""
    elif not tpl:
        return _agent_redirect_err("batch_empty")
    else:
        ui = load_ui_settings(db)
        prov = (provider or ui.get("default_provider") or "").lower().strip()
        if prov == "ollama":
            prov = "gemini"
        if prov not in ("openai", "gemini"):
            prov = "gemini"
        oa = effective_openai_api_key(db)
        gk = effective_google_llm_api_key(db)
        if prov == "openai" and not oa.strip():
            prov = "gemini"
        if prov == "gemini" and not gk.strip():
            prov = "openai"
        if prov == "openai" and not oa.strip():
            return _agent_redirect_err("no_openai")
        if prov == "gemini" and not gk.strip():
            return _agent_redirect_err("no_gemini")
        llm_provider_for_job = prov

    try:
        n = int((batch_count or "10").strip())
    except ValueError:
        n = 10
    n = max(1, min(n, BATCH_MAX_PEOPLE))

    q = db.query(Person).filter(Person.organization_id == org_id)
    if donor_id and str(donor_id).strip().isdigit():
        did = int(donor_id.strip())
        if not (
            db.query(Donor)
            .filter(Donor.id == did, Donor.organization_id == org_id)
            .first()
        ):
            return _agent_redirect_err("bad_donor")
        q = q.filter(Person.donor_id == did)
    people = q.order_by(Person.id.desc()).limit(n).all()
    if not people:
        return _agent_redirect_err("no_people_batch")

    if outreach_worker_at_capacity():
        return _agent_redirect_err("agent_outreach_busy")
    if not try_begin_ai_agent():
        return _agent_redirect_err("agent_self_busy")

    ids = [p.id for p in people]
    total_rows = len(ids) * vpp if mode == "first_touch" else len(ids)
    try:
        await ai_batch_worker(
            ids,
            ctx,
            llm_provider_for_job,
            tpl,
            job_mode=mode,
            variants_per_person=vpp,
        )
    finally:
        end_ai_agent()

    return RedirectResponse(
        f"/outreach?focus=agent&agent_done=1&batch_contacts={len(ids)}&batch_rows={total_rows}",
        status_code=303,
    )


@router.get("/{campaign_id:int}")
async def outreach_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    job = None
    if camp.job_id:
        j = db.get(Job, camp.job_id)
        if j and j.organization_id == org_id:
            job = j
    rows = (
        db.query(OutreachQueue)
        .filter(OutreachQueue.outreach_campaign_id == camp.id)
        .order_by(OutreachQueue.id.asc())
        .limit(150)
        .all()
    )
    pids = list({r.person_id for r in rows})
    aids = list({r.fb_account_id for r in rows})
    people = (
        {
            p.id: p
            for p in db.query(Person)
            .filter(Person.id.in_(pids), Person.organization_id == org_id)
            .all()
        }
        if pids
        else {}
    )
    accs = (
        {
            a.id: a
            for a in db.query(FBAccount)
            .filter(FBAccount.id.in_(aids), FBAccount.organization_id == org_id)
            .all()
        }
        if aids
        else {}
    )
    cnt_by = dict(
        db.query(OutreachQueue.status, func.count(OutreachQueue.id))
        .filter(OutreachQueue.outreach_campaign_id == camp.id)
        .group_by(OutreachQueue.status)
        .all()
    )
    total_q = sum(cnt_by.values()) or 0
    cfg = camp.config if isinstance(camp.config, dict) else {}
    tb = cfg.get("time_budget_seconds")
    feas = None
    if tb and total_q:
        action_key = "comment_post" if _outreach_campaign_kind(cfg.get("campaign_kind")) == "comment" else "send_dm"
        feas = check_time_budget_feasibility(total_q, float(tb), action_key)
    tpl = (
        db.query(Template)
        .filter(Template.id == camp.template_id, Template.organization_id == org_id)
        .first()
        if camp.template_id
        else None
    )
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    all_templates = (
        db.query(Template)
        .filter(Template.organization_id == org_id)
        .order_by(Template.name)
        .all()
    )
    pf = cfg

    done_campaign = sorted(_done_in_campaign_set(cfg))
    done_campaign_people: list[Person] = []
    if done_campaign:
        dpmap = {
            p.id: p
            for p in db.query(Person)
            .filter(Person.id.in_(done_campaign), Person.organization_id == org_id)
            .all()
        }
        done_campaign_people = [dpmap[i] for i in done_campaign if i in dpmap]

    donor_total = None
    donor_left_estimate = None
    import_batch_label = None
    if bool(cfg.get("batch_from_donor")) and bool(cfg.get("batch_all_donors")):
        query = db.query(Person.id).filter(Person.organization_id == org_id)
        query = apply_person_language_filter(query, cfg.get("language_filter"))
        donor_total = query.count()
        donor_left_estimate = max(0, donor_total - len(done_campaign))
    elif bool(cfg.get("batch_from_donor")) and cfg.get("ui_import_batch_id") is not None:
        try:
            bid = int(cfg["ui_import_batch_id"])
        except (TypeError, ValueError):
            bid = 0
        if bid > 0:
            b_row = (
                db.query(ImportBatch)
                .filter(ImportBatch.id == bid, ImportBatch.organization_id == org_id)
                .first()
            )
            import_batch_label = (b_row.filename if b_row else None) or f"партия #{bid}"
            query = db.query(Person.id).filter(
                Person.import_batch_id == bid,
                Person.organization_id == org_id,
            )
            query = apply_person_language_filter(query, cfg.get("language_filter"))
            donor_total = query.count()
            donor_left_estimate = max(0, donor_total - len(done_campaign))
    elif bool(cfg.get("batch_from_donor")) and cfg.get("ui_donor_id") is not None:
        try:
            did = int(cfg["ui_donor_id"])
        except (TypeError, ValueError):
            did = 0
        if did > 0:
            query = db.query(Person.id).filter(
                Person.donor_id == did,
                Person.organization_id == org_id,
            )
            query = apply_person_language_filter(query, cfg.get("language_filter"))
            donor_total = query.count()
            donor_left_estimate = max(0, donor_total - len(done_campaign))

    next_batch_preview = _peek_next_batch_ids(db, camp, org_id=org_id, limit=35)
    max_next_slots = _next_batch_capacity(camp)
    candidates_remaining = len(_candidate_person_ids(db, camp, org_id=org_id))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "outreach/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "outreach",
            "campaign": camp,
            "job": job,
            "queue_rows": rows,
            "people": people,
            "accs": accs,
            "cnt_by": cnt_by,
            "preset_labels": PRESET_LABELS,
            "time_budget_presets": TIME_BUDGET_PRESETS,
            "budget_feasibility": feas,
            "template_row": tpl,
            "donors": donors,
            "templates": all_templates,
            "pf": pf,
            "done_campaign": done_campaign,
            "done_campaign_people": done_campaign_people,
            "donor_total": donor_total,
            "donor_left_estimate": donor_left_estimate,
            "import_batch_label": import_batch_label,
            "next_batch_preview": next_batch_preview,
            "max_next_slots": max_next_slots,
            "candidates_remaining": candidates_remaining,
            "campaign_kind": _outreach_campaign_kind(cfg.get("campaign_kind")),
            "message_mode": _outreach_message_mode(cfg.get("message_mode")),
            "comment_mode": _outreach_comment_mode(cfg.get("comment_mode")),
            "language_filter_label": language_filter_label,
            "humanize_outreach_row_error": humanize_outreach_row_error,
        },
    )


@router.get("/{campaign_id:int}/edit")
async def outreach_edit_form(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)
    accounts = _outreach_accounts_for_form(db, org_id)
    import_batches = _manual_import_batches_query(db, org_id)
    parser_import_batches = _parser_import_batches_query(db, org_id)
    tpls = (
        db.query(Template)
        .filter(Template.organization_id == org_id)
        .order_by(Template.name)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "outreach/campaign_form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "outreach",
            "campaign": camp,
            "accounts": accounts,
            "donors": [],
            "import_batches": import_batches,
            "parser_import_batches": parser_import_batches,
            "templates": tpls,
            "preset_labels": PRESET_LABELS,
            "time_budget_presets": TIME_BUDGET_PRESETS,
            "batch_preview_ids": [],
            "campaign_kind": _outreach_campaign_kind(((camp.config or {}) if isinstance(camp.config, dict) else {}).get("campaign_kind")),
            "agent_settings": load_ui_settings(db),
            "has_openai": bool((effective_openai_api_key(db) or "").strip()),
            "has_gemini": bool((effective_google_llm_api_key(db) or "").strip()),
            "language_filter_labels": {
                key: label
                for key, label in LANGUAGE_FILTER_LABELS.items()
                if key != "unknown"
            },
        },
    )


@router.post("/{campaign_id:int}/edit")
async def outreach_edit_save(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)

    prev_sender_ids: list[int] = []
    if isinstance(camp.fb_account_ids, list):
        for x in camp.fb_account_ids:
            try:
                prev_sender_ids.append(int(x))
            except (TypeError, ValueError):
                continue

    form = await request.form()
    campaign_kind = _outreach_campaign_kind(form.get("campaign_kind") or ((camp.config or {}) if isinstance(camp.config, dict) else {}).get("campaign_kind"))
    camp.name = (form.get("name") or "").strip() or camp.name
    try:
        camp.messages_per_account = max(1, min(int(form.get("messages_per_account") or 20), 500))
    except (TypeError, ValueError):
        pass
    tid_raw = (form.get("template_id") or "").strip()
    camp.template_id = int(tid_raw) if tid_raw.isdigit() else None
    if not _template_belongs_to_org(db, camp.template_id, org_id):
        return RedirectResponse(f"/outreach/{campaign_id}/edit?err=template", status_code=303)

    sel = [int(x) for x in form.getlist("fb_account_id") if str(x).strip().isdigit()]
    sel = list(dict.fromkeys(sel))
    account_rotation = form.get("account_rotation") == "1"
    auto_slot_rotation = form.get("auto_slot_rotation") == "1"
    if not _fb_accounts_belong_to_org(
        db, sel, org_id, require_active_slots=not account_rotation
    ):
        return RedirectResponse(f"/outreach/{campaign_id}/edit?err=accounts", status_code=303)
    if not sel:
        return RedirectResponse(f"/outreach/{campaign_id}/edit?err=no_senders", status_code=303)
    camp.fb_account_ids = sel
    _remap_queued_outreach_accounts_if_senders_changed(db, camp, sel, prev_sender_ids)

    donor_id = (form.get("donor_id") or "").strip()
    if not donor_id or not _outreach_donor_field_allowed(donor_id):
        return RedirectResponse(f"/outreach/{campaign_id}/edit?err=no_source", status_code=303)
    if not _batch_donor_scope_ok(donor_id, True):
        return RedirectResponse(f"/outreach/{campaign_id}/edit?err=batch_scope", status_code=303)
    if _is_import_batch_select(donor_id):
        bid = _import_batch_id_from_select(donor_id) or 0
        if bid <= 0 or not (
            db.query(ImportBatch)
            .filter(ImportBatch.id == bid, ImportBatch.organization_id == org_id)
            .first()
        ):
            return RedirectResponse(f"/outreach/{campaign_id}/edit?err=batch", status_code=303)
    camp.person_ids = []

    sw24, sw_s, sw_e = parse_send_window_form(form)
    record_any = _form_bool_last_wins(form, "record_contacted_after_any_dm_attempt", default=False)
    camp.config = _merge_config(
        camp.config if isinstance(camp.config, dict) else {},
        campaign_kind=campaign_kind,
        ui_donor_id=donor_id,
        language_filter=str(form.get("language_filter") or "all"),
        preset=str(form.get("preset") or "slow"),
        time_budget_minutes=str(form.get("time_budget_minutes") or ""),
        skip_contacted=_form_bool_last_wins(form, "skip_contacted", default=True),
        skip_contacted_scope=_parse_skip_contacted_scope(form),
        skip_previously_failed=_form_bool_last_wins(form, "skip_previously_failed", default=True),
        record_contacted_after_any_dm_attempt=record_any,
        like_first=bool(form.get("like_first")),
        add_friend_first=bool(form.get("add_friend_first")),
        react_reel_first=bool(form.get("react_reel_first")),
        like_mode=str(form.get("like_mode") or "first"),
        like_pool_size=str(form.get("like_pool_size") or "5"),
        like_count=str(form.get("like_count") or "1"),
        message_mode=str(form.get("message_mode") or "template"),
        ai_dm_example=str(form.get("ai_dm_example") or ""),
        ai_dm_hint=str(form.get("ai_dm_hint") or ""),
        ai_dm_use_profile_post=bool(form.get("ai_dm_use_profile_post")),
        ai_dm_provider=str(form.get("ai_dm_provider") or ""),
        comment_mode=str(form.get("comment_mode") or "template"),
        ai_comment_hint=str(form.get("ai_comment_hint") or ""),
        ai_comment_provider=str(form.get("ai_comment_provider") or ""),
        comment_post_mode=str(form.get("comment_post_mode") or "first"),
        comment_pool_size=str(form.get("comment_pool_size") or "5"),
        account_rotation=account_rotation,
        auto_slot_rotation=auto_slot_rotation and account_rotation,
        send_window_24h=sw24,
        send_window_start_hour=sw_s,
        send_window_end_hour=sw_e,
    )
    st = (form.get("status") or camp.status).strip()
    if st in ("draft", "active"):
        camp.status = st
    db.commit()
    return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/build-queue")
async def outreach_build_queue(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp or camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}" if camp else "/outreach", status_code=303)
    n, skipped, skipped_failed, why = _distribute_outreach_queue(db, camp, org_id=org_id)
    if n:
        camp.status = "ready"
    else:
        camp.status = "draft"
    db.commit()
    q = f"built={n}&skipped={skipped}&skipped_failed={skipped_failed}"
    if why:
        q += "&why=" + quote(why, safe="")
    return RedirectResponse(f"/outreach/{campaign_id}?{q}", status_code=303)


@router.post("/{campaign_id:int}/start")
async def outreach_start(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    admin_id = (request.session.get("user") or {}).get("id")

    ids = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
    proxy_blk = campaign_fb_account_ids_blocked_message(db, ids)
    if proxy_blk:
        return RedirectResponse(
            f"/outreach/{campaign_id}?err=proxy_tunnel&why={quote(proxy_blk, safe='')}",
            status_code=303,
        )
    busy_msg = account_automation_conflict_message(
        db,
        ids,
        ignore_outreach_campaign_id=camp.id,
    )
    if busy_msg:
        return RedirectResponse(
            f"/outreach/{campaign_id}?err=account_busy&why={quote(busy_msg, safe='')}",
            status_code=303,
        )

    _remap_queued_outreach_orphan_row_accounts(db, camp)

    if camp.status == "paused":
        camp.status = "running"
        db.commit()
        if camp.job_id:
            spawn_outreach_worker(camp.job_id)
        return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)

    if camp.status not in ("ready", "draft"):
        return RedirectResponse(f"/outreach/{campaign_id}?err=state", status_code=303)

    def _queued_outreach_rows() -> list:
        return (
            db.query(OutreachQueue)
            .filter(
                OutreachQueue.outreach_campaign_id == camp.id,
                OutreachQueue.status == "queued",
            )
            .all()
        )

    queued = _queued_outreach_rows()
    why_empty: str | None = None
    if not queued:
        # Очередь ещё не собирали — собираем сами (как «Собрать очередь»), затем запускаем.
        n_built, _sk, _skf, why_empty = _distribute_outreach_queue(db, camp, org_id=org_id)
        camp.status = "ready" if n_built else "draft"
        db.commit()
        queued = _queued_outreach_rows()

    if not queued:
        tail = ("&why=" + quote(why_empty, safe="")) if why_empty else ""
        return RedirectResponse(f"/outreach/{campaign_id}?err=no_queue{tail}", status_code=303)

    if outreach_worker_at_capacity():
        return RedirectResponse(f"/outreach/{campaign_id}?err=busy", status_code=303)

    if ai_agent_busy():
        return RedirectResponse(f"/outreach/{campaign_id}?err=ai_agent_busy", status_code=303)

    need_new_job = True
    if camp.job_id:
        old = db.get(Job, camp.job_id)
        if old and old.status == "running":
            return RedirectResponse(f"/outreach/{campaign_id}?err=job_running", status_code=303)
        if old and old.status == "queued":
            need_new_job = False
        elif old and old.status not in ("success", "failed", "cancelled"):
            need_new_job = False

    if need_new_job:
        job = create_job(
            db,
            organization_id=camp.organization_id,
            job_type=JOB_TYPE,
            config_snapshot={"campaign_id": camp.id, "name": camp.name},
            admin_id=admin_id,
        )
        camp.job_id = job.id
    job = db.get(Job, camp.job_id)
    if not job:
        return RedirectResponse(f"/outreach/{campaign_id}?err=no_job", status_code=303)

    for row in (
        db.query(OutreachQueue)
        .filter(
            OutreachQueue.outreach_campaign_id == camp.id,
            OutreachQueue.status == "queued",
        )
        .all()
    ):
        row.job_id = job.id
        person = db.get(Person, row.person_id)
        if person:
            row.message_text = _render_outreach_queue_body(db, camp, person) or None
        row.like_first = bool((camp.config or {}).get("like_first", False))
        row.add_friend_first = bool((camp.config or {}).get("add_friend_first", False)) and _outreach_campaign_kind(((camp.config or {}) if isinstance(camp.config, dict) else {}).get("campaign_kind")) == "dm"

    camp.status = "running"
    db.commit()
    spawn_outreach_worker(job.id)
    return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/pause")
async def outreach_pause(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp or camp.status != "running":
        return RedirectResponse(f"/outreach/{campaign_id}" if camp else "/outreach", status_code=303)
    camp.status = "paused"
    from backend.services.automation_auto_resume import strip_auto_resume_flag_from_config

    camp.config = strip_auto_resume_flag_from_config(
        camp.config if isinstance(camp.config, dict) else {}
    )
    db.commit()
    if camp.job_id:
        force_kill_outreach_playwright_for_job(int(camp.job_id))
    return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/delete")
async def outreach_delete(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}?err=running", status_code=303)
    jid = camp.job_id
    if jid:
        job = db.get(Job, jid)
        if job and job.job_type == JOB_TYPE and job.status in ("running", "queued"):
            finish_job(db, job, status="cancelled", error="Кампания удалена")
    _persist_contacted_from_dm_queue_before_campaign_delete(db, camp)
    db.query(OutreachQueue).filter(OutreachQueue.outreach_campaign_id == camp.id).delete()
    db.delete(camp)
    db.commit()
    release_outreach_worker_lock_for_job(jid)
    return RedirectResponse("/outreach", status_code=303)


@router.post("/{campaign_id:int}/retry-failed")
async def outreach_retry_failed(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp or camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}" if camp else "/outreach", status_code=303)
    for row in (
        db.query(OutreachQueue)
        .filter(
            OutreachQueue.outreach_campaign_id == camp.id,
            OutreachQueue.status == "failed",
        )
        .all()
    ):
        row.status = "queued"
        row.error = None
        row.processed_at = None
        row.job_id = None
        person = db.get(Person, row.person_id)
        if person:
            row.message_text = _render_outreach_queue_body(db, camp, person) or None
    camp.status = "ready"
    camp.job_id = None
    db.commit()
    return RedirectResponse(f"/outreach/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/forget-campaign-done")
async def outreach_forget_campaign_done(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
    person_id: int = Form(...),
):
    """Снять человека с учёта успешных отправок этой кампании (и глобально «уже писали» для аккаунтов кампании)."""
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}?err=running_forget", status_code=303)
    cfg = camp.config if isinstance(camp.config, dict) else {}
    done = _done_in_campaign_set(cfg)
    if int(person_id) not in done:
        return RedirectResponse(f"/outreach/{campaign_id}?err=not_in_done", status_code=303)
    _remove_person_from_campaign_done(db, camp, int(person_id))
    db.commit()
    return RedirectResponse(f"/outreach/{campaign_id}?forgot=1", status_code=303)


@router.post("/{campaign_id:int}/forget-campaign-done-bulk")
async def outreach_forget_campaign_done_bulk(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Снять с учёта успешных отправок несколько контактов (чекбоксы на карточке кампании)."""
    org_id = require_org_id(request, db)
    camp = _outreach_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/outreach", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/outreach/{campaign_id}?err=running_forget", status_code=303)
    form = await request.form()
    raw_ids: set[int] = set()
    for item in form.getlist("person_id"):
        try:
            raw_ids.add(int(item))
        except (TypeError, ValueError):
            continue
    if not raw_ids:
        return RedirectResponse(f"/outreach/{campaign_id}?err=forgot_none", status_code=303)
    cfg = camp.config if isinstance(camp.config, dict) else {}
    done = _done_in_campaign_set(cfg)
    to_remove = raw_ids & done
    if not to_remove:
        return RedirectResponse(f"/outreach/{campaign_id}?err=not_in_done", status_code=303)
    _remove_people_from_campaign_done(db, camp, to_remove)
    db.commit()
    n = len(to_remove)
    return RedirectResponse(f"/outreach/{campaign_id}?forgot={n}", status_code=303)
