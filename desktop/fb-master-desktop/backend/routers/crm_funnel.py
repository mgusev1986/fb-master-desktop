"""Воронка CRM: канбан по стадиям, настройка стадий, смена стадии, экспорт CSV."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import CRMStage, Conversation, Donor, Person
from backend.services.contacted_registry import engaged_person_ids_subquery
from backend.services.content_disposition import attachment_content_disposition
from backend.services.crm_stages_registry import (
    get_ordered_stages,
    icon_class_for_crm_stage_slug,
    next_sort_order,
    seed_builtin_stages,
    stage_labels_map,
    suggest_slug,
    workflow_label_for_stage,
)
from backend.services.messenger_person_link import (
    build_messenger_person_id_index,
    relink_conversations_missing_person,
)
from backend.services.tenancy import require_org_id
from backend.services.active_fb_account import get_active_fb_account_id
from backend.services.outreach_shared_profile import messenger_blocked_by_outreach

router = APIRouter(tags=["crm_funnel"])

CARD_LIMIT_PER_COLUMN = 500

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")


def _parse_donor_id(raw: str | None) -> int | None:
    if not raw or not str(raw).strip().isdigit():
        return None
    return int(str(raw).strip())


def _funnel_query_params(
    *,
    q: str,
    donor_id: int | None,
    view: str,
    tab: str,
) -> str:
    p: dict[str, str] = {}
    if q:
        p["q"] = q
    if donor_id is not None:
        p["donor_id"] = str(donor_id)
    if view and view != "kanban":
        p["view"] = view
    if view == "tabs" and tab:
        p["tab"] = tab
    return "?" + urlencode(p) if p else ""


def _redirect_funnel(
    *,
    q: str,
    donor_id: int | None,
    view: str,
    tab: str,
    notice: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    base = "/crm/funnel" + _funnel_query_params(q=q, donor_id=donor_id, view=view, tab=tab)
    extra: dict[str, str] = {}
    if notice:
        extra["stages_notice"] = notice
    if error:
        extra["stages_error"] = error
    if extra:
        sep = "&" if "?" in base else "?"
        base += sep + urlencode(extra)
    return RedirectResponse(base, status_code=303)


def _apply_filters(query, q: str, donor_id: int | None):
    from backend.services.person_search import apply_person_text_search_filter

    query = apply_person_text_search_filter(query, q)
    if donor_id is not None:
        query = query.filter(Person.donor_id == donor_id)
    return query


def _apply_funnel_people_scope(query, q: str, donor_id: int | None, engaged_sq):
    query = _apply_filters(query, q, donor_id)
    # Пустая стадия — контакт убран с доски действием «Вырезать»; остаётся в «Базе контактов», стадию можно задать снова.
    query = query.filter(Person.crm_stage != "")
    # Колонка «new» (в UI — «Уже контактировали»): только те, с кем реально был контакт,
    # иначе на доску попали бы все импортированные с дефолтной стадией new.
    # Любая другая стадия (follow_up_later, interested, …) — как в списке «База контактов»,
    # без требования записи в contacted_people / диалога в мессенджере.
    query = query.filter(
        or_(
            Person.crm_stage != "new",
            Person.id.in_(select(engaged_sq.c.person_id)),
        )
    )
    return query


def _stage_counts(db: Session, q: str, donor_id: int | None, engaged_sq) -> dict[str, int]:
    base = _apply_funnel_people_scope(db.query(Person), q, donor_id, engaged_sq)
    rows = base.with_entities(Person.crm_stage, func.count(Person.id)).group_by(Person.crm_stage).all()
    return {str(stage or ""): int(n) for stage, n in rows}


def _donor_name_map(db: Session) -> dict[int, str]:
    donors = db.query(Donor.id, Donor.name).all()
    return {d.id: (d.name or f"Донор #{d.id}") for d in donors}


def _messenger_thread_person_ids(db: Session, person_ids: set[int]) -> set[int]:
    if not person_ids:
        return set()
    rows = (
        db.query(Conversation.person_id)
        .filter(Conversation.person_id.in_(person_ids))
        .distinct()
        .all()
    )
    return {int(pid) for (pid,) in rows if pid is not None}


def _valid_stage_slugs(db: Session) -> set[str]:
    return {s.slug for s in get_ordered_stages(db)}


def _delete_stage_fallback(rows: list[CRMStage], stage_id: int) -> tuple[str, str] | None:
    ids = [r.id for r in rows]
    if stage_id not in ids:
        return None
    i = ids.index(stage_id)
    if i + 1 < len(rows):
        r = rows[i + 1]
        return r.slug, r.label
    if i - 1 >= 0:
        r = rows[i - 1]
        return r.slug, r.label
    return None


@router.get("/crm/funnel")
async def crm_funnel_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    q = (request.query_params.get("q") or "").strip()
    donor_raw = request.query_params.get("donor_id", "")
    donor_id = _parse_donor_id(donor_raw)
    view = (request.query_params.get("view") or "kanban").strip().lower()
    if view not in ("kanban", "tabs"):
        view = "kanban"

    relinked = relink_conversations_missing_person(
        db, build_messenger_person_id_index(db, organization_id=org_id)
    )
    if relinked:
        db.commit()

    engaged_sq = engaged_person_ids_subquery()

    stage_rows = get_ordered_stages(db)
    stage_keys = [s.slug for s in stage_rows]
    stage_labels = {s.slug: workflow_label_for_stage(s.slug, s.label) for s in stage_rows}
    if not stage_keys:
        seed_builtin_stages(db)
        db.commit()
        stage_rows = get_ordered_stages(db)
        stage_keys = [s.slug for s in stage_rows]
        stage_labels = {s.slug: workflow_label_for_stage(s.slug, s.label) for s in stage_rows}

    active_tab = (request.query_params.get("tab") or stage_keys[0]).strip()
    if active_tab not in stage_keys:
        active_tab = stage_keys[0]

    counts = _stage_counts(db, q, donor_id, engaged_sq)
    donor_labels = _donor_name_map(db)
    donors = db.query(Donor).order_by(Donor.name.asc().nullslast(), Donor.id).all()

    columns: dict[str, list[Person]] = {k: [] for k in stage_keys}
    other_by_stage: dict[str, list[Person]] = {}
    overflow: dict[str, int] = {}

    known_set = set(stage_keys)
    for stage_key in stage_keys:
        sub = _apply_funnel_people_scope(db.query(Person), q, donor_id, engaged_sq).filter(Person.crm_stage == stage_key)
        sub = sub.order_by(
            Person.crm_stage_changed_at.desc().nullslast(),
            Person.updated_at.desc(),
        )
        total = sub.count()
        rows = sub.limit(CARD_LIMIT_PER_COLUMN).all()
        columns[stage_key] = rows
        if total > len(rows):
            overflow[stage_key] = total - len(rows)

    other_q = _apply_funnel_people_scope(db.query(Person), q, donor_id, engaged_sq).filter(~Person.crm_stage.in_(known_set))
    other_total = other_q.count()
    other_rows = (
        other_q.order_by(Person.crm_stage.asc(), Person.updated_at.desc()).limit(CARD_LIMIT_PER_COLUMN).all()
    )
    for p in other_rows:
        key = (p.crm_stage or "").strip() or "_empty"
        other_by_stage.setdefault(key, []).append(p)
    other_overflow = max(0, other_total - len(other_rows))

    visible_person_ids = {p.id for plist in columns.values() for p in plist}
    visible_person_ids.update(p.id for plist in other_by_stage.values() for p in plist)
    messenger_thread_ids = _messenger_thread_person_ids(db, visible_person_ids)
    messenger_open_url_by_person_id = {
        pid: f"/messenger2/open-person/{pid}"
        for pid in visible_person_ids
    }

    active_aid = get_active_fb_account_id(request, db, org_id)
    messenger_outreach_blocked = bool(
        active_aid and messenger_blocked_by_outreach(db, active_aid)
    )

    stages_notice = (request.query_params.get("stages_notice") or "").strip()
    stages_error = (request.query_params.get("stages_error") or "").strip()

    stage_icon_map = {s.slug: icon_class_for_crm_stage_slug(s.slug) for s in stage_rows}
    if "new" in stage_icon_map:
        stage_icon_map["new"] = "ti-check"

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "crm/funnel.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "crm_funnel",
            "search_q": q,
            "donor_id": donor_id,
            "donor_raw": donor_raw,
            "donors": donors,
            "view": view,
            "active_tab": active_tab,
            "stage_rows": stage_rows,
            "stage_keys": stage_keys,
            "stage_labels": stage_labels,
            "columns": columns,
            "counts": counts,
            "overflow": overflow,
            "other_by_stage": other_by_stage,
            "other_total": other_total,
            "other_overflow": other_overflow,
            "messenger_thread_person_ids": messenger_thread_ids,
            "messenger_open_url_by_person_id": messenger_open_url_by_person_id,
            "messenger_outreach_blocked": messenger_outreach_blocked,
            "donor_labels": donor_labels,
            "card_limit": CARD_LIMIT_PER_COLUMN,
            "stages_notice": stages_notice,
            "stages_error": stages_error,
            "stage_icon_map": stage_icon_map,
        },
    )


@router.post("/crm/funnel/stages/add")
async def crm_funnel_stage_add(
    db: Session = Depends(get_db),
    label: str = Form(""),
    slug: str = Form(""),
    ret_q: str = Form(""),
    ret_donor_id: str = Form(""),
    ret_view: str = Form("kanban"),
    ret_tab: str = Form(""),
):
    label = (label or "").strip()
    if not label:
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
            error="Введите название стадии",
        )
    slug_clean = (slug or "").strip().lower()
    if slug_clean:
        if not _SLUG_RE.match(slug_clean):
            return _redirect_funnel(
                q=ret_q.strip(),
                donor_id=_parse_donor_id(ret_donor_id),
                view=(ret_view or "kanban").strip(),
                tab=(ret_tab or "").strip(),
                error="Slug: латиница, цифры и _, до 50 символов, с буквы",
            )
        if db.query(CRMStage).filter(CRMStage.slug == slug_clean).first():
            return _redirect_funnel(
                q=ret_q.strip(),
                donor_id=_parse_donor_id(ret_donor_id),
                view=(ret_view or "kanban").strip(),
                tab=(ret_tab or "").strip(),
                error="Такой slug уже есть",
            )
        final_slug = slug_clean
    else:
        final_slug = suggest_slug(label, db)

    db.add(
        CRMStage(
            slug=final_slug,
            label=label,
            sort_order=next_sort_order(db),
            is_builtin=False,
        )
    )
    db.commit()
    return _redirect_funnel(
        q=ret_q.strip(),
        donor_id=_parse_donor_id(ret_donor_id),
        view=(ret_view or "kanban").strip(),
        tab=(ret_tab or "").strip(),
        notice=f"Стадия «{label}» добавлена",
    )


@router.post("/crm/funnel/stages/{stage_id:int}/rename")
async def crm_funnel_stage_rename(
    stage_id: int,
    db: Session = Depends(get_db),
    label: str = Form(""),
    ret_q: str = Form(""),
    ret_donor_id: str = Form(""),
    ret_view: str = Form("kanban"),
    ret_tab: str = Form(""),
):
    label = (label or "").strip()
    row = db.get(CRMStage, stage_id)
    if not row or not label:
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
            error="Стадия не найдена или пустое имя",
        )
    row.label = label
    db.commit()
    return _redirect_funnel(
        q=ret_q.strip(),
        donor_id=_parse_donor_id(ret_donor_id),
        view=(ret_view or "kanban").strip(),
        tab=(ret_tab or "").strip(),
        notice="Название обновлено",
    )


@router.post("/crm/funnel/stages/{stage_id:int}/delete")
async def crm_funnel_stage_delete(
    stage_id: int,
    db: Session = Depends(get_db),
    ret_q: str = Form(""),
    ret_donor_id: str = Form(""),
    ret_view: str = Form("kanban"),
    ret_tab: str = Form(""),
):
    row = db.get(CRMStage, stage_id)
    if not row:
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
            error="Стадия не найдена",
        )
    rows = get_ordered_stages(db)
    if len(rows) <= 1:
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
            error="Нельзя удалить единственную стадию воронки",
        )
    fallback = _delete_stage_fallback(rows, stage_id)
    n = db.query(Person).filter(Person.crm_stage == row.slug).count()
    if n and fallback:
        now = datetime.now(timezone.utc)
        db.query(Person).filter(Person.crm_stage == row.slug).update(
            {
                Person.crm_stage: fallback[0],
                Person.crm_stage_changed_at: now,
            },
            synchronize_session=False,
        )
    elif n and not fallback:
        db.query(Person).filter(Person.crm_stage == row.slug).update(
            {
                Person.crm_stage: "",
                Person.crm_stage_changed_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    db.delete(row)
    db.commit()
    if n and fallback:
        target_label = workflow_label_for_stage(fallback[0], fallback[1])
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
            notice=f"Стадия удалена; {n} контакт(ов) перенесены в «{target_label}»",
        )
    if n:
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
            notice=f"Стадия удалена; {n} контакт(ов) очищены от этой стадии",
        )
    return _redirect_funnel(
        q=ret_q.strip(),
        donor_id=_parse_donor_id(ret_donor_id),
        view=(ret_view or "kanban").strip(),
        tab=(ret_tab or "").strip(),
        notice="Стадия удалена",
    )


@router.post("/crm/funnel/stages/{stage_id:int}/move")
async def crm_funnel_stage_move(
    stage_id: int,
    db: Session = Depends(get_db),
    direction: str = Form(""),
    ret_q: str = Form(""),
    ret_donor_id: str = Form(""),
    ret_view: str = Form("kanban"),
    ret_tab: str = Form(""),
):
    direction = (direction or "").strip().lower()
    if direction not in ("up", "down"):
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
        )

    rows = get_ordered_stages(db)
    ids = [r.id for r in rows]
    if stage_id not in ids:
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
        )
    i = ids.index(stage_id)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(rows):
        return _redirect_funnel(
            q=ret_q.strip(),
            donor_id=_parse_donor_id(ret_donor_id),
            view=(ret_view or "kanban").strip(),
            tab=(ret_tab or "").strip(),
        )
    a, b = rows[i], rows[j]
    a.sort_order, b.sort_order = b.sort_order, a.sort_order
    db.commit()
    return _redirect_funnel(
        q=ret_q.strip(),
        donor_id=_parse_donor_id(ret_donor_id),
        view=(ret_view or "kanban").strip(),
        tab=(ret_tab or "").strip(),
        notice="Порядок стадий обновлён",
    )


class FunnelMoveBody(BaseModel):
    person_id: int = Field(..., ge=1)
    crm_stage: str = Field(..., min_length=1, max_length=50)
    # При переходе в «Написать позже»: 24h | 7d | 30d, либо none/clear — без AI-напоминания; поле отсутствует — не трогать план.
    messenger_followup_preset: str | None = None
    messenger_followup_prompt: str | None = Field(None, max_length=4000)


def _clear_person_messenger_followup(person: Person) -> None:
    person.messenger_followup_at = None
    person.messenger_followup_prompt = None
    person.messenger_followup_status = None
    person.messenger_followup_last_error = None


@router.post("/crm/funnel/move")
async def crm_funnel_move(
    request: Request, body: FunnelMoveBody, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    allowed = _valid_stage_slugs(db)
    if body.crm_stage not in allowed:
        raise HTTPException(status_code=400, detail="Неизвестная стадия CRM")
    person = db.get(Person, body.person_id)
    if not person or person.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    old_stage = (person.crm_stage or "").strip()
    now = datetime.now(timezone.utc)
    person.crm_stage = body.crm_stage
    person.crm_stage_changed_at = now

    if old_stage == "follow_up_later" and body.crm_stage != "follow_up_later":
        _clear_person_messenger_followup(person)
    elif body.crm_stage == "follow_up_later" and body.messenger_followup_preset is not None:
        preset = str(body.messenger_followup_preset).strip().lower()
        prompt = (body.messenger_followup_prompt or "").strip()[:4000] or None
        if preset in ("", "none", "clear"):
            _clear_person_messenger_followup(person)
        elif preset in ("24h", "7d", "30d"):
            deltas = {
                "24h": timedelta(hours=24),
                "7d": timedelta(days=7),
                "30d": timedelta(days=30),
            }
            person.messenger_followup_at = now + deltas[preset]
            person.messenger_followup_prompt = prompt
            person.messenger_followup_status = "pending"
            person.messenger_followup_last_error = None
        else:
            raise HTTPException(
                status_code=400,
                detail="Неверный пресет напоминания (24h, 7d, 30d или none)",
            )

    db.commit()
    return JSONResponse({"ok": True, "person_id": body.person_id, "crm_stage": body.crm_stage})


class PersonMessengerFollowupBody(BaseModel):
    messenger_followup_preset: str = Field(default="", max_length=16)
    messenger_followup_prompt: str | None = Field(None, max_length=4000)


@router.post("/crm/person/{person_id}/messenger-followup")
async def crm_person_messenger_followup_save(
    request: Request,
    person_id: int,
    body: PersonMessengerFollowupBody,
    db: Session = Depends(get_db),
):
    """Сохранить срок и текст задания для отложенного AI-сообщения (карточка «Написать позже»)."""
    org_id = require_org_id(request, db)
    person = db.get(Person, person_id)
    if not person or person.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    if (person.crm_stage or "").strip() != "follow_up_later":
        raise HTTPException(
            status_code=400,
            detail="Сначала переведите контакт в стадию «Написать позже»",
        )
    preset = body.messenger_followup_preset.strip().lower()
    prompt = (body.messenger_followup_prompt or "").strip()[:4000] or None
    now = datetime.now(timezone.utc)
    if preset in ("", "none", "clear"):
        _clear_person_messenger_followup(person)
    elif preset in ("24h", "7d", "30d"):
        deltas = {
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }
        person.messenger_followup_at = now + deltas[preset]
        person.messenger_followup_prompt = prompt
        person.messenger_followup_status = "pending"
        person.messenger_followup_last_error = None
    else:
        raise HTTPException(
            status_code=400,
            detail="Неверный пресет (24h, 7d, 30d или none)",
        )
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "person_id": person.id,
            "messenger_followup_at": (
                person.messenger_followup_at.isoformat()
                if person.messenger_followup_at
                else None
            ),
            "messenger_followup_status": person.messenger_followup_status,
        }
    )


class FunnelRemoveBody(BaseModel):
    person_id: int = Field(..., ge=1)


@router.post("/crm/funnel/remove")
async def crm_funnel_remove(
    request: Request, body: FunnelRemoveBody, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    person = db.get(Person, body.person_id)
    if not person or person.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    now = datetime.now(timezone.utc)
    person.crm_stage = ""
    person.crm_stage_changed_at = now
    _clear_person_messenger_followup(person)
    db.commit()
    return JSONResponse({"ok": True, "person_id": body.person_id})


def _people_for_export(db: Session, q: str, donor_id: int | None) -> list[Person]:
    engaged_sq = engaged_person_ids_subquery()
    sub = _apply_funnel_people_scope(db.query(Person), q, donor_id, engaged_sq)
    return sub.order_by(Person.crm_stage.asc(), Person.id.asc()).all()


@router.get("/crm/funnel/export.csv")
async def crm_funnel_export_csv(request: Request, db: Session = Depends(get_db)):
    q = (request.query_params.get("q") or "").strip()
    donor_id = _parse_donor_id(request.query_params.get("donor_id", ""))
    people = _people_for_export(db, q, donor_id)
    donor_labels = _donor_name_map(db)
    labels = {
        slug: workflow_label_for_stage(slug, label)
        for slug, label in stage_labels_map(db).items()
    }

    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow(
        [
            "ID",
            "Стадия (код)",
            "Стадия (название)",
            "Имя",
            "Ссылка на профиль",
            "ID донора",
            "Донор",
            "Заметки CRM",
            "Дата смены стадии",
            "Создан",
            "Обновлён",
        ]
    )
    for p in people:
        stage = p.crm_stage or ""
        w.writerow(
            [
                p.id,
                stage,
                labels.get(stage, stage),
                p.display_name or "",
                p.canonical_url or "",
                p.donor_id or "",
                donor_labels.get(p.donor_id, "") if p.donor_id else "",
                (p.crm_notes or "").replace("\r\n", "\n").replace("\n", " | "),
                p.crm_stage_changed_at.isoformat() if p.crm_stage_changed_at else "",
                p.created_at.isoformat() if p.created_at else "",
                p.updated_at.isoformat() if p.updated_at else "",
            ]
        )

    data = buf.getvalue().encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    pretty = f"Воронка_CRM_{ts}.csv"
    cd = attachment_content_disposition(pretty, ascii_filename=f"crm_funnel_{ts}.csv")
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": cd},
    )
