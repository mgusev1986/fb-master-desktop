"""Импорт базы из CSV / XLSX файлов."""

from __future__ import annotations

import csv
from typing import Any
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    AIAgentRun,
    ContactedPerson,
    Conversation,
    CRMActivity,
    Donor,
    ImportBatch,
    JobEvent,
    OrganizationContactedPerson,
    OutreachCampaign,
    OutreachQueue,
    Person,
    SequenceEnrollment,
    WarmupCampaign,
    WarmupQueue,
)
from backend.services.content_disposition import attachment_content_disposition
from backend.services.fb_url_normalize import normalize_facebook_profile_url
from backend.services.messenger_person_link import relink_messenger_conversations_to_people
from backend.services.friends_list_scrape import friends_workbook_to_bytes, looks_like_fb_restricted_display_name
from backend.services.tenancy import require_org_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/import", tags=["import"])

# Не раздуваем БД: сохраняем только первые N записей о пропусках (остальные только в счётчике).
SKIP_DETAILS_MAX = 800


def _append_skip_detail(
    log: list[dict[str, Any]],
    reason: str,
    *,
    url_raw: str = "",
    name: str = "",
    canonical: str | None = None,
    person_id: int | None = None,
) -> None:
    if len(log) >= SKIP_DETAILS_MAX:
        return
    log.append(
        {
            "reason": reason,
            "url_raw": (url_raw or "")[:450],
            "name": (name or "")[:220],
            "canonical": (canonical or "")[:512] if canonical else None,
            "person_id": person_id,
        }
    )


def _normalize_fb_url(raw: str) -> str | None:
    return normalize_facebook_profile_url(raw)


def _cell_text(val: Any) -> str:
    """Текст ячейки: NBSP из Excel не считается содержимым имени."""
    return str(val or "").replace("\u00a0", " ").strip()


def _guess_columns(header: list[str]) -> tuple[int | None, int | None, int | None]:
    """Индексы колонок: URL, имя (или ФИО), опционально фамилия (как в экспорте «Имя» / «Фамилия»)."""
    url_idx = None
    name_idx = None
    surname_idx = None
    for i, h in enumerate(header):
        low = h.strip().lower()
        if url_idx is None and any(k in low for k in ("ссылк", "url", "link", "href", "профил")):
            url_idx = i
        if surname_idx is None and any(
            k in low for k in ("фамилия", "фамили", "surname", "last name", "lastname", "last_name")
        ):
            surname_idx = i
        if name_idx is None:
            if any(k in low for k in ("фио", "имя", "ник")):
                name_idx = i
            elif re.search(r"\bfirst[\s_]?name\b", low) or low == "firstname":
                name_idx = i
            elif low.strip() == "name":
                name_idx = i
    if url_idx is None:
        for i, h in enumerate(header):
            if re.search(r"facebook\.com", h, re.I):
                url_idx = i
                break
    # «Имя» не должно совпасть с колонкой «Фамилия»
    if name_idx is not None and surname_idx is not None and name_idx == surname_idx:
        surname_idx = None
    return url_idx, name_idx, surname_idx


def _split_url_name_in_one_cell(raw: str) -> tuple[str, str]:
    """
    Одна ячейка: URL и ФИО вместе — через запятую или точку с запятой (как в EU CSV).
    """
    s = (raw or "").strip()
    if not s:
        return s, ""
    low = s.lower()
    if not (low.startswith("http") or low.startswith("www.") or "facebook.com" in low):
        return s, ""
    for sep in (",", ";"):
        if sep not in s:
            continue
        left, _, right = s.partition(sep)
        left, right = left.strip(), right.strip()
        if left and right and "facebook.com" in left.lower():
            return left, right
    return s, ""


def _combine_display_name(first: str, last: str) -> str:
    a, b = (first or "").strip(), (last or "").strip()
    if a and b:
        return f"{a} {b}".strip()
    return a or b


