"""CRUD доноров."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Donor, Person
from backend.services.fb_url_normalize import (
    normalize_facebook_group_members_url,
    normalize_facebook_profile_url,
)
from backend.services.tenancy import require_org_id
from backend.services.content_disposition import attachment_content_disposition
from backend.services.friends_list_scrape import friends_workbook_to_bytes

router = APIRouter(prefix="/donors", tags=["donors"])

_BULK_MAX_LINES = 400


def _is_facebook_http_url(url: str) -> bool:
    u = url.strip().lower()
    return u.startswith("http://") or u.startswith("https://")


def _dedup_key_for_url(url: str) -> str:
    u = url.strip()
    g = normalize_facebook_group_members_url(u)
    if g:
        return g.lower()
    canon = normalize_facebook_profile_url(u)
    if canon:
        return canon.lower()
    return u.lower()


def _parse_bulk_line(line: str) -> tuple[str, str | None, str | None] | None:
    """
    Одна строка: URL
    Или: URL<TAB>имя<TAB>заметки
    Или: URL | имя | заметки (если строка начинается с http)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "\t" in line:
        parts = [p.strip() for p in line.split("\t", 2)]
    elif line.lower().startswith("http") and "|" in line:
        parts = [p.strip() for p in line.split("|", 2)]
    else:
        parts = [line]
    url = parts[0]
    if not url or not _is_facebook_http_url(url):
        return None
    if "facebook.com" not in url.lower():
        return None
    name = parts[1] if len(parts) > 1 and parts[1] else None
    notes = parts[2] if len(parts) > 2 and parts[2] else None
    return (url.strip(), name, notes)


def _existing_donor_keys(db: Session, org_id: int) -> set[str]:
    keys: set[str] = set()
    for d in db.query(Donor).filter(Donor.organization_id == org_id).all():
        keys.add(_dedup_key_for_url(d.url))
    return keys


@router.get("")
async def donor_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    q = request.query_params.get("q", "").strip()
    query = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.created_at.desc())
    )
    if q:
        query = query.filter(
            Donor.url.ilike(f"%{q}%") | Donor.name.ilike(f"%{q}%")
        )
    donors = query.all()
    templates = request.app.state.templates
    return templates.TemplateResponse("donors/list.html", {
        "request": request,
        "user": request.session.get("user"),
        "donors": donors,
        "search": q,
        "page_id": "donors",
    })


@router.get("/new")
async def donor_form_new(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse("donors/form.html", {
        "request": request,
        "user": request.session.get("user"),
        "donor": None,
        "page_id": "donors",
    })


@router.get("/bulk")
async def donor_bulk_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "donors/bulk.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "donors",
        },
    )


@router.post("/bulk-save")
async def donor_bulk_save(
    request: Request,
    db: Session = Depends(get_db),
    bulk_text: str = Form(""),
    default_notes: str = Form(""),
):
    default_notes = default_notes.strip() or None
    rows: list[tuple[str, str | None, str | None]] = []
    bad_lines = 0
    for line in (bulk_text or "").splitlines():
        parsed = _parse_bulk_line(line)
        if parsed is None:
            if line.strip() and not line.strip().startswith("#"):
                bad_lines += 1
            continue
        rows.append(parsed)
        if len(rows) >= _BULK_MAX_LINES:
            break

    if not rows and bad_lines == 0:
        return RedirectResponse("/donors/bulk?err=empty", status_code=303)

    org_id = require_org_id(request, db)
    existing = _existing_donor_keys(db, org_id)
    seen_batch: set[str] = set()
    added = 0
    skipped_dup = 0

    for url, name, notes in rows:
        gm = normalize_facebook_group_members_url(url)
        if gm:
            url = gm
        key = _dedup_key_for_url(url)
        if key in seen_batch:
            skipped_dup += 1
            continue
        seen_batch.add(key)
        if key in existing:
            skipped_dup += 1
            continue
        existing.add(key)
        merged_notes = (notes or default_notes) or None
        db.add(Donor(organization_id=org_id, url=url, name=name, notes=merged_notes))
        added += 1

    db.commit()
    q = (
        f"bulk_added={added}&bulk_dup={skipped_dup}&bulk_bad={bad_lines}"
        f"&bulk_total={len(rows)}"
    )
    return RedirectResponse(f"/donors?{q}", status_code=303)


@router.get("/{donor_id}/export-crm")
async def donor_export_crm_xlsx(
    donor_id: int, request: Request, db: Session = Depends(get_db)
):
    """Excel: ссылка, ФИО, ID в CRM — для вставки ID столбиком в рассылку."""
    org_id = require_org_id(request, db)
    donor = (
        db.query(Donor)
        .filter(Donor.id == donor_id, Donor.organization_id == org_id)
        .first()
    )
    if not donor:
        raise HTTPException(status_code=404, detail="Донор не найден")
    rows = db.query(Person).filter(Person.donor_id == donor_id).order_by(Person.id).all()
    by_url: dict[str, str] = {}
    id_map: dict[str, int] = {}
    for p in rows:
        if not p.canonical_url:
            continue
        by_url[p.canonical_url] = (p.display_name or "").strip()
        id_map[p.canonical_url] = p.id
    data = friends_workbook_to_bytes(by_url, crm_ids_by_canonical=id_map)
    base = (donor.name or f"donor_{donor.id}")[:80]
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\n\r\t]+', "", base).strip() or str(donor.id)
    pretty = f"Friends_CRM_{safe}.xlsx"
    cd = attachment_content_disposition(
        pretty,
        ascii_filename=f"Friends_CRM_donor_{donor_id}.xlsx",
    )
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": cd},
    )


@router.get("/{donor_id}/edit")
async def donor_form_edit(donor_id: int, request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    donor = (
        db.query(Donor)
        .filter(Donor.id == donor_id, Donor.organization_id == org_id)
        .first()
    )
    if not donor:
        return RedirectResponse("/donors", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse("donors/form.html", {
        "request": request,
        "user": request.session.get("user"),
        "donor": donor,
        "page_id": "donors",
    })


@router.post("/save")
async def donor_save(
    request: Request,
    db: Session = Depends(get_db),
    donor_id: int = Form(None),
    url: str = Form(...),
    name: str = Form(""),
    notes: str = Form(""),
):
    url = url.strip()
    gm = normalize_facebook_group_members_url(url)
    if gm:
        url = gm
    name = name.strip()
    notes = notes.strip()

    org_id = require_org_id(request, db)
    if donor_id:
        donor = (
            db.query(Donor)
            .filter(Donor.id == donor_id, Donor.organization_id == org_id)
            .first()
        )
        if donor:
            donor.url = url
            donor.name = name or donor.name
            donor.notes = notes
    else:
        donor = Donor(
            organization_id=org_id,
            url=url,
            name=name or None,
            notes=notes or None,
        )
        db.add(donor)

    db.commit()
    return RedirectResponse("/donors", status_code=303)


@router.post("/{donor_id}/delete")
async def donor_delete(donor_id: int, request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    donor = (
        db.query(Donor)
        .filter(Donor.id == donor_id, Donor.organization_id == org_id)
        .first()
    )
    if donor:
        db.delete(donor)
        db.commit()
    return RedirectResponse("/donors", status_code=303)
