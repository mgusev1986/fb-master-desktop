"""Reddit templates router."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import templates as templates_svc

router = APIRouter(prefix="/reddit/templates")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def templates_list(request: Request):
    kind = request.query_params.get("kind", "").strip() or None
    db = SessionLocal()
    try:
        rows = templates_svc.list_templates(db, _org_id(request), kind=kind)
        serialized = [templates_svc.serialize(r) for r in rows]
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/templates/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_templates",
            "templates": serialized,
            "active_kind": kind or "",
            "template_kinds": templates_svc.TEMPLATE_KINDS,
            "known_placeholders": templates_svc.KNOWN_PLACEHOLDERS,
            "tone_presets": templates_svc.TONE_PRESETS,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def template_new(request: Request):
    return _tpl(request).TemplateResponse(
        "reddit/templates/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_templates",
            "row": None,
            "template_kinds": templates_svc.TEMPLATE_KINDS,
            "tone_presets": templates_svc.TONE_PRESETS,
            "known_placeholders": templates_svc.KNOWN_PLACEHOLDERS,
        },
    )


@router.get("/{template_id}/edit", response_class=HTMLResponse)
async def template_edit(template_id: int, request: Request):
    db = SessionLocal()
    try:
        row = templates_svc.get_template(db, template_id)
        serialized = templates_svc.serialize(row) if row else None
    finally:
        db.close()
    if serialized is None:
        return RedirectResponse("/reddit/templates", status_code=303)
    return _tpl(request).TemplateResponse(
        "reddit/templates/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_templates",
            "row": serialized,
            "template_kinds": templates_svc.TEMPLATE_KINDS,
            "tone_presets": templates_svc.TONE_PRESETS,
            "known_placeholders": templates_svc.KNOWN_PLACEHOLDERS,
        },
    )


@router.post("/save")
async def template_save(
    request: Request,
    template_id: str = Form(default=""),
    name: str = Form(default=""),
    kind: str = Form(default="dm"),
    body: str = Form(default=""),
    tone: str = Form(default=""),
    variant_mode: str = Form(default="random"),
    variants_text: str = Form(default=""),
    banned_phrases_text: str = Form(default=""),
):
    variants = [v.strip() for v in variants_text.splitlines() if v.strip()]
    banned = [v.strip() for v in banned_phrases_text.splitlines() if v.strip()]
    db = SessionLocal()
    try:
        if template_id.strip().isdigit():
            templates_svc.update_template(
                db, int(template_id),
                name=name, kind=kind, body=body, tone=tone,
                variants=variants, variant_mode=variant_mode, banned_phrases=banned,
            )
        else:
            templates_svc.create_template(
                db, _org_id(request),
                name=name, kind=kind, body=body, tone=tone,
                variants=variants, variant_mode=variant_mode, banned_phrases=banned,
            )
    finally:
        db.close()
    return RedirectResponse("/reddit/templates", status_code=303)


@router.post("/{template_id}/delete")
async def template_delete(template_id: int, request: Request):
    db = SessionLocal()
    try:
        templates_svc.delete_template(db, template_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/templates", status_code=303)


@router.post("/{template_id}/generate-variations")
async def template_generate_variations(
    template_id: int,
    request: Request,
    count: int = Form(default=3),
    tone_hint: str = Form(default=""),
):
    db = SessionLocal()
    try:
        row = templates_svc.get_template(db, template_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "template not found"}, status_code=404)
        payload = templates_svc.generate_variations(db, row, count=max(1, min(8, count)), tone_hint=tone_hint.strip() or None)
    finally:
        db.close()
    return JSONResponse(payload)


@router.post("/check-banned")
async def template_check_banned(request: Request, body: str = Form(default=""), banned_phrases_text: str = Form(default="")):
    banned = [b.strip() for b in banned_phrases_text.splitlines() if b.strip()]
    hits = templates_svc.banned_phrases_hit(body, banned)
    return JSONResponse({"hits": hits})


# ── AI ops через общий LLM-сервис (variations / rewrite / shorten / soften) ──


@router.post("/{template_id}/ai")
async def template_ai_op(
    template_id: int,
    request: Request,
    op: str = Form(...),
    intent: str = Form(default=""),
    n: int = Form(default=3),
):
    """AI-операции над шаблоном.

    op = "variations" | "rewrite" | "shorten" | "soften"
    Для variations N результатов сохраняются в `variants_json`. Для rewrite/
    shorten/soften — новый текст становится `body`, старый body уходит в
    backup `variants_json[0]` (можно откатить через apply-variant).
    """
    from backend.modules.reddit.services import template_ai

    db = SessionLocal()
    try:
        tpl = templates_svc.get_template(db, template_id)
        if tpl is None:
            return JSONResponse({"ok": False, "reason_code": "not_found"}, status_code=404)
        if op == "variations":
            res = await template_ai.generate_variations(db, tpl.body or "", n=n)
            if res.get("ok"):
                tpl.variants_json = res.get("variants") or []
                from datetime import datetime, timezone

                tpl.updated_at = datetime.now(timezone.utc)
                db.commit()
        elif op in ("rewrite", "shorten", "soften"):
            if op == "rewrite":
                res = await template_ai.rewrite(db, tpl.body or "", intent or "more natural")
            elif op == "shorten":
                res = await template_ai.shorten(db, tpl.body or "", max_chars=400)
            else:
                res = await template_ai.soften(db, tpl.body or "")
            if res.get("ok"):
                variants = list(tpl.variants_json or [])
                if tpl.body and tpl.body not in variants:
                    variants.insert(0, tpl.body)
                tpl.variants_json = variants[:8]
                tpl.body = res["text"]
                from datetime import datetime, timezone

                tpl.updated_at = datetime.now(timezone.utc)
                db.commit()
        else:
            return JSONResponse({"ok": False, "reason_code": "unknown_op"}, status_code=400)
    finally:
        db.close()
    return JSONResponse(res)


@router.post("/{template_id}/apply-variant")
async def template_apply_variant(template_id: int, request: Request, variant_idx: int = Form(...)):
    db = SessionLocal()
    try:
        tpl = templates_svc.get_template(db, template_id)
        if tpl is None:
            return JSONResponse({"ok": False, "reason_code": "not_found"}, status_code=404)
        variants = list(tpl.variants_json or [])
        try:
            new_body = variants[int(variant_idx)]
        except (IndexError, ValueError):
            return JSONResponse({"ok": False, "reason_code": "variant_oob"}, status_code=400)
        if tpl.body and tpl.body not in variants:
            variants.insert(0, tpl.body)
        tpl.variants_json = variants[:8]
        tpl.body = new_body
        from datetime import datetime, timezone

        tpl.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    return JSONResponse({"ok": True, "body": new_body})


__all__ = ["router"]