def _extract_url_name_from_row(
    row_cells: list[Any],
    url_idx: int | None,
    name_idx: int | None,
    surname_idx: int | None = None,
) -> tuple[str, str] | None:
    """Вернуть (url, имя) или None если строку пропускаем."""
    if url_idx is None or url_idx >= len(row_cells):
        return None
    if url_idx == name_idx:
        combined = _cell_text(row_cells[url_idx])
        if not combined:
            return None
        u, n = _split_url_name_in_one_cell(combined)
        return (u, n) if u else None
    url_raw = _cell_text(row_cells[url_idx])
    name_raw = ""
    if name_idx is not None and name_idx < len(row_cells):
        name_raw = _cell_text(row_cells[name_idx])
    surname_raw = ""
    if (
        surname_idx is not None
        and surname_idx < len(row_cells)
        and surname_idx != name_idx
    ):
        surname_raw = _cell_text(row_cells[surname_idx])
    # Пустая вторая колонка в Excel иногда даёт пробел / NBSP — тогда «URL;ФИО» остаётся в первой.
    if url_raw and not name_raw and not surname_raw:
        u, n = _split_url_name_in_one_cell(url_raw)
        if n:
            url_raw, name_raw = u, n
    if not url_raw:
        return None
    if surname_raw:
        name_raw = _combine_display_name(name_raw, surname_raw)
    return url_raw, name_raw


