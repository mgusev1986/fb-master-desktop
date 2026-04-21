"""Страница FAQ со справкой по разделам (заглушки под будущий контент)."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["faq"])


@router.get("/faq")
async def faq_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "faq.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "faq",
        },
    )
