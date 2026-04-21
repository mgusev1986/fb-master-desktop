"""LinkedIn sequences (Agent Mode) — UI и runtime запуск."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.linkedin.services import accounts as accounts_svc
from backend.modules.linkedin.services import leads as leads_svc
from backend.modules.linkedin.services import sequences as seq_svc
from backend.modules.linkedin.services import templates as templates_svc

router = APIRouter(prefix="/linkedin/sequences")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def sequences_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        sequences = seq_svc.list_sequences(db, org_id)
        templates = templates_svc.list_templates(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        # Шаблон по умолчанию для UI-предзаполнения.
        default_graph = seq_svc.make_default_invite_then_dm_template()
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/sequences/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_sequences",
            "sequences": sequences,
            "templates": templates,
            "accounts": accounts,
            "default_graph_json": json.dumps(default_graph, ensure_ascii=False, indent=2),
            "node_types": seq_svc.VALID_NODE_TYPES,
            "edge_labels": seq_svc.VALID_EDGE_LABELS,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def sequences_create(
    request: Request,
    name: str = Form(...),
    graph_json: str = Form(...),
):
    db = SessionLocal()
    try:
        parsed = seq_svc.parse_graph_from_json_text(graph_json)
        if parsed is None:
            return RedirectResponse("/linkedin/sequences?notice=invalid_json", status_code=303)
        nodes, edges = parsed
        try:
            seq_svc.create_sequence(db, _org_id(request), name=name, nodes=nodes, edges=edges)
        except ValueError as e:
            return RedirectResponse(f"/linkedin/sequences?notice=invalid_graph&err={str(e)[:80]}", status_code=303)
    finally:
        db.close()
    return RedirectResponse("/linkedin/sequences?notice=created", status_code=303)


@router.post("/{seq_id}/archive")
async def sequences_archive(seq_id: int, request: Request):
    db = SessionLocal()
    try:
        seq_svc.archive_sequence(db, seq_id)
    finally:
        db.close()
    return RedirectResponse("/linkedin/sequences?notice=archived", status_code=303)


@router.get("/{seq_id}", response_class=HTMLResponse)
async def sequences_detail(seq_id: int, request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        seq = seq_svc.get_sequence(db, seq_id)
        if seq is None or seq.organization_id != org_id:
            raise HTTPException(status_code=404, detail="sequence_not_found")
        runs = seq_svc.list_runs(db, seq_id)
        leads = leads_svc.list_leads(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/sequences/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_sequences",
            "sequence": seq,
            "runs": runs,
            "leads": leads,
            "accounts": accounts,
            "graph_json": json.dumps({"nodes": seq.nodes_json, "edges": seq.edges_json}, ensure_ascii=False, indent=2),
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{seq_id}/start-run")
async def sequences_start_run(
    seq_id: int,
    request: Request,
    account_id: int = Form(...),
    lead_ids: list[int] = Form(default=[]),
):
    db = SessionLocal()
    try:
        started = 0
        skipped = 0
        for lid in lead_ids or []:
            try:
                seq_svc.start_run(db, seq_id, int(lid), int(account_id))
                started += 1
            except Exception:
                skipped += 1
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/sequences/{seq_id}?notice=started&started={started}&skipped={skipped}", status_code=303)


@router.post("/{seq_id}/step-now")
async def sequences_step_now(seq_id: int, request: Request, run_id: int = Form(...)):
    """Сделать один step.run для конкретного run-id."""
    from backend.modules.linkedin.runtime.sequence_worker import step_run
    from backend.modules.linkedin.models import LinkedInSequenceRun

    db = SessionLocal()
    try:
        run = db.get(LinkedInSequenceRun, int(run_id))
        if run is None or run.sequence_id != seq_id:
            return RedirectResponse(f"/linkedin/sequences/{seq_id}?notice=run_not_found", status_code=303)
        try:
            res = step_run(db, run, headless=True)
            qs = "step_ok" if res.get("ok") else f"step_failed_{res.get('reason') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"step_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/sequences/{seq_id}?notice={qs}", status_code=303)


__all__ = ["router"]