def _detect_csv_delimiter(text: str) -> str:
    """Excel в RU/DE часто сохраняет CSV с «;», стандарт RFC — «,»."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ","
    first = lines[0]
    semi, comma, tab = first.count(";"), first.count(","), first.count("\t")
    if tab and tab >= max(semi, comma):
        return "\t"
    if semi > comma or (semi >= 1 and comma == 0):
        return ";"
    if semi == comma == 0:
        try:
            return csv.Sniffer().sniff(text[:8192], delimiters=";\t,").delimiter
        except csv.Error:
            return ","
    return ","


def _parse_csv_rows(content: bytes) -> list[tuple[str, str]]:
    text = content.decode("utf-8-sig", errors="replace")
    delim = _detect_csv_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows_out: list[tuple[str, str]] = []
    header = next(reader, None)
    if not header:
        return rows_out
    url_idx, name_idx, surname_idx = _guess_columns(header)
    if url_idx is None:
        url_idx = 1 if len(header) > 1 else 0
    if name_idx is None:
        name_idx = 2 if len(header) > 2 else (0 if url_idx != 0 else 1)
    for row in reader:
        pair = _extract_url_name_from_row(row, url_idx, name_idx, surname_idx)
        if pair:
            rows_out.append(pair)
    return rows_out


def _parse_xlsx_rows(content: bytes) -> list[tuple[str, str]]:
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows_out: list[tuple[str, str]] = []
        if ws is None:
            return rows_out
        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows:
            return rows_out
        header = [str(c or "") for c in all_rows[0]]
        url_idx, name_idx, surname_idx = _guess_columns(header)
        if url_idx is None:
            url_idx = 1 if len(header) > 1 else 0
        if name_idx is None:
            name_idx = 2 if len(header) > 2 else (0 if url_idx != 0 else 1)
        for row in all_rows[1:]:
            cells = list(row)
            pair = _extract_url_name_from_row(cells, url_idx, name_idx, surname_idx)
            if pair:
                rows_out.append(pair)
        return rows_out
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _merge_duplicate_parser_batches_if_any(db: Session) -> None:
    """Слить лишние партии parser:… по одному donor_id (старые дубликаты в истории)."""
    dup_rows = (
        db.query(ImportBatch.donor_id)
        .filter(
            ImportBatch.donor_id.isnot(None),
            ImportBatch.filename.startswith("parser:"),
        )
        .group_by(ImportBatch.donor_id)
        .having(func.count(ImportBatch.id) > 1)
        .all()
    )
    if not dup_rows:
        return
    for (donor_id,) in dup_rows:
        donor = db.get(Donor, donor_id)
        if not donor:
            continue
        label = (donor.name or "").strip() or f"donor_{donor.id}"
        want_name = f"parser:{label}"
        batches = (
            db.query(ImportBatch)
            .filter(
                ImportBatch.donor_id == donor.id,
                ImportBatch.filename.startswith("parser:"),
            )
            .order_by(ImportBatch.id.asc())
            .all()
        )
        if len(batches) < 2:
            continue
        keep = batches[0]
        if keep.filename != want_name:
            keep.filename = want_name
        for obsolete in batches[1:]:
            db.query(Person).filter(Person.import_batch_id == obsolete.id).update(
                {Person.import_batch_id: keep.id},
                synchronize_session=False,
            )
            db.delete(obsolete)
        db.query(Person).filter(Person.donor_id == donor.id).update(
            {Person.import_batch_id: keep.id},
            synchronize_session=False,
        )
    db.commit()


def _delete_import_batch(db: Session, batch_id: int) -> int:
    """
    Удаляет партию импорта и всех людей, привязанных к ней (только «новые» контакты этого файла).
    Возвращает число удалённых людей.
    """
    batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Импорт не найден")

    rows = db.query(Person.id).filter(Person.import_batch_id == batch_id).all()
    person_ids = [r[0] for r in rows]
    remove = set(person_ids)

    if person_ids:
        from backend.services.contacted_registry import mirror_installation_contacted_urls_for_person_ids

        mirror_installation_contacted_urls_for_person_ids(db, person_ids)
        db.query(ContactedPerson).filter(ContactedPerson.person_id.in_(person_ids)).delete(
            synchronize_session=False
        )
        db.query(OrganizationContactedPerson).filter(
            OrganizationContactedPerson.person_id.in_(person_ids)
        ).delete(synchronize_session=False)
        db.query(CRMActivity).filter(CRMActivity.person_id.in_(person_ids)).delete(
            synchronize_session=False
        )
        db.query(OutreachQueue).filter(OutreachQueue.person_id.in_(person_ids)).delete(
            synchronize_session=False
        )
        db.query(WarmupQueue).filter(WarmupQueue.person_id.in_(person_ids)).delete(
            synchronize_session=False
        )
        db.query(SequenceEnrollment).filter(SequenceEnrollment.person_id.in_(person_ids)).delete(
            synchronize_session=False
        )
        db.query(JobEvent).filter(JobEvent.person_id.in_(person_ids)).update(
            {JobEvent.person_id: None},
            synchronize_session=False,
        )
        db.query(Conversation).filter(Conversation.person_id.in_(person_ids)).update(
            {Conversation.person_id: None},
            synchronize_session=False,
        )
        db.query(AIAgentRun).filter(AIAgentRun.person_id.in_(person_ids)).update(
            {AIAgentRun.person_id: None},
            synchronize_session=False,
        )

        if remove:
            for camp in db.query(OutreachCampaign).all():
                ids = list(camp.person_ids or [])
                new_ids = [i for i in ids if i not in remove]
                if new_ids != ids:
                    camp.person_ids = new_ids
            for camp in db.query(WarmupCampaign).all():
                ids = list(camp.person_ids or [])
                new_ids = [i for i in ids if i not in remove]
                if new_ids != ids:
                    camp.person_ids = new_ids

        db.query(Person).filter(Person.id.in_(person_ids)).delete(synchronize_session=False)

    db.delete(batch)
    db.commit()
    return len(person_ids)


class ImportBatchIdsBody(BaseModel):
    """Тело POST для выгрузки нескольких партий импорта одним ZIP."""

    batch_ids: list[int] = Field(default_factory=list, min_length=1, max_length=40)


def _import_batch_xlsx_bytes(db: Session, batch: ImportBatch) -> bytes:
    """Один лист Excel по людям партии (как у GET /import/batch/{id}/export)."""
    batch_id = batch.id
    rows = (
        db.query(Person)
        .filter(Person.import_batch_id == batch_id)
        .order_by(Person.id)
        .all()
    )
    by_url: dict[str, str] = {}
    id_map: dict[str, int] = {}
    for p in rows:
        if not p.canonical_url:
            continue
        by_url[p.canonical_url] = (p.display_name or "").strip()
        id_map[p.canonical_url] = p.id
    return friends_workbook_to_bytes(by_url, crm_ids_by_canonical=id_map)


def _safe_export_filename_from_batch(batch: ImportBatch, batch_id: int) -> str:
    base = (batch.filename or f"batch_{batch_id}")[:120]
    low = base.lower()
    for ext in (".csv", ".xlsx", ".xls"):
        if low.endswith(ext):
            base = base[: -len(ext)]
            break
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\n\r\t]+', "", base).strip() or str(batch_id)
    fname = f"Import_{safe}.xlsx"
    if len(fname) > 200:
        fname = f"Import_batch_{batch_id}.xlsx"
    return fname


@router.get("/batch/{batch_id}/export")
async def import_batch_export_xlsx(
    batch_id: int, request: Request, db: Session = Depends(get_db)
):
    """Excel: ссылка, имя, фамилия, ID CRM — все люди партии импорта (ручной файл или parser:…)."""
    org_id = require_org_id(request, db)
    batch = (
        db.query(ImportBatch)
        .filter(ImportBatch.id == batch_id, ImportBatch.organization_id == org_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Партия не найдена")
    data = _import_batch_xlsx_bytes(db, batch)
    pretty = _safe_export_filename_from_batch(batch, batch_id)
    cd = attachment_content_disposition(
        pretty,
        ascii_filename=f"Import_batch_{batch_id}.xlsx",
    )
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": cd},
    )


@router.post("/batches/export-zip")
async def import_batches_export_zip(
    body: ImportBatchIdsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Несколько партий — один ZIP с отдельным .xlsx на каждую (до 40 за запрос)."""
    org_id = require_org_id(request, db)
    raw_ids = [x for x in body.batch_ids if isinstance(x, int) and x > 0]
    seen: set[int] = set()
    ids: list[int] = []
    for bid in raw_ids:
        if bid not in seen:
            seen.add(bid)
            ids.append(bid)
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одной партии")
    batches = (
        db.query(ImportBatch)
        .filter(ImportBatch.organization_id == org_id, ImportBatch.id.in_(ids))
        .all()
    )
    by_id = {b.id: b for b in batches}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail="Одна из партий не найдена")
    buf = io.BytesIO()
    used_members: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for bid in ids:
            b = by_id[bid]
            xlsx = _import_batch_xlsx_bytes(db, b)
            inner = _safe_export_filename_from_batch(b, bid)
            member = f"{bid}_{inner}"
            if member in used_members:
                member = f"batch_{bid}.xlsx"
            used_members.add(member)
            zf.writestr(member, xlsx)
    buf.seek(0)
    zip_bytes = buf.getvalue()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    pretty_zip = f"Импорт_выбранные_{stamp}.zip"
    cd = attachment_content_disposition(
        pretty_zip,
        ascii_filename=f"Import_batches_{stamp}.zip",
    )
    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": cd},
    )


