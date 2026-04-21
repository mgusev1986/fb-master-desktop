"""Reddit sequences router."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import sequences as seq_svc
from backend.modules.reddit.services import templates as templates_svc

router = APIRouter(prefix="/reddit/sequences")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def sequences_list(request: Request):
    db = SessionLocal()
    try:
        rows = seq_svc.list_sequences(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/sequences/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_sequences",
            "sequences": rows,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def sequence_new(request: Request):
    db = SessionLocal()
    try:
        tpls = templates_svc.list_templates(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/sequences/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_sequences",
            "sequence": None,
            "node_kinds": seq_svc.NODE_KINDS,
            "templates_list": tpls,
            "default_nodes_json": json.dumps([
                {"id": "n1", "kind": "draft_dm", "params": {"template_id": None}, "requires_approval": True, "description": "Create DM draft"},
                {"id": "n2", "kind": "send_dm", "params": {}, "requires_approval": False, "description": "Send approved draft"},
                {"id": "n3", "kind": "wait", "params": {"hours": 48}, "requires_approval": False, "description": "Wait 2 days"},
                {"id": "n4", "kind": "check_reply", "params": {}, "requires_approval": False, "description": "Check reply"},
            ], ensure_ascii=False, indent=2),
        },
    )


@router.get("/{sequence_id}", response_class=HTMLResponse)
async def sequence_detail(sequence_id: int, request: Request):
    state = request.query_params.get("state", "").strip() or None
    db = SessionLocal()
    try:
        seq = seq_svc.get_sequence(db, sequence_id)
        if seq is None:
            return RedirectResponse("/reddit/sequences", status_code=303)
        runs = seq_svc.list_runs(db, seq.id, state=state)
        tpls = templates_svc.list_templates(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/sequences/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_sequences",
            "sequence": seq,
            "runs": runs,
            "state_filter": state or "",
            "run_states": seq_svc.RUN_STATES,
            "templates_list": tpls,
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("/{sequence_id}/edit", response_class=HTMLResponse)
async def sequence_edit(sequence_id: int, request: Request):
    db = SessionLocal()
    try:
        seq = seq_svc.get_sequence(db, sequence_id)
        if seq is None:
            return RedirectResponse("/reddit/sequences", status_code=303)
        tpls = templates_svc.list_templates(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/sequences/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_sequences",
            "sequence": seq,
            "node_kinds": seq_svc.NODE_KINDS,
            "templates_list": tpls,
            "default_nodes_json": json.dumps(seq.nodes_json or [], ensure_ascii=False, indent=2),
        },
    )


@router.post("/save")
async def sequence_save(
    request: Request,
    sequence_id: str = Form(default=""),
    name: str = Form(default=""),
    status: str = Form(default="draft"),
    timezone_name: str = Form(default="Europe/Madrid"),
    nodes_json: str = Form(default="[]"),
):
    try:
        nodes = json.loads(nodes_json or "[]")
        if not isinstance(nodes, list):
            nodes = []
    except Exception:
        nodes = []
    db = SessionLocal()
    try:
        sid = int(sequence_id) if sequence_id.strip().isdigit() else None
        seq = seq_svc.save_sequence(
            db, _org_id(request),
            sequence_id=sid, name=name, status=status, nodes=nodes, timezone_name=timezone_name,
        )
        out_id = seq.id
    finally:
        db.close()
    return RedirectResponse(f"/reddit/sequences/{out_id}", status_code=303)


@router.post("/{sequence_id}/delete")
async def sequence_delete(sequence_id: int, request: Request):
    db = SessionLocal()
    try:
        seq_svc.delete_sequence(db, sequence_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/sequences", status_code=303)


@router.post("/{sequence_id}/enroll")
async def sequence_enroll(
    sequence_id: int,
    request: Request,
    lead_ids: str = Form(default=""),
    account_id: str = Form(default=""),
):
    ids = [int(x) for x in lead_ids.split(",") if x.strip().isdigit()]
    db = SessionLocal()
    try:
        seq = seq_svc.get_sequence(db, sequence_id)
        if seq is None:
            return RedirectResponse("/reddit/sequences", status_code=303)
        acc_id = int(account_id) if account_id.strip().isdigit() else None
        seq_svc.enroll_leads(db, seq, ids, account_id=acc_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/sequences/{sequence_id}?notice=enrolled", status_code=303)


@router.post("/{sequence_id}/run/{run_id}/advance")
async def run_advance(sequence_id: int, run_id: int, request: Request):
    db = SessionLocal()
    try:
        result = seq_svc.advance_run(db, run_id)
    finally:
        db.close()
    notice = result.get("message") or "advanced"
    return RedirectResponse(f"/reddit/sequences/{sequence_id}?notice={notice.replace(' ', '+')[:80]}", status_code=303)


@router.post("/{sequence_id}/run/{run_id}/cancel")
async def run_cancel(sequence_id: int, run_id: int, request: Request):
    db = SessionLocal()
    try:
        seq_svc.cancel_run(db, run_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/sequences/{sequence_id}", status_code=303)


__all__ = ["router"]
