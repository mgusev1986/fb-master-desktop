"""Заголовок Content-Disposition с не-ASCII именем файла (RFC 5987)."""

from __future__ import annotations

from urllib.parse import quote


def attachment_content_disposition(pretty_filename: str, *, ascii_filename: str) -> str:
    """
    pretty_filename — имя для современных браузеров (filename*=UTF-8''…).
    ascii_filename — только ASCII (fallback), например Import_batch_3.xlsx.
    """
    fn = (pretty_filename or ascii_filename).replace("\r", "").replace("\n", "")
    asc = "".join(
        c for c in (ascii_filename or "export.xlsx") if 32 <= ord(c) < 127 and c not in '\\"'
    ) or "export.xlsx"
    return f"attachment; filename=\"{asc}\"; filename*=UTF-8''{quote(fn, safe='')}"