@router.get("/api/batch/{batch_id}/skipped")
async def import_batch_skipped_json(
    batch_id: int, request: Request, db: Session = Depends(get_db)
):
    """Детализация пропущенных строк (ручной импорт CSV/XLSX)."""
    org_id = require_org_id(request, db)
    b = (
        db.query(ImportBatch)
        .filter(ImportBatch.id == batch_id, ImportBatch.organization_id == org_id)
        .first()
    )
    if not b:
        raise HTTPException(status_code=404, detail="Партия не найдена")
    raw = b.skip_details_json
    items = raw if isinstance(raw, list) else []
    return JSONResponse(
        {
            "filename": b.filename,
            "rows_skipped": int(b.rows_skipped or 0),
            "items": items,
            "shown": len(items),
            "truncated": bool((b.rows_skipped or 0) > len(items)),
            "has_details": len(items) > 0,
        }
    )


@router.get("")
async def import_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    _merge_duplicate_parser_batches_if_any(db)
    batches = (
        db.query(ImportBatch)
        .filter(ImportBatch.organization_id == org_id)
        .order_by(ImportBatch.created_at.desc())
        .limit(20)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse("import/upload.html", {
        "request": request,
        "user": request.session.get("user"),
        "batches": batches,
        "page_id": "import",
    })


@router.post("/upload")
async def import_upload(
    request: Request,
    db: Session = Depends(get_db),
    files: list[UploadFile] = File(...),
):
    total_new = 0
    total_updated = 0
    total_skipped = 0
    now = datetime.now(timezone.utc)
    org_id = require_org_id(request, db)

    for f in files:
        content = await f.read()
        fname = f.filename or "unknown"
        rows: list[tuple[str, str]] = []

        if fname.lower().endswith(".csv"):
            rows = _parse_csv_rows(content)
        elif fname.lower().endswith((".xlsx", ".xls")):
            rows = _parse_xlsx_rows(content)
        else:
            continue

        batch = ImportBatch(organization_id=org_id, filename=fname, rows_total=len(rows))
        db.add(batch)
        db.flush()  # нужен batch.id, чтобы привязать к партии и уже существующих людей по URL

        new_count = 0
        upd_count = 0
        skip_count = 0
        skip_details: list[dict[str, Any]] = []

        for url_raw, name_raw in rows:
            canonical = _normalize_fb_url(url_raw)
            if not canonical:
                skip_count += 1
                _append_skip_detail(
                    skip_details, "invalid_url", url_raw=url_raw, name=name_raw or ""
                )
                continue
            # canonical_url уникален глобально: иначе при другой org — INSERT дал бы IntegrityError
            existing = db.query(Person).filter(Person.canonical_url == canonical).first()
            r_flag = looks_like_fb_restricted_display_name(name_raw or "")
            if existing:
                if existing.organization_id != org_id:
                    skip_count += 1
                    _append_skip_detail(
                        skip_details,
                        "url_other_organization",
                        url_raw=url_raw,
                        name=name_raw or "",
                        canonical=canonical,
                        person_id=existing.id,
                    )
                    continue
                changed = False
                if existing.import_batch_id != batch.id:
                    existing.import_batch_id = batch.id
                    changed = True
                if name_raw and (
                    not existing.display_name or len(name_raw) > len(existing.display_name or "")
                ):
                    existing.display_name = name_raw
                    changed = True
                if bool(existing.fb_profile_restricted) != r_flag:
                    existing.fb_profile_restricted = r_flag
                    changed = True
                if changed:
                    existing.updated_at = now
                    upd_count += 1
                else:
                    skip_count += 1
                    _append_skip_detail(
                        skip_details,
                        "unchanged",
                        url_raw=url_raw,
                        name=name_raw or "",
                        canonical=canonical,
                        person_id=existing.id,
                    )
            else:
                person = Person(
                    organization_id=org_id,
                    canonical_url=canonical,
                    display_name=name_raw or None,
                    import_batch_id=None,
                    parsed_at=now,
                    fb_profile_restricted=r_flag,
                )
                db.add(person)
                new_count += 1

        batch.rows_new = new_count
        batch.rows_updated = upd_count
        batch.rows_skipped = skip_count
        batch.skip_details_json = skip_details if skip_details else None
        db.commit()

        for p in (
            db.query(Person)
            .filter(
                Person.organization_id == org_id,
                Person.import_batch_id.is_(None),
                Person.parsed_at == now,
            )
            .all()
        ):
            p.import_batch_id = batch.id
        db.commit()
        try:
            relink_messenger_conversations_to_people(db)
            db.commit()
        except Exception:
            logger.exception("import: relink messenger after batch %s", fname)
            db.rollback()

        total_new += new_count
        total_updated += upd_count
        total_skipped += skip_count
        logger.info("Import %s: %d new, %d updated, %d skipped", fname, new_count, upd_count, skip_count)

    return RedirectResponse("/import", status_code=303)


@router.post("/{batch_id}/delete")
async def import_batch_delete(
    batch_id: int, request: Request, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    b = (
        db.query(ImportBatch)
        .filter(ImportBatch.id == batch_id, ImportBatch.organization_id == org_id)
        .first()
    )
    if not b:
        return RedirectResponse("/import", status_code=303)
    n_people = _delete_import_batch(db, batch_id)
    logger.info("Deleted import batch %s and %d people", batch_id, n_people)
    return RedirectResponse("/import", status_code=303)
