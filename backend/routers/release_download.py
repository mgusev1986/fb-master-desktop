"""Выдача установщиков из static/releases по подписанной ссылке."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import BASE_DIR
from backend.services import release_download as rd

router = APIRouter(tags=["public"])
_STATIC_DIR = (BASE_DIR / "static").resolve()


@router.api_route("/download/release", methods=["GET", "HEAD"])
async def download_signed_release(
    f: str = Query("", alias="f", max_length=256),
    exp: str = Query("", max_length=32),
    sig: str = Query("", max_length=128),
):
    if not rd.release_signing_enabled():
        raise HTTPException(status_code=404, detail="signed_downloads_disabled")
    if not rd.verify_release_token(f, exp, sig):
        raise HTTPException(status_code=403, detail="invalid_or_expired_link")
    path = rd.resolved_release_file(_STATIC_DIR, f)
    if path is None:
        raise HTTPException(status_code=404, detail="file_not_found")
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="attachment",
    )
