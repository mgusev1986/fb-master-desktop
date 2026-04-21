"""Главная страница (дашборд)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Donor, FBAccount, Job, Person
from backend.services.tenancy import require_org_id

router = APIRouter(tags=["dashboard"])


@router.get("/home")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    stats = {
        "donors": db.query(Donor).filter(Donor.organization_id == org_id).count(),
        "people": db.query(Person).filter(Person.organization_id == org_id).count(),
        "fb_accounts": db.query(FBAccount).filter(FBAccount.organization_id == org_id).count(),
        "recent_jobs": (
            db.query(Job)
            .filter(Job.organization_id == org_id)
            .order_by(Job.created_at.desc())
            .limit(5)
            .all()
        ),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": request.session.get("user"),
        "stats": stats,
        "page_id": "dashboard",
    })
