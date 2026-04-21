"""CRUD шаблонов сообщений (рассылка, прогрев и т.д.)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Template as MsgTemplate
from backend.services.ai_agent_llm import LLMError
from backend.services.ai_agent_service import load_ui_settings
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key
from backend.services.message_template_render import (
    count_template_variants,
    join_template_variants,
    normalize_name_placeholder_token,
    prepare_template_body_for_storage,
    preview_sample,
    split_template_variants,
)
from backend.services.template_variations_llm import generate_template_variations
from backend.services.tenancy import require_org_id

router = APIRouter(prefix="/templates", tags=["templates"])

CATEGORY_LABELS: dict[str, str] = {
    "outreach": "Рассылка (ЛС)",
    "warmup": "Прогрев",
    "comment": "Комментарии",
    "other": "Другое",
}

CATEGORIES = list(CATEGORY_LABELS.items())


def _llm_err_suggests_fallback(msg: str) -> bool:
    m = (msg or "").lower()
    return any(
        k in m
        for k in (
            "quota",
            "rate limit",
            "429",
            "billing",
            "exceeded",
            "resource exhausted",
            "free_tier",
            "limit: 0",
            "too many requests",
            "overloaded",
            "high demand",
            "spikes in demand",
            "overload",
            "unavailable",
            "503",
        )
    )


def _friendly_neuro_error(raw: str) -> str:
    r = (raw or "").strip()
    low = r.lower()
    if "quota" in low or "free_tier" in low or ("limit" in low and "exceeded" in low):
        return (
            "Квота Google Gemini исчерпана (часто так на бесплатном тарифе). "
            "Варианты: включить оплату в Google AI Studio / Google Cloud, подождать сброс лимита "
            "или задать в .env рабочий OPENAI_API_KEY и выбрать провайдера OpenAI."
        )
    if "openai" in low and ("quota" in low or "billing" in low or "insufficient" in low):
        return (
            "Лимит или оплата OpenAI: проверьте баланс и лимиты на platform.openai.com, "
            "либо используйте Gemini при наличии GOOGLE_API_KEY."
        )
    if "high demand" in low or "spikes in demand" in low:
        return (
            "Сервис Gemini временно перегружен. Повторите запрос позже, "
            "либо задайте ключ OpenAI и выберите провайдера OpenAI — программа попробует его автоматически."
        )
    _conn = "нет подключения" in low or "connection" in low or "connect" in low
    _local_provider = "ollama" in low or "локальн" in low
    if _conn and _local_provider:
        return (
            "Локальная модель недоступна: проверьте, что сервер нейросети запущен на этом ПК или по адресу из "
            "настроек ИИ-агента, и что выбранная модель загружена."
        )
    if len(r) > 400:
        return r[:400] + "…"
    return r or "Ошибка модели. Попробуйте другой провайдер или позже."


def _model_for_provider(ui: dict, provider: str) -> str:
    if provider == "openai":
        return str(ui.get("model_openai") or "gpt-4o-mini")
    if provider == "ollama":
        return str(ui.get("model_ollama") or "gemma4:e2b-it-q8_0")
    return str(ui.get("model_gemini") or "gemini-2.5-flash")


async def _neuro_variations_with_fallback(
    *,
    db: Session,
    preferred: str,
    ui: dict,
    source_body: str,
    count: int,
) -> tuple[list[str], str, bool]:
    """(variants, provider_used, used_fallback)."""
    oa = effective_openai_api_key(db)
    gk = effective_google_llm_api_key(db)
    order: list[str] = []
    if preferred in ("openai", "gemini", "ollama"):
        order.append(preferred)
    for alt in ("openai", "gemini", "ollama"):
        if alt not in order:
            order.append(alt)

    last_err = ""
    for p in order:
        if p == "openai" and not oa.strip():
            continue
        if p == "gemini" and not gk.strip():
            continue
        model = _model_for_provider(ui, p)
        try:
            variants = await generate_template_variations(
                provider=p,
                openai_key=oa,
                google_key=gk,
                ollama_base_url=str(ui.get("ollama_base_url") or "http://127.0.0.1:11434"),
                model=model,
                source_body=source_body,
                count=count,
            )
            return variants, p, p != preferred
        except LLMError as e:
            last_err = str(e)
            if _llm_err_suggests_fallback(last_err) and p != order[-1]:
                continue
            break
    raise LLMError(_friendly_neuro_error(last_err))


def _redirect(msg: str, kind: str = "ok") -> RedirectResponse:
    return RedirectResponse(
        f"/templates?flash={quote(msg[:400], safe='')}&flash_type={kind}",
        status_code=303,
    )


@router.get("")
async def template_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    q = request.query_params.get("q", "").strip()
    cat = request.query_params.get("category", "").strip()
    query = (
        db.query(MsgTemplate)
        .filter(MsgTemplate.organization_id == org_id)
        .order_by(MsgTemplate.updated_at.desc())
    )
    if q:
        query = query.filter(
            MsgTemplate.name.ilike(f"%{q}%") | MsgTemplate.body.ilike(f"%{q}%")
        )
    if cat:
        query = query.filter(MsgTemplate.category == cat)
    items = query.all()
    flash = request.query_params.get("flash", "")
    flash_type = request.query_params.get("flash_type", "info")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "message_templates/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "items": items,
            "search": q,
            "category_filter": cat,
            "categories": CATEGORIES,
            "category_labels": CATEGORY_LABELS,
            "flash": flash,
            "flash_type": flash_type,
            "page_id": "templates",
            "preview_sample": preview_sample,
            "count_template_variants": count_template_variants,
            "ai_settings": load_ui_settings(db),
            "has_openai": bool(effective_openai_api_key(db).strip()),
            "has_gemini": bool(effective_google_llm_api_key(db).strip()),
            "has_ollama": True,
        },
    )


@router.get("/new")
async def template_new(request: Request, db: Session = Depends(get_db)):
    tpl = request.app.state.templates
    return tpl.TemplateResponse(
        "message_templates/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "row": None,
            "categories": CATEGORIES,
            "page_id": "templates",
            "preview_sample": preview_sample,
            "count_template_variants": count_template_variants,
            "ai_settings": load_ui_settings(db),
            "has_openai": bool(effective_openai_api_key(db).strip()),
            "has_gemini": bool(effective_google_llm_api_key(db).strip()),
            "has_ollama": True,
            "variants_for_editor": [""],
        },
    )


@router.get("/{template_id}/edit")
async def template_edit(template_id: int, request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    row = (
        db.query(MsgTemplate)
        .filter(MsgTemplate.id == template_id, MsgTemplate.organization_id == org_id)
        .first()
    )
    if not row:
        raise HTTPException(404)
    parts = split_template_variants(row.body or "")
    if not parts:
        parts = [""]
    tpl = request.app.state.templates
    return tpl.TemplateResponse(
        "message_templates/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "row": row,
            "categories": CATEGORIES,
            "page_id": "templates",
            "preview_sample": preview_sample,
            "count_template_variants": count_template_variants,
            "ai_settings": load_ui_settings(db),
            "has_openai": bool(effective_openai_api_key(db).strip()),
            "has_gemini": bool(effective_google_llm_api_key(db).strip()),
            "has_ollama": True,
            "variants_for_editor": parts,
        },
    )


@router.post("/{template_id:int}/neuro-variations")
async def template_neuro_variations(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    org_id = require_org_id(request, db)
    row = (
        db.query(MsgTemplate)
        .filter(MsgTemplate.id == template_id, MsgTemplate.organization_id == org_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "Шаблон не найден")
    form = await request.form()
    try:
        count = int((form.get("count") or "10").strip())
    except ValueError:
        count = 10
    count = max(1, min(count, 40))
    prov = (str(form.get("provider") or "").strip().lower())
    ui = load_ui_settings(db)
    if prov not in ("openai", "gemini", "ollama"):
        prov = str(ui.get("default_provider") or "gemini").lower()
        if prov not in ("openai", "gemini", "ollama"):
            prov = "gemini"
    oa = effective_openai_api_key(db)
    gk = effective_google_llm_api_key(db)
    if prov == "openai" and not oa.strip():
        prov = "gemini" if gk.strip() else "ollama"
    if prov == "gemini" and not gk.strip():
        prov = "openai" if oa.strip() else "ollama"
    if prov == "openai" and not oa.strip():
        return JSONResponse({"ok": False, "error": "Нет ключа OpenAI"}, status_code=400)
    if prov == "gemini" and not gk.strip():
        return JSONResponse({"ok": False, "error": "Нет ключа Google (Gemini)"}, status_code=400)
    src_parts = split_template_variants(row.body or "")
    source_body = src_parts[0] if src_parts else ""
    try:
        variants, used_p, used_fb = await _neuro_variations_with_fallback(
            db=db,
            preferred=prov,
            ui=ui,
            source_body=source_body,
            count=count,
        )
    except LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)[:2000]}, status_code=400)
    out: dict = {"ok": True, "variants": variants, "count": len(variants), "provider_used": used_p}
    if used_fb:
        out["fallback"] = True
    return JSONResponse(out)


@router.post("/{template_id:int}/neuro-variations-save")
async def template_neuro_variants_save(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Сохранить сгенерированные нейро-варианты в одно поле body шаблона."""
    org_id = require_org_id(request, db)
    row = (
        db.query(MsgTemplate)
        .filter(MsgTemplate.id == template_id, MsgTemplate.organization_id == org_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "Шаблон не найден")
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Некорректный JSON"}, status_code=400)
    variants = data.get("variants")
    if not isinstance(variants, list) or len(variants) < 1:
        return JSONResponse({"ok": False, "error": "Передайте непустой массив variants"}, status_code=400)
    parts = [normalize_name_placeholder_token(str(v).strip()) for v in variants if str(v).strip()]
    if not parts:
        return JSONResponse({"ok": False, "error": "Все варианты пустые"}, status_code=400)
    row.body = join_template_variants(parts)
    row.variant_cursor = 0
    db.commit()
    return JSONResponse({"ok": True, "saved": len(parts)})


@router.post("/save")
async def template_save(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    body: str = Form(...),
    category: str = Form("outreach"),
    template_id: int | None = Form(default=None),
    variant_pick_mode: str = Form("random"),
):
    org_id = require_org_id(request, db)
    name = name.strip()
    body = prepare_template_body_for_storage(body)
    if not name:
        return _redirect("Укажите название шаблона", "error")
    if not body:
        return _redirect("Текст шаблона не может быть пустым", "error")
    allowed = set(CATEGORY_LABELS.keys())
    if category not in allowed:
        category = "outreach"

    vpm = (variant_pick_mode or "random").strip().lower()
    if vpm not in ("random", "sequential"):
        vpm = "random"

    if template_id:
        row = (
            db.query(MsgTemplate)
            .filter(MsgTemplate.id == template_id, MsgTemplate.organization_id == org_id)
            .first()
        )
        if not row:
            raise HTTPException(404)
        row.name = name
        row.body = body
        row.category = category
        row.variant_pick_mode = vpm
    else:
        row = MsgTemplate(
            organization_id=org_id,
            name=name,
            body=body,
            category=category,
            variant_pick_mode=vpm,
        )
        db.add(row)
    db.commit()
    return _redirect("Шаблон сохранён", "ok")


@router.post("/{template_id}/delete")
async def template_delete(
    request: Request,
    template_id: int,
    db: Session = Depends(get_db),
    confirm: str = Form(""),
):
    org_id = require_org_id(request, db)
    row = (
        db.query(MsgTemplate)
        .filter(MsgTemplate.id == template_id, MsgTemplate.organization_id == org_id)
        .first()
    )
    if not row:
        raise HTTPException(404)
    if confirm != "yes":
        return _redirect("Отметьте подтверждение удаления", "error")
    db.delete(row)
    db.commit()
    return _redirect("Шаблон удалён", "ok")
