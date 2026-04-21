"""Стадии воронки CRM: сиды, порядок, подписи для Person.crm_stage (slug)."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Встроенные стадии (slug фиксированы — совместимость с уже сохранёнными Person.crm_stage)
BUILTIN_CRM_STAGES: list[tuple[str, str, int]] = [
    ("new", "Новый", 10),
    ("interested", "Заинтересован", 20),
    ("follow_up_later", "Написать позже", 30),
    ("conversion", "Конверсия", 40),
    ("refused", "Отказ", 50),
]

WORKFLOW_STAGE_LABEL_OVERRIDES: dict[str, str] = {
    # В рабочем CRM-контуре first touch уже произошёл, поэтому `new`
    # показываем как стадию состоявшегося контакта, не меняя сырые данные People.
    "new": "Уже контактировали",
}


def seed_builtin_stages(db: Session) -> None:
    """
    Если таблица пуста — вставить стандартный набор (первый запуск / всё удалили).
    Иначе только миграция подписи follow_up_later; удалённые вручную стадии не восстанавливаем.
    Без commit.
    """
    from backend.models import CRMStage

    total = db.query(CRMStage).count()
    if total == 0:
        for slug, label, order in BUILTIN_CRM_STAGES:
            db.add(
                CRMStage(
                    slug=slug,
                    label=label,
                    sort_order=order,
                    is_builtin=True,
                )
            )
        return

    r = db.query(CRMStage).filter(CRMStage.slug == "follow_up_later").first()
    if r and r.label in (
        "Подписаться позже",
        "Subscribe later",
        "To write later",
    ):
        r.label = "Написать позже"


def get_ordered_stages(db: Session):
    from backend.models import CRMStage

    rows = (
        db.query(CRMStage)
        .order_by(CRMStage.sort_order.asc(), CRMStage.id.asc())
        .all()
    )
    if not rows:
        seed_builtin_stages(db)
        db.commit()
        rows = (
            db.query(CRMStage)
            .order_by(CRMStage.sort_order.asc(), CRMStage.id.asc())
            .all()
        )
    return rows


def stage_slugs_ordered(db: Session) -> list[str]:
    return [s.slug for s in get_ordered_stages(db)]


def stage_labels_map(db: Session) -> dict[str, str]:
    return {s.slug: s.label for s in get_ordered_stages(db)}


def default_crm_stage_slug_for_new_person(db: Session) -> str:
    """Slug для нового Person: «new», если стадия есть в справочнике, иначе первая по порядку."""
    rows = get_ordered_stages(db)
    if not rows:
        return "new"
    if any(r.slug == "new" for r in rows):
        return "new"
    return rows[0].slug


def workflow_label_for_stage(slug: str, label: str | None = None) -> str:
    """Подпись стадии именно для рабочего CRM-потока (воронка/мессенджер)."""
    s = (slug or "").strip().lower()
    if s in WORKFLOW_STAGE_LABEL_OVERRIDES:
        return WORKFLOW_STAGE_LABEL_OVERRIDES[s]
    return (label or slug or "").strip()


def suggest_slug(label: str, db: Session) -> str:
    """Латинский slug из подписи или stage_<hex>, уникальный в crm_stages."""
    from backend.models import CRMStage

    raw = (label or "").strip().lower()
    ascii_part = raw.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "_", ascii_part).strip("_")[:40]
    if len(base) < 2:
        base = f"stage_{uuid.uuid4().hex[:10]}"
    cand = base[:50]
    n = 0
    while db.query(CRMStage).filter(CRMStage.slug == cand).first():
        n += 1
        suffix = f"_{n}"
        cand = (base[: 50 - len(suffix)] + suffix)[:50]
    return cand


def next_sort_order(db: Session) -> int:
    from backend.models import CRMStage

    m = db.query(func.max(CRMStage.sort_order)).scalar()
    return int(m or 0) + 10


def icon_class_for_crm_stage_slug(slug: str) -> str:
    """Класс иконки Tabler (`ti ti-…`) для slug стадии — мессенджер и воронка."""
    s = (slug or "").strip().lower()
    known = {
        "new": "ti-user-plus",
        "interested": "ti-flame",
        "follow_up_later": "ti-pencil",
        "conversion": "ti-trophy",
        "refused": "ti-ban",
    }
    if s in known:
        return known[s]
    if "call" in s or "phone" in s:
        return "ti-phone"
    if "later" in s or "write" in s:
        return "ti-clock"
    return "ti-tag"
