"""Раздел «Режим агента»: пошаговая автоматизация взаимодействия с людьми."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, nullslast
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    CRMStage,
    Donor,
    FBAccount,
    ImportBatch,
    Person,
    SequenceCampaign,
    SequenceEnrollment,
    SequenceStep,
    Setting,
    Template,
    WarmupCampaign,
)
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.crm_stages_registry import workflow_label_for_stage
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key
from backend.services.job_logging import create_job
from backend.services.proxy_health_guard import campaign_fb_account_ids_blocked_message
from backend.services.tenancy import require_org_id
from backend.services.sequence_campaigns import (
    active_enrollment_count,
    agent_automation_settings,
    campaign_person_filter,
    enroll_next_batch,
    enrolled_person_ids_for_campaign,
    max_step_wait_value,
    normalize_step_wait_unit,
    remaining_person_ids_for_campaign,
    source_person_ids_for_campaign,
    step_wait_settings,
    unfinished_enrollment_count,
)
from backend.services.sequence_timezone import normalize_sequence_tz_name, raw_sequence_timezone_name
from backend.services.sequence_worker import (
    JOB_TYPE,
    count_due_enrollments,
    sequence_autotick_enabled,
    sequence_worker_at_capacity,
    sequence_worker_busy,
    sequence_worker_holds_campaign_job,
    spawn_sequence_worker,
)
from backend.services.person_ids_input import parse_person_ids_field
from backend.services.person_language import (
    LANGUAGE_FILTER_LABELS,
    language_filter_label,
    normalize_person_language_filter,
)
from backend.services.sending_window import merge_send_window_into_config, parse_send_window_form
from backend.services.throttle import PRESET_LABELS, is_valid_throttle_preset

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def _sequence_account_busy_message(db: Session, camp: SequenceCampaign) -> str | None:
    ids = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
    return account_automation_conflict_message(
        db,
        ids,
        ignore_sequence_campaign_id=camp.id,
    )


def _try_spawn_sequence_for_due(
    db: Session,
    camp: SequenceCampaign,
    *,
    admin_id: int | None,
) -> None:
    """Если есть готовые к выполнению шаги и воркер свободен — создаёт job и поднимает поток с Playwright."""
    if sequence_worker_at_capacity():
        return
    ids = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
    if campaign_fb_account_ids_blocked_message(db, ids):
        return
    if _sequence_account_busy_message(db, camp):
        return
    if count_due_enrollments(db, camp.id) <= 0:
        return
    job = create_job(
        db,
        organization_id=camp.organization_id,
        job_type=JOB_TYPE,
        config_snapshot={"campaign_id": camp.id, "manual": True},
        admin_id=admin_id,
    )
    db.commit()
    spawn_sequence_worker(job.id)

SCENARIO_STEP_SINGLE_TYPES = [
    ("like_post", "Лайк поста"),
    ("comment_post", "Комментарий к посту"),
    ("friend_request", "Заявка в друзья"),
    ("send_dm", "Личное сообщение"),
]

SCENARIO_STEP_COMBO_TYPES = [
    ("like_and_comment", "Лайк + комментарий"),
    ("like_comment_friend_request", "Лайк + комментарий + заявка в друзья"),
    ("like_comment_friend_request_send_dm", "Лайк + комментарий + заявка в друзья + личное сообщение"),
]

SCENARIO_STEP_TYPE_GROUPS = [
    ("Обычные действия", SCENARIO_STEP_SINGLE_TYPES),
    ("Комбо: несколько действий в одном шаге", SCENARIO_STEP_COMBO_TYPES),
]

SCENARIO_STEP_TYPES = SCENARIO_STEP_SINGLE_TYPES + SCENARIO_STEP_COMBO_TYPES

LIKE_ACTIONS = {
    "like_post",
    "like_and_comment",
    "like_comment_friend_request",
    "like_comment_friend_request_send_dm",
}
COMMENT_ACTIONS = {
    "comment_post",
    "like_and_comment",
    "like_comment_friend_request",
    "like_comment_friend_request_send_dm",
}
DM_ACTIONS = {
    "send_dm",
    "like_comment_friend_request_send_dm",
}
COMBO_DM_ACTIONS = {
    "like_comment_friend_request_send_dm",
}

STEP_TEMPLATE_ACTIONS = {"comment_post", "like_and_comment", "like_comment_friend_request", "like_comment_friend_request_send_dm", "send_dm"}
STEP_TEMPLATE_CATEGORIES = {"comment", "outreach", "warmup"}
STEP_TEMPLATE_CATEGORY_LABELS = {
    "comment": "Комментарии",
    "outreach": "Личные сообщения",
    "warmup": "Прогрев",
}

STEP_TEMPLATE_CATEGORY_BY_ACTION = {
    "comment_post": "comment",
    "like_and_comment": "comment",
    "like_comment_friend_request": "comment",
    "like_comment_friend_request_send_dm": "comment",
    "send_dm": "outreach",
}


def _person_ids_from_form(form, db: Session, org_id: int) -> list[int]:
    donor_id = (form.get("donor_id") or "").strip()
    extra = parse_person_ids_field(str(form.get("extra_person_ids") or ""))
    person_ids: list[int] = []
    if donor_id.isdigit():
        did = int(donor_id)
        if not (
            db.query(Donor)
            .filter(Donor.id == did, Donor.organization_id == org_id)
            .first()
        ):
            return []
        person_ids = [
            r[0]
            for r in db.query(Person.id)
            .filter(Person.donor_id == did, Person.organization_id == org_id)
            .order_by(Person.id)
            .all()
        ]
    person_ids.extend(extra)
    uniq = list(dict.fromkeys(person_ids))
    if not uniq:
        return []
    ok = {
        r[0]
        for r in db.query(Person.id)
        .filter(Person.id.in_(uniq), Person.organization_id == org_id)
        .all()
    }
    return [p for p in uniq if p in ok]


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
    return q.count() == len(uniq)


def _scenario_accounts_for_form(db: Session, org_id: int) -> list[FBAccount]:
    return (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .order_by(nullslast(FBAccount.active_slot.asc()), FBAccount.label.asc())
        .all()
    )


def _merge_sequence_rotation_config(
    existing: dict[str, Any] | None,
    *,
    account_rotation: bool,
    auto_slot_rotation: bool,
    sequence_actions_per_account: int,
) -> dict[str, Any]:
    base = dict(existing) if isinstance(existing, dict) else {}
    if account_rotation:
        base["account_rotation"] = True
        base["sequence_actions_per_account"] = max(
            1, min(int(sequence_actions_per_account), 500)
        )
        if auto_slot_rotation:
            base["auto_slot_rotation"] = True
        else:
            base.pop("auto_slot_rotation", None)
    else:
        base.pop("account_rotation", None)
        base.pop("auto_slot_rotation", None)
        base.pop("sequence_actions_per_account", None)
    return base


def _sequence_campaign_for_org(
    db: Session, campaign_id: int, org_id: int
) -> SequenceCampaign | None:
    return (
        db.query(SequenceCampaign)
        .filter(
            SequenceCampaign.id == campaign_id,
            SequenceCampaign.organization_id == org_id,
        )
        .first()
    )


def _int_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        if str(item).strip().isdigit():
            out.append(int(item))
    return list(dict.fromkeys(out))


def _bool_from_form(form, name: str) -> bool:
    return str(form.get(name) or "").strip().lower() in {"1", "true", "on", "yes"}


def _merge_like_post_config_from_form(form: Any, cfg: dict[str, Any]) -> None:
    """Настройки шага «Лайк поста»: только лайки по постам в ленте профиля."""
    cfg["like_scope"] = "post"
    for obsolete in ("comment_skip_likes", "comment_pool_size"):
        cfg.pop(obsolete, None)
    plm = str(form.get("post_like_mode") or "first").strip().lower()
    cfg["post_like_mode"] = plm if plm in ("first", "random") else "first"
    try:
        cfg["post_pool_size"] = max(1, min(int(form.get("post_pool_size") or 5), 10))
    except (TypeError, ValueError):
        cfg["post_pool_size"] = 5
    try:
        cfg["like_count"] = max(1, min(int(form.get("like_count_post") or 1), 10))
    except (TypeError, ValueError):
        cfg["like_count"] = 1
    cfg["post_skip_likes"] = 0


def _merge_comment_post_config_from_form(form: Any, cfg: dict[str, Any]) -> None:
    """ИИ-комментарий: без шаблона и ручного текста в config."""
    if not _bool_from_form(form, "ai_comment"):
        return
    cfg["ai_comment"] = True
    hint = (form.get("ai_comment_hint") or "").strip()[:2000]
    if hint:
        cfg["ai_comment_hint"] = hint
    cfg.pop("template_id", None)
    cfg.pop("message", None)


def _merge_comment_target_config_from_form(form: Any, cfg: dict[str, Any]) -> None:
    """Настройки выбора поста для комментария: первый сверху или случайно в пуле."""
    mode = str(form.get("comment_post_mode") or "first").strip().lower()
    cfg["comment_post_mode"] = mode if mode in ("first", "random") else "first"
    try:
        cfg["comment_pool_size"] = max(1, min(int(form.get("comment_pool_size") or 5), 10))
    except (TypeError, ValueError):
        cfg["comment_pool_size"] = 5


def _merge_send_dm_ai_config_from_form(form: Any, cfg: dict[str, Any]) -> None:
    """ИИ для ЛС: опорный пример (ai_dm_example), затем шаблон/поле шага; подсказка — только тон."""
    if not _bool_from_form(form, "ai_dm"):
        return
    cfg["ai_dm"] = True
    example = (form.get("ai_dm_example") or "").strip()[:6000]
    if example:
        cfg["ai_dm_example"] = example
    else:
        cfg.pop("ai_dm_example", None)
    hint = (form.get("ai_dm_hint") or "").strip()[:2000]
    if hint:
        cfg["ai_dm_hint"] = hint
    if not _bool_from_form(form, "ai_dm_use_profile_post"):
        cfg["ai_dm_use_profile_post"] = False


def _merge_combo_dm_config_from_form(form: Any, db: Session, cfg: dict[str, Any]) -> None:
    """Отдельный текст/шаблон ЛС для комбо-шага, где есть и комментарий, и личное сообщение."""
    cfg["dm_separate"] = True
    dm_message = (form.get("dm_message") or "").strip()
    if dm_message:
        cfg["dm_message"] = dm_message
    dm_template_id_raw = str(form.get("dm_template_id") or "").strip()
    if dm_template_id_raw.isdigit():
        dm_template_id = int(dm_template_id_raw)
        if db.get(Template, dm_template_id):
            cfg["dm_template_id"] = dm_template_id


def _parse_scenario_step_form(form: Any, db: Session) -> tuple[str, int, str, dict[str, Any], int]:
    """Разбор полей формы добавления/редактирования шага сценария."""
    try:
        day_offset = int(form.get("day_offset") or 0)
    except (TypeError, ValueError):
        day_offset = 0
    day_offset = max(0, min(day_offset, 3650))
    action_type = (form.get("action_type") or "like_post").strip()
    if not any(action_type == a for a, _ in SCENARIO_STEP_TYPES):
        action_type = "like_post"
    wait_unit = normalize_step_wait_unit(str(form.get("wait_unit") or ""))
    try:
        wait_value = int(form.get("wait_value") or 0)
    except (TypeError, ValueError):
        wait_value = 0
    wait_value = max(0, min(wait_value, max_step_wait_value(wait_unit)))
    message = (form.get("step_message") or "").strip()
    template_id_raw = str(form.get("template_id") or "").strip()
    cfg: dict[str, Any] = {}
    if message:
        cfg["message"] = message
    if action_type in STEP_TEMPLATE_ACTIONS and template_id_raw.isdigit():
        template_id = int(template_id_raw)
        if db.get(Template, template_id):
            cfg["template_id"] = template_id
    cfg["wait_value"] = wait_value
    cfg["wait_unit"] = wait_unit
    if action_type in LIKE_ACTIONS:
        _merge_like_post_config_from_form(form, cfg)
    if action_type in COMMENT_ACTIONS:
        _merge_comment_target_config_from_form(form, cfg)
        _merge_comment_post_config_from_form(form, cfg)
    if action_type in DM_ACTIONS:
        _merge_send_dm_ai_config_from_form(form, cfg)
    if action_type in COMBO_DM_ACTIONS:
        _merge_combo_dm_config_from_form(form, db, cfg)
    return action_type, wait_value, wait_unit, cfg, day_offset


def _step_editor_client_dict(step: SequenceStep) -> dict[str, Any]:
    """Данные шага для заполнения формы в браузере (JSON)."""
    cfg = step.config if isinstance(step.config, dict) else {}
    wv, wu = step_wait_settings(step)
    tid = cfg.get("template_id")
    template_id: int | None
    if tid is not None and str(tid).strip().isdigit():
        template_id = int(str(tid).strip())
    else:
        template_id = None
    msg = cfg.get("message")
    message_str = msg.strip() if isinstance(msg, str) else ""
    dm_tid = cfg.get("dm_template_id")
    dm_template_id: int | None
    if dm_tid is not None and str(dm_tid).strip().isdigit():
        dm_template_id = int(str(dm_tid).strip())
    else:
        dm_template_id = None
    dm_msg = cfg.get("dm_message")
    dm_message_str = dm_msg.strip() if isinstance(dm_msg, str) else ""
    try:
        pool_sz = max(1, min(int(cfg.get("post_pool_size") or 5), 10))
    except (TypeError, ValueError):
        pool_sz = 5
    try:
        like_c = max(1, min(int(cfg.get("like_count") or 1), 10))
    except (TypeError, ValueError):
        like_c = 1
    try:
        comment_pool_sz = max(1, min(int(cfg.get("comment_pool_size") or 5), 10))
    except (TypeError, ValueError):
        comment_pool_sz = 5
    plm = str(cfg.get("post_like_mode") or "first").strip().lower()
    if plm not in ("first", "random"):
        plm = "first"
    cpm = str(cfg.get("comment_post_mode") or "first").strip().lower()
    if cpm not in ("first", "random"):
        cpm = "first"
    return {
        "id": step.id,
        "order": step.step_index + 1,
        "action_type": step.action_type,
        "wait_value": wv,
        "wait_unit": wu,
        "template_id": template_id,
        "message": message_str,
        "dm_template_id": dm_template_id,
        "dm_message": dm_message_str,
        "post_like_mode": plm,
        "post_pool_size": pool_sz,
        "like_count": like_c,
        "comment_post_mode": cpm,
        "comment_pool_size": comment_pool_sz,
        "ai_comment": bool(cfg.get("ai_comment")),
        "ai_comment_hint": (cfg.get("ai_comment_hint") or "") if isinstance(cfg.get("ai_comment_hint"), str) else "",
        "ai_dm": bool(cfg.get("ai_dm")),
        "ai_dm_example": (cfg.get("ai_dm_example") or "") if isinstance(cfg.get("ai_dm_example"), str) else "",
        "ai_dm_hint": (cfg.get("ai_dm_hint") or "") if isinstance(cfg.get("ai_dm_hint"), str) else "",
        "ai_dm_use_profile_post": cfg.get("ai_dm_use_profile_post") is not False,
    }


def _manual_import_batches_for_agent(db: Session, org_id: int) -> list[ImportBatch]:
    """Партии ручного импорта CSV/XLSX (раздел «Импорт базы»), без parser:…"""
    return (
        db.query(ImportBatch)
        .filter(
            ImportBatch.organization_id == org_id,
            ~ImportBatch.filename.startswith("parser:"),
        )
        .order_by(ImportBatch.created_at.desc())
        .limit(120)
        .all()
    )


def _merge_person_filter(
    existing: dict[str, Any] | None,
    db: Session,
    *,
    org_id: int,
    ui_donor_id: str,
    person_source: str = "donor",
    import_batch_id: str = "",
    import_batch_in_form: bool = True,
    preset: str,
    time_budget_seconds: str,
    extra_person_ids: list[int] | None = None,
    crm_stage: str = "",
    language_filter: str = "all",
    auto_batch_enabled: bool = False,
    batch_size: str = "",
    batch_wait_value: str = "",
    batch_wait_unit: str = "days",
) -> dict[str, Any]:
    base = dict(existing) if isinstance(existing, dict) else {}
    src = (person_source or "donor").strip().lower()
    if src == "all":
        base["all_people"] = True
        base.pop("ui_donor_id", None)
        base.pop("ui_import_batch_id", None)
    elif src == "import_batch":
        base.pop("all_people", None)
        base.pop("ui_donor_id", None)
        ib = (import_batch_id or "").strip()
        if ib.isdigit() and (
            db.query(ImportBatch)
            .filter(ImportBatch.id == int(ib), ImportBatch.organization_id == org_id)
            .first()
        ):
            base["ui_import_batch_id"] = int(ib)
        elif not import_batch_in_form and isinstance(existing, dict):
            prev = existing.get("ui_import_batch_id")
            ps = str(prev).strip() if prev is not None else ""
            if ps.isdigit() and (
                db.query(ImportBatch)
                .filter(ImportBatch.id == int(ps), ImportBatch.organization_id == org_id)
                .first()
            ):
                base["ui_import_batch_id"] = int(ps)
            else:
                base.pop("ui_import_batch_id", None)
        else:
            base.pop("ui_import_batch_id", None)
    else:
        base.pop("all_people", None)
        base.pop("ui_import_batch_id", None)
        if ui_donor_id.strip().isdigit():
            did = int(ui_donor_id.strip())
            if (
                db.query(Donor)
                .filter(Donor.id == did, Donor.organization_id == org_id)
                .first()
            ):
                base["ui_donor_id"] = did
            elif "ui_donor_id" in base:
                del base["ui_donor_id"]
        elif "ui_donor_id" in base:
            del base["ui_donor_id"]
    if extra_person_ids:
        cand: list[int] = []
        for x in extra_person_ids:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v > 0:
                cand.append(v)
        cand = list(dict.fromkeys(cand))
        if cand:
            ok = {
                r[0]
                for r in db.query(Person.id)
                .filter(Person.id.in_(cand), Person.organization_id == org_id)
                .all()
            }
            base["extra_person_ids"] = [i for i in cand if i in ok]
        else:
            base["extra_person_ids"] = []
    elif "extra_person_ids" in base:
        del base["extra_person_ids"]
    stage_slug = (crm_stage or "").strip()
    if stage_slug:
        base["crm_stage"] = stage_slug
    elif "crm_stage" in base:
        del base["crm_stage"]
    lang = normalize_person_language_filter(language_filter)
    if lang != "all":
        base["language_filter"] = lang
    elif "language_filter" in base:
        del base["language_filter"]
    base["preset"] = preset if is_valid_throttle_preset(preset) else "slow"
    base["auto_batch_enabled"] = bool(auto_batch_enabled)
    base["batch_size"] = max(1, min(int(batch_size or 20), 1000)) if str(batch_size or "").strip().isdigit() else 20
    raw_bwu = str(batch_wait_unit or "").strip().lower()
    if raw_bwu in {"minute", "minutes", "min", "mins"}:
        base["batch_wait_unit"] = "minutes"
    elif raw_bwu in {"hour", "hours", "hr", "hrs"}:
        base["batch_wait_unit"] = "hours"
    else:
        base["batch_wait_unit"] = "days"
    if base["batch_wait_unit"] == "minutes":
        max_wait = 60 * 24 * 365
    elif base["batch_wait_unit"] == "hours":
        max_wait = 24 * 365
    else:
        max_wait = 3650

    def _default_batch_wait() -> int:
        if base["batch_wait_unit"] == "minutes":
            return 30
        if base["batch_wait_unit"] == "hours":
            return 24
        return 1

    if str(batch_wait_value or "").strip():
        try:
            base["batch_wait_value"] = max(0, min(int(batch_wait_value), max_wait))
        except ValueError:
            base["batch_wait_value"] = _default_batch_wait()
    elif "batch_wait_value" not in base:
        base["batch_wait_value"] = _default_batch_wait()
    tb_raw = (time_budget_seconds or "").strip()
    if tb_raw:
        try:
            v = float(tb_raw.replace(",", "."))
            if v > 0:
                base["time_budget_seconds"] = v
            elif "time_budget_seconds" in base:
                del base["time_budget_seconds"]
        except ValueError:
            pass
    elif "time_budget_seconds" in base:
        del base["time_budget_seconds"]
    return base


def _sequence_templates(db: Session, org_id: int) -> list[Template]:
    return (
        db.query(Template)
        .filter(
            Template.organization_id == org_id,
            Template.category.in_(tuple(STEP_TEMPLATE_CATEGORIES)),
        )
        .order_by(Template.category.asc(), Template.name.asc())
        .all()
    )


def _crm_stage_options(db: Session) -> list[tuple[str, str]]:
    rows = db.query(CRMStage).order_by(CRMStage.sort_order.asc(), CRMStage.id.asc()).all()
    return [(row.slug, workflow_label_for_stage(row.slug, row.label)) for row in rows]


def _template_category_for_action(action_type: str) -> str | None:
    return STEP_TEMPLATE_CATEGORY_BY_ACTION.get((action_type or "").strip())


@router.get("")
async def scenarios_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    items = (
        db.query(SequenceCampaign)
        .filter(SequenceCampaign.organization_id == org_id)
        .order_by(SequenceCampaign.id.desc())
        .limit(50)
        .all()
    )
    legacy_warmups = (
        db.query(func.count())
        .select_from(WarmupCampaign)
        .filter(WarmupCampaign.organization_id == org_id)
        .scalar()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "scenarios/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "scenarios",
            "campaigns": items,
            "sequence_busy": sequence_worker_busy(),
            "legacy_warmups_count": int(legacy_warmups or 0),
            "language_filter_label": language_filter_label,
        },
    )


@router.get("/new")
async def scenarios_new_form(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    accounts = _scenario_accounts_for_form(db, org_id)
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    crm_stages = _crm_stage_options(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "scenarios/campaign_form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "scenarios",
            "campaign": None,
            "accounts": accounts,
            "donors": donors,
            "import_batches": _manual_import_batches_for_agent(db, org_id),
            "preset_labels": PRESET_LABELS,
            "crm_stages": crm_stages,
            "language_filter_labels": LANGUAGE_FILTER_LABELS,
        },
    )


@router.post("/new")
async def scenarios_new_save(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    name = (form.get("name") or "").strip() or "Режим без названия"
    sel = [
        int(x)
        for x in form.getlist("fb_account_id")
        if str(x).strip().isdigit()
    ]
    sel = list(dict.fromkeys(sel))
    account_rotation = form.get("account_rotation") == "1"
    auto_slot_rotation = form.get("auto_slot_rotation") == "1"
    try:
        seq_cap = max(1, min(int(form.get("sequence_actions_per_account") or 50), 500))
    except (TypeError, ValueError):
        seq_cap = 50
    if not _fb_accounts_belong_to_org(
        db, sel, org_id, require_active_slots=not account_rotation
    ):
        return RedirectResponse("/scenarios/new?err=accounts", status_code=303)
    extra_person_ids = parse_person_ids_field(str(form.get("extra_person_ids") or ""))
    form_keys = set(form.keys())
    pf = _merge_person_filter(
        None,
        db,
        org_id=org_id,
        ui_donor_id=(form.get("donor_id") or ""),
        person_source=str(form.get("person_source") or "donor"),
        import_batch_id=str(form.get("import_batch_id") or ""),
        import_batch_in_form="import_batch_id" in form_keys,
        preset=str(form.get("preset") or "slow"),
        time_budget_seconds=str(form.get("time_budget_seconds") or ""),
        extra_person_ids=extra_person_ids,
        crm_stage=str(form.get("crm_stage") or ""),
        language_filter=str(form.get("language_filter") or "all"),
        auto_batch_enabled=_bool_from_form(form, "auto_batch_enabled"),
        batch_size=str(form.get("batch_size") or ""),
        batch_wait_value=str(form.get("batch_wait_value") or ""),
        batch_wait_unit=str(form.get("batch_wait_unit") or "days"),
    )
    status = (form.get("status") or "draft").strip()
    if status not in ("draft", "active"):
        status = "draft"

    sw24, sw_s, sw_e = parse_send_window_form(form)
    cfg0 = _merge_sequence_rotation_config(
        None,
        account_rotation=account_rotation,
        auto_slot_rotation=auto_slot_rotation,
        sequence_actions_per_account=seq_cap,
    )
    cfg0 = merge_send_window_into_config(
        cfg0,
        send_window_24h=sw24,
        send_window_start_hour=sw_s,
        send_window_end_hour=sw_e,
    )
    camp = SequenceCampaign(
        organization_id=org_id,
        name=name,
        status=status,
        fb_account_ids=sel,
        person_filter=pf,
        config=cfg0,
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return RedirectResponse(f"/scenarios/{camp.id}", status_code=303)


@router.get("/{campaign_id:int}")
async def scenarios_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = (
        db.query(SequenceCampaign)
        .filter(
            SequenceCampaign.id == campaign_id,
            SequenceCampaign.organization_id == org_id,
        )
        .first()
    )
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    steps = (
        db.query(SequenceStep)
        .filter(SequenceStep.sequence_id == camp.id)
        .order_by(SequenceStep.step_index.asc(), SequenceStep.id.asc())
        .all()
    )
    enroll_rows = (
        db.query(SequenceEnrollment)
        .filter(SequenceEnrollment.sequence_id == camp.id)
        .order_by(SequenceEnrollment.id.desc())
        .limit(200)
        .all()
    )
    pids = list({e.person_id for e in enroll_rows})
    aids = list({e.fb_account_id for e in enroll_rows if e.fb_account_id})
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

    cnt_en = dict(
        db.query(SequenceEnrollment.status, func.count(SequenceEnrollment.id))
        .filter(SequenceEnrollment.sequence_id == camp.id)
        .group_by(SequenceEnrollment.status)
        .all()
    )
    due_n = count_due_enrollments(db, camp.id)
    pf = campaign_person_filter(camp)
    autotick = sequence_autotick_enabled(db)
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    message_templates = _sequence_templates(db, org_id)
    template_map = {t.id: t for t in message_templates}
    crm_stages = _crm_stage_options(db)
    automation = agent_automation_settings(camp)
    active_n = active_enrollment_count(db, camp.id)
    unfinished_n = unfinished_enrollment_count(db, camp.id)
    source_ids = remaining_person_ids_for_campaign(db, camp)
    all_source_total = len(source_person_ids_for_campaign(db, camp))
    import_batch_filename: str | None = None
    ib_raw = pf.get("ui_import_batch_id")
    if ib_raw is not None and str(ib_raw).strip().isdigit():
        ib_row = (
            db.query(ImportBatch)
            .filter(
                ImportBatch.id == int(str(ib_raw).strip()),
                ImportBatch.organization_id == org_id,
            )
            .first()
        )
        if ib_row:
            import_batch_filename = ib_row.filename

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "scenarios/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "scenarios",
            "campaign": camp,
            "donors": donors,
            "import_batch_filename": import_batch_filename,
            "pf": pf,
            "steps": steps,
            "step_types": SCENARIO_STEP_TYPES,
            "step_type_groups": SCENARIO_STEP_TYPE_GROUPS,
            "step_labels": dict(SCENARIO_STEP_TYPES),
            "step_wait_settings": step_wait_settings,
            "template_category_for_action": _template_category_for_action,
            "template_category_labels": STEP_TEMPLATE_CATEGORY_LABELS,
            "enroll_rows": enroll_rows,
            "people": people,
            "accs": accs,
            "cnt_en": cnt_en,
            "due_n": due_n,
            "sequence_busy": sequence_worker_busy(),
            "autotick": autotick,
            "preset_labels": PRESET_LABELS,
            "sequence_timezone_effective": normalize_sequence_tz_name(raw_sequence_timezone_name(db)),
            "message_templates": message_templates,
            "template_map": template_map,
            "crm_stages": crm_stages,
            "language_filter_label": language_filter_label,
            "automation": automation,
            "active_enrollment_count": active_n,
            "unfinished_enrollment_count": unfinished_n,
            "remaining_source_count": len(source_ids),
            "source_total_count": all_source_total,
            "has_openai": bool((effective_openai_api_key(db) or "").strip()),
            "has_gemini": bool((effective_google_llm_api_key(db) or "").strip()),
            "scenario_steps_editor_data": [_step_editor_client_dict(s) for s in steps],
        },
    )


@router.get("/{campaign_id:int}/edit")
async def scenarios_edit_form(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = (
        db.query(SequenceCampaign)
        .filter(
            SequenceCampaign.id == campaign_id,
            SequenceCampaign.organization_id == org_id,
        )
        .first()
    )
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    accounts = _scenario_accounts_for_form(db, org_id)
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    crm_stages = _crm_stage_options(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "scenarios/campaign_form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "scenarios",
            "campaign": camp,
            "accounts": accounts,
            "donors": donors,
            "import_batches": _manual_import_batches_for_agent(db, org_id),
            "preset_labels": PRESET_LABELS,
            "crm_stages": crm_stages,
            "language_filter_labels": LANGUAGE_FILTER_LABELS,
        },
    )


@router.post("/{campaign_id:int}/edit")
async def scenarios_edit_save(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    form = await request.form()
    camp.name = (form.get("name") or "").strip() or camp.name
    sel = [
        int(x)
        for x in form.getlist("fb_account_id")
        if str(x).strip().isdigit()
    ]
    sel = list(dict.fromkeys(sel))
    account_rotation = form.get("account_rotation") == "1"
    auto_slot_rotation = form.get("auto_slot_rotation") == "1"
    try:
        seq_cap = max(1, min(int(form.get("sequence_actions_per_account") or 50), 500))
    except (TypeError, ValueError):
        seq_cap = 50
    if not _fb_accounts_belong_to_org(
        db, sel, org_id, require_active_slots=not account_rotation
    ):
        return RedirectResponse(f"/scenarios/{campaign_id}/edit?err=accounts", status_code=303)
    camp.fb_account_ids = sel
    sw24, sw_s, sw_e = parse_send_window_form(form)
    cfg0 = _merge_sequence_rotation_config(
        camp.config if isinstance(camp.config, dict) else {},
        account_rotation=account_rotation,
        auto_slot_rotation=auto_slot_rotation,
        sequence_actions_per_account=seq_cap,
    )
    camp.config = merge_send_window_into_config(
        cfg0,
        send_window_24h=sw24,
        send_window_start_hour=sw_s,
        send_window_end_hour=sw_e,
    )
    extra_person_ids = parse_person_ids_field(str(form.get("extra_person_ids") or ""))
    form_keys = set(form.keys())
    camp.person_filter = _merge_person_filter(
        camp.person_filter if isinstance(camp.person_filter, dict) else {},
        db,
        org_id=org_id,
        ui_donor_id=(form.get("donor_id") or ""),
        person_source=str(form.get("person_source") or "donor"),
        import_batch_id=str(form.get("import_batch_id") or ""),
        import_batch_in_form="import_batch_id" in form_keys,
        preset=str(form.get("preset") or "slow"),
        time_budget_seconds=str(form.get("time_budget_seconds") or ""),
        extra_person_ids=extra_person_ids,
        crm_stage=str(form.get("crm_stage") or ""),
        language_filter=str(form.get("language_filter") or "all"),
        auto_batch_enabled=_bool_from_form(form, "auto_batch_enabled"),
        batch_size=str(form.get("batch_size") or ""),
        batch_wait_value=str(form.get("batch_wait_value") or ""),
        batch_wait_unit=str(form.get("batch_wait_unit") or "days"),
    )
    status = (form.get("status") or camp.status).strip()
    if status in ("draft", "active", "paused"):
        camp.status = status
    db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/step/add")
async def scenarios_step_add(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    form = await request.form()
    action_type, wait_value, wait_unit, cfg, day_offset = _parse_scenario_step_form(form, db)

    max_idx = (
        db.query(func.max(SequenceStep.step_index))
        .filter(SequenceStep.sequence_id == camp.id)
        .scalar()
    )
    next_idx = (max_idx if max_idx is not None else -1) + 1

    row = SequenceStep(
        sequence_id=camp.id,
        step_index=next_idx,
        day_offset=wait_value if wait_unit == "days" else day_offset,
        action_type=action_type,
        config=cfg or None,
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        logger.exception("scenarios_step_add failed campaign_id=%s", campaign_id)
        db.rollback()
        return RedirectResponse(f"/scenarios/{campaign_id}?err=step_save#scenario-chain", status_code=303)
    return RedirectResponse(f"/scenarios/{campaign_id}#scenario-chain", status_code=303)


@router.post("/{campaign_id:int}/step/{step_id:int}/update")
async def scenarios_step_update(request: Request, campaign_id: int, step_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    row = db.get(SequenceStep, step_id)
    if not camp or not row or row.sequence_id != campaign_id:
        return RedirectResponse(f"/scenarios/{campaign_id}#scenario-chain", status_code=303)
    form = await request.form()
    action_type, wait_value, wait_unit, cfg, day_offset = _parse_scenario_step_form(form, db)
    row.action_type = action_type
    row.config = cfg or None
    row.day_offset = wait_value if wait_unit == "days" else day_offset
    try:
        db.commit()
    except Exception:
        logger.exception("scenarios_step_update failed campaign_id=%s step_id=%s", campaign_id, step_id)
        db.rollback()
        return RedirectResponse(f"/scenarios/{campaign_id}?err=step_save#scenario-chain", status_code=303)
    return RedirectResponse(f"/scenarios/{campaign_id}#scenario-chain", status_code=303)


@router.post("/{campaign_id:int}/step/{step_id:int}/delete")
async def scenarios_step_delete(
    request: Request, campaign_id: int, step_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    if not _sequence_campaign_for_org(db, campaign_id, org_id):
        return RedirectResponse("/scenarios", status_code=303)
    row = db.get(SequenceStep, step_id)
    if not row or row.sequence_id != campaign_id:
        return RedirectResponse(f"/scenarios/{campaign_id}#scenario-chain", status_code=303)
    db.delete(row)
    db.commit()
    steps = (
        db.query(SequenceStep)
        .filter(SequenceStep.sequence_id == campaign_id)
        .order_by(SequenceStep.step_index.asc(), SequenceStep.id.asc())
        .all()
    )
    for i, s in enumerate(steps):
        s.step_index = i
    db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}#scenario-chain", status_code=303)


@router.post("/{campaign_id:int}/enroll")
async def scenarios_enroll(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    if camp.status == "paused":
        return RedirectResponse(f"/scenarios/{campaign_id}?err=paused", status_code=303)
    form = await request.form()
    person_ids = _person_ids_from_form(form, db, org_id)
    if not person_ids:
        person_ids = list(dict.fromkeys(remaining_person_ids_for_campaign(db, camp) + list(enrolled_person_ids_for_campaign(db, camp.id))))
    if not person_ids:
        person_ids = list(dict.fromkeys(source_person_ids_for_campaign(db, camp)))
    acc_ids = list(camp.fb_account_ids or [])
    if not person_ids:
        logger.warning(
            "scenarios_enroll no_people campaign_id=%s person_filter=%r source_n_would_be=%s",
            campaign_id,
            camp.person_filter,
            len(source_person_ids_for_campaign(db, camp)),
        )
        return RedirectResponse(f"/scenarios/{campaign_id}?err=no_people", status_code=303)

    db.query(SequenceEnrollment).filter(SequenceEnrollment.sequence_id == camp.id).delete()
    db.commit()

    for i, pid in enumerate(person_ids):
        aid = acc_ids[i % len(acc_ids)] if acc_ids else None
        db.add(
            SequenceEnrollment(
                sequence_id=camp.id,
                person_id=pid,
                fb_account_id=aid,
                current_step_index=0,
                status="active",
            )
        )
    db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}?enrolled={len(person_ids)}", status_code=303)


@router.post("/{campaign_id:int}/automation/start")
async def scenarios_automation_start(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    busy_msg = _sequence_account_busy_message(db, camp)
    if busy_msg:
        return RedirectResponse(
            f"/scenarios/{campaign_id}?err=account_busy&why={quote(busy_msg, safe='')}",
            status_code=303,
        )
    if camp.status == "paused":
        return RedirectResponse(f"/scenarios/{campaign_id}?err=paused", status_code=303)
    if camp.status != "active":
        camp.status = "active"
        db.commit()
    seeded = enroll_next_batch(db, camp, force=True)
    return RedirectResponse(f"/scenarios/{campaign_id}?seeded={seeded}", status_code=303)


@router.post("/{campaign_id:int}/pause")
async def scenarios_pause(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    if camp.status == "active":
        camp.status = "paused"
        db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}?paused=1", status_code=303)


@router.post("/{campaign_id:int}/resume")
async def scenarios_resume(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    busy_msg = _sequence_account_busy_message(db, camp)
    if busy_msg:
        return RedirectResponse(
            f"/scenarios/{campaign_id}?err=account_busy&why={quote(busy_msg, safe='')}",
            status_code=303,
        )
    if camp.status == "paused":
        camp.status = "active"
        db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}?resumed=1", status_code=303)


@router.post("/{campaign_id:int}/full-autopilot/start")
async def scenarios_full_autopilot_start(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    """Включить авто-партии, глобальный автозапуск, активировать режим, первая партия + старт воркера при наличии шагов."""
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    busy_msg = _sequence_account_busy_message(db, camp)
    if busy_msg:
        return RedirectResponse(
            f"/scenarios/{campaign_id}?err=account_busy&why={quote(busy_msg, safe='')}",
            status_code=303,
        )
    ids0 = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
    proxy_blk = campaign_fb_account_ids_blocked_message(db, ids0)
    if proxy_blk:
        return RedirectResponse(
            f"/scenarios/{campaign_id}?err=proxy_tunnel&why={quote(proxy_blk, safe='')}",
            status_code=303,
        )
    pf = dict(campaign_person_filter(camp))
    pf["auto_batch_enabled"] = True
    camp.person_filter = pf
    camp.status = "active"
    row = db.get(Setting, "sequence_autotick")
    if row:
        row.value = True
    else:
        db.add(Setting(key="sequence_autotick", value=True))
    db.commit()
    db.refresh(camp)
    seeded = enroll_next_batch(db, camp, force=True)
    admin_id = (request.session.get("user") or {}).get("id")
    _try_spawn_sequence_for_due(db, camp, admin_id=admin_id)
    return RedirectResponse(
        f"/scenarios/{campaign_id}?full_autopilot=1&seeded={seeded}",
        status_code=303,
    )


@router.post("/{campaign_id:int}/automation/next-batch")
async def scenarios_next_batch(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    if camp.status == "paused":
        return RedirectResponse(f"/scenarios/{campaign_id}?err=paused", status_code=303)
    if camp.status != "active":
        return RedirectResponse(f"/scenarios/{campaign_id}?err=not_active", status_code=303)
    seeded = enroll_next_batch(db, camp, force=True)
    return RedirectResponse(f"/scenarios/{campaign_id}?seeded={seeded}", status_code=303)


@router.post("/{campaign_id:int}/automation/reset")
async def scenarios_automation_reset(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    if sequence_worker_holds_campaign_job(db, camp.id):
        return RedirectResponse(f"/scenarios/{campaign_id}?err=busy", status_code=303)
    db.query(SequenceEnrollment).filter(SequenceEnrollment.sequence_id == camp.id).delete()
    pf = campaign_person_filter(camp)
    pf.pop("last_batch_at", None)
    camp.person_filter = pf
    db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}?reset=1", status_code=303)


@router.post("/{campaign_id:int}/run-due")
async def scenarios_run_due(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    if camp.status == "paused":
        return RedirectResponse(f"/scenarios/{campaign_id}?err=paused", status_code=303)
    if camp.status != "active":
        return RedirectResponse(f"/scenarios/{campaign_id}?err=not_active", status_code=303)
    due_now = count_due_enrollments(db, camp.id)
    if due_now <= 0:
        enroll_next_batch(db, camp, force=False)
    due_now = count_due_enrollments(db, camp.id)
    if due_now <= 0:
        if unfinished_enrollment_count(db, camp.id) > 0:
            return RedirectResponse(f"/scenarios/{campaign_id}?err=blocked", status_code=303)
        return RedirectResponse(f"/scenarios/{campaign_id}?err=nothing_due", status_code=303)
    busy_msg = _sequence_account_busy_message(db, camp)
    if busy_msg:
        return RedirectResponse(
            f"/scenarios/{campaign_id}?err=account_busy&why={quote(busy_msg, safe='')}",
            status_code=303,
        )
    if sequence_worker_at_capacity():
        return RedirectResponse(f"/scenarios/{campaign_id}?err=busy", status_code=303)

    ids1 = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
    proxy_blk = campaign_fb_account_ids_blocked_message(db, ids1)
    if proxy_blk:
        return RedirectResponse(
            f"/scenarios/{campaign_id}?err=proxy_tunnel&why={quote(proxy_blk, safe='')}",
            status_code=303,
        )

    admin_id = (request.session.get("user") or {}).get("id")
    _try_spawn_sequence_for_due(db, camp, admin_id=admin_id)
    return RedirectResponse(f"/scenarios/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/autotick")
async def scenarios_autotick_save(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    if not _sequence_campaign_for_org(db, campaign_id, org_id):
        return RedirectResponse("/scenarios", status_code=303)
    form = await request.form()
    on = bool(form.get("sequence_autotick"))
    row = db.get(Setting, "sequence_autotick")
    if row:
        row.value = on
    else:
        db.add(Setting(key="sequence_autotick", value=on))
    db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/delete")
async def scenarios_delete(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    camp = _sequence_campaign_for_org(db, campaign_id, org_id)
    if not camp:
        return RedirectResponse("/scenarios", status_code=303)
    if sequence_worker_holds_campaign_job(db, camp.id):
        return RedirectResponse(f"/scenarios/{campaign_id}?err=busy", status_code=303)
    db.query(SequenceEnrollment).filter(SequenceEnrollment.sequence_id == camp.id).delete()
    db.query(SequenceStep).filter(SequenceStep.sequence_id == camp.id).delete()
    db.delete(camp)
    db.commit()
    return RedirectResponse("/scenarios", status_code=303)


@router.post("/{campaign_id:int}/enrollment/{enrollment_id:int}/reset")
async def scenarios_enrollment_reset(
    request: Request, campaign_id: int, enrollment_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    if not _sequence_campaign_for_org(db, campaign_id, org_id):
        return RedirectResponse("/scenarios", status_code=303)
    row = db.get(SequenceEnrollment, enrollment_id)
    if not row or row.sequence_id != campaign_id:
        return RedirectResponse(f"/scenarios/{campaign_id}", status_code=303)
    row.current_step_index = 0
    row.status = "active"
    row.error = None
    row.last_run_at = None
    db.commit()
    return RedirectResponse(f"/scenarios/{campaign_id}", status_code=303)
