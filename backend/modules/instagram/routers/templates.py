"""Instagram templates router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.instagram.services import templates as templates_svc

router = APIRouter(prefix="/instagram/templates")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def templates_index(request: Request):
    db = SessionLocal()
    try:
        items = templates_svc.list_templates(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/templates/list.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_templates",
            "templates": items, "kinds": templates_svc.VALID_KINDS,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def templates_create(request: Request, name: str = Form(...), kind: str = Form(default="dm_first_touch"), body: str = Form(...), tone: str = Form(default="")):
    db = SessionLocal()
    try:
        try:
            templates_svc.create_template(db, _org_id(request), name=name, kind=kind, body=body, tone=tone)
        except ValueError:
            return RedirectResponse("/instagram/templates?notice=missing_body", status_code=303)
    finally:
        db.close()
    return RedirectResponse("/instagram/templates?notice=created", status_code=303)


@router.post("/{template_id}/delete")
async def templates_delete(template_id: int, request: Request):
    db = SessionLocal()
    try:
        templates_svc.delete_template(db, template_id)
    finally:
        db.close()
    return RedirectResponse("/instagram/templates?notice=deleted", status_code=303)


@router.post("/{template_id}/ai")
async def templates_ai(template_id: int, request: Request, op: str = Form(...), intent: str = Form(default=""), n: int = Form(default=3)):
    from backend.modules.instagram.services import template_ai

    db = SessionLocal()
    try:
        tpl = templates_svc.get_template(db, template_id)
        if tpl is None:
            return JSONResponse({"ok": False, "reason_code": "not_found"}, status_code=404)
        if op == "variations":
            res = await template_ai.generate_variations(db, tpl.body or "", n=n)
            if res.get("ok"):
                tpl.body_variants = res.get("variants") or []
                from datetime import datetime, timezone

                tpl.updated_at = datetime.now(timezone.utc)
                db.commit()
        elif op in ("rewrite", "shorten", "soften"):
            if op == "rewrite":
                res = await template_ai.rewrite(db, tpl.body or "", intent or "more natural")
            elif op == "shorten":
                res = await template_ai.shorten(db, tpl.body or "", max_chars=300)
            else:
                res = await template_ai.soften(db, tpl.body or "")
            if res.get("ok"):
                variants = list(tpl.body_variants or [])
                if tpl.body and tpl.body not in variants:
                    variants.insert(0, tpl.body)
                tpl.body_variants = variants[:8]
                tpl.body = res["text"]
                from datetime import datetime, timezone

                tpl.updated_at = datetime.now(timezone.utc)
                db.commit()
        else:
            return JSONResponse({"ok": False, "reason_code": "unknown_op"}, status_code=400)
    finally:
        db.close()
    return JSONResponse(res)


__all__ = ["router"]
