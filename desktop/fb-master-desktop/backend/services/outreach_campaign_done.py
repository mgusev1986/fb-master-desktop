"""Учёт успешно обработанных контактов в кампании рассылки: отдельно ЛС и комментинг."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.models import OutreachCampaign

LEGACY_DONE_KEY = "done_in_campaign"
DONE_DM_KEY = "done_outreach_dm"
DONE_COMMENT_KEY = "done_outreach_comment"


def outreach_campaign_kind_normalized(raw: Any) -> str:
    k = str(raw or "dm").strip().lower()
    if k in ("comment", "commenting", "comments"):
        return "comment"
    return "dm"


def _done_storage_key(kind: str) -> str:
    return DONE_COMMENT_KEY if kind == "comment" else DONE_DM_KEY


def _parse_done_id_list(raw: Any) -> set[int]:
    if not raw or not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for x in raw:
        if isinstance(x, int):
            out.add(x)
        elif isinstance(x, str) and x.strip().isdigit():
            out.add(int(x.strip()))
    return out


def outreach_done_person_ids(cfg: dict[str, Any] | None) -> set[int]:
    """
    ID людей, которых для этой кампании уже не берём в очередь после успеха.
    Для кампании ЛС — только успешные личные сообщения; для комментинга — только успешные комментарии.
    Старые кампании: единый список done_in_campaign читается как fallback для текущего типа кампании.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    kind = outreach_campaign_kind_normalized(cfg.get("campaign_kind"))
    key = _done_storage_key(kind)
    cur = _parse_done_id_list(cfg.get(key))
    if cur:
        return cur
    return _parse_done_id_list(cfg.get(LEGACY_DONE_KEY))


def outreach_append_done_person(db: Session, camp: OutreachCampaign, person_id: int) -> None:
    """Записать успех по текущему типу кампании (ЛС или комментарий), не смешивая типы."""
    cfg = dict(camp.config or {})
    kind = outreach_campaign_kind_normalized(cfg.get("campaign_kind"))
    key = _done_storage_key(kind)
    dl = list(cfg.get(key) or [])
    pid = int(person_id)
    if pid not in dl:
        dl.append(pid)
    cfg[key] = dl
    camp.config = cfg
    db.commit()


def _id_in_done_targets(x: Any, targets: set[int]) -> bool:
    try:
        if isinstance(x, int):
            return x in targets
        if isinstance(x, str) and x.strip().isdigit():
            return int(x.strip()) in targets
    except Exception:
        pass
    return False


def outreach_strip_done_targets(cfg: dict[str, Any], person_ids: set[int]) -> dict[str, Any]:
    """Убрать людей из всех списков done (новые и legacy)."""
    cfg = dict(cfg)
    for k in (DONE_DM_KEY, DONE_COMMENT_KEY, LEGACY_DONE_KEY):
        raw = cfg.get(k)
        if not raw or not isinstance(raw, list):
            continue
        cfg[k] = [x for x in raw if not _id_in_done_targets(x, person_ids)]
    return cfg


def outreach_merge_queue_done_into_cfg(
    cfg: dict[str, Any], queue_done_person_ids: list[int]
) -> dict[str, Any]:
    """Перед сбросом очереди: перенести done из строк очереди в список для типа кампании."""
    cfg = dict(cfg)
    kind = outreach_campaign_kind_normalized(cfg.get("campaign_kind"))
    key = _done_storage_key(kind)
    cur = list(cfg.get(key) or [])
    seen = set(_parse_done_id_list(cur))
    seen |= _parse_done_id_list(cfg.get(LEGACY_DONE_KEY))
    for pid in queue_done_person_ids:
        if pid is None:
            continue
        p = int(pid)
        if p not in seen:
            cur.append(p)
            seen.add(p)
    cfg[key] = cur
    return cfg
