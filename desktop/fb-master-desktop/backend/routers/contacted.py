"""Реестр «Уже контактировали»: глобальная пара человек + FB-аккаунт для пропуска в рассылке."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import ContactedPerson, FBAccount, Person
from backend.services.contacted_registry import prune_organization_contacted_if_no_account_pairs

router = APIRouter(prefix="/contacted", tags=["contacted"])

PAGE_SIZE = 50


def _contacted_list_redirect(
    *,
    q: str,
    page: int,
    account_id: str,
) -> RedirectResponse:
    params: dict[str, str] = {}
    if (q or "").strip():
        params["q"] = (q or "").strip()
    if account_id.strip().isdigit():
        params["account_id"] = account_id.strip()
    if page and page > 1:
        params["page"] = str(int(page))
    url = "/contacted"
    if params:
        url += "?" + urlencode(params)
    return RedirectResponse(url, status_code=303)


@router.get("")
async def contacted_list(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    page = max(1, int(request.query_params.get("page", "1") or 1))
    acc_filter = request.query_params.get("account_id", "").strip()

    query = (
        db.query(ContactedPerson, Person, FBAccount)
        .join(Person, Person.id == ContactedPerson.person_id)
        .join(FBAccount, FBAccount.id == ContactedPerson.fb_account_id)
        .order_by(
            ContactedPerson.last_message_at.desc().nullslast(),
            ContactedPerson.first_contacted_at.desc(),
        )
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            Person.display_name.ilike(like)
            | Person.canonical_url.ilike(like)
            | ContactedPerson.canonical_url.ilike(like)
            | FBAccount.label.ilike(like)
        )
    if acc_filter.isdigit():
        query = query.filter(ContactedPerson.fb_account_id == int(acc_filter))

    total = query.count()
    rows = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    accounts = db.query(FBAccount).order_by(FBAccount.label).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "contacted/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "contacted",
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": total_pages,
            "search": q,
            "account_filter": acc_filter,
            "fb_accounts": accounts,
        },
    )


@router.post("/{contacted_id:int}/remove")
async def contacted_remove(
    contacted_id: int,
    db: Session = Depends(get_db),
    ret_q: str = Form(""),
    ret_page: int = Form(1),
    ret_account_id: str = Form(""),
):
    row = db.get(ContactedPerson, contacted_id)
    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    person = db.get(Person, row.person_id)
    db.delete(row)
    db.flush()
    if person is not None:
        prune_organization_contacted_if_no_account_pairs(
            db,
            organization_id=int(person.organization_id),
            person_ids={int(person.id)},
        )
    db.commit()
    return _contacted_list_redirect(q=ret_q, page=ret_page, account_id=ret_account_id)
