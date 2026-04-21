"""Reddit templates — CRUD, рендер, banned-phrase detection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import RedditTemplate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


TEMPLATE_KINDS = ("dm", "chat_opener", "comment", "follow_up")
TONE_PRESETS = ("friendly", "formal", "casual", "expert", "concise")

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
KNOWN_PLACEHOLDERS = (
    "username",
    "subreddit",
    "last_post_title",
    "last_comment_body",
    "topic",
    "karma",
)


def extract_placeholders(body: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(body or "")))


def render_body(body: str, context: dict[str, Any]) -> str:
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        return str(context.get(key, "{" + key + "}"))

    return PLACEHOLDER_RE.sub(_sub, body or "")


# ── CRUD ──────────────────────────────────────────────────────────────


def list_templates(
    db: "Session",
    organization_id: int | None,
    kind: str | None = None,
) -> list[RedditTemplate]:
    q = db.query(RedditTemplate).filter(RedditTemplate.organization_id == organization_id)
    if kind:
        q = q.filter(RedditTemplate.kind == kind)
    return q.order_by(RedditTemplate.kind.asc(), RedditTemplate.id.desc()).all()


def get_template(db: "Session", template_id: int) -> RedditTemplate | None:
    return db.get(RedditTemplate, int(template_id))


def create_template(
    db: "Session",
    organization_id: int | None,
    *,
    name: str,
    kind: str,
    body: str,
    tone: str | None = None,
    variants: list[str] | None = None,
    variant_mode: str = "random",
    banned_phrases: list[str] | None = None,
) -> RedditTemplate:
    row = RedditTemplate(
        organization_id=organization_id,
        name=(name or "").strip()[:200] or "Template",
        kind=kind if kind in TEMPLATE_KINDS else "dm",
        body=body or "",
        placeholders_json=extract_placeholders(body),
        tone=(tone or "").strip() or None,
        variants_json=variants or None,
        variant_mode=variant_mode if variant_mode in ("random", "sequential") else "random",
        banned_phrases_json=banned_phrases or None,
    )
    db.add(row)
    db.commit()
    return row


def update_template(
    db: "Session",
    template_id: int,
    *,
    name: str | None = None,
    kind: str | None = None,
    body: str | None = None,
    tone: str | None = None,
    variants: list[str] | None = None,
    variant_mode: str | None = None,
    banned_phrases: list[str] | None = None,
) -> RedditTemplate | None:
    row = db.get(RedditTemplate, int(template_id))
    if row is None:
        return None
    if name is not None:
        row.name = (name or "").strip()[:200] or row.name
    if kind is not None and kind in TEMPLATE_KINDS:
        row.kind = kind
    if body is not None:
        row.body = body
        row.placeholders_json = extract_placeholders(body)
    if tone is not None:
        row.tone = (tone or "").strip() or None
    if variants is not None:
        row.variants_json = variants or None
    if variant_mode is not None and variant_mode in ("random", "sequential"):
        row.variant_mode = variant_mode
    if banned_phrases is not None:
        row.banned_phrases_json = banned_phrases or None
    db.commit()
    return row


def delete_template(db: "Session", template_id: int) -> None:
    row = db.get(RedditTemplate, int(template_id))
    if row is None:
        return
    db.delete(row)
    db.commit()


# ── Banned-phrase detection ───────────────────────────────────────────


_DEFAULT_BANNED = (
    "click here",
    "buy now",
    "dm me",
    "telegram me",
    "crypto pump",
    "free money",
    "guaranteed",
    "join my group",
)


def banned_phrases_hit(body: str, banned: list[str] | None = None) -> list[str]:
    text = (body or "").lower()
    phrases = list(banned or ()) + list(_DEFAULT_BANNED)
    return [p for p in phrases if p and p.lower() in text]


# ── AI variations (переиспользуем FB ai_agent_service если можем) ─────


def generate_variations(
    db: "Session",
    template: RedditTemplate,
    *,
    count: int = 3,
    tone_hint: str | None = None,
) -> dict[str, Any]:
    """Сгенерировать AI-варианты. Возвращает {'ok', 'variants', 'error', 'model'}.

    Если AI-ключи не настроены — корректно отдаёт ok=False с понятной
    причиной, не падает.
    """
    try:
        from backend.services.ai_agent_service import generate_variations_generic  # type: ignore[attr-defined]
    except ImportError:
        generate_variations_generic = None  # type: ignore[assignment]

    if generate_variations_generic is None:
        # Fallback: попробуем прямую генерацию через имеющийся механизм.
        return _fallback_ai_variations(db, template, count=count, tone_hint=tone_hint)

    try:
        result = generate_variations_generic(
            db,
            body=template.body,
            count=count,
            tone=tone_hint or template.tone or "friendly",
            context="reddit_" + (template.kind or "dm"),
        )
        return {"ok": True, "variants": list(result.get("variants") or []), "model": result.get("model"), "error": None}
    except Exception as exc:
        return _fallback_ai_variations(db, template, count=count, tone_hint=tone_hint, last_error=str(exc))


def _fallback_ai_variations(
    db: "Session",
    template: RedditTemplate,
    *,
    count: int = 3,
    tone_hint: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    """Простейший fallback: модификации из исходного текста через простые эвристики.

    Это не AI, но даёт работоспособный flow, когда LLM-провайдеры недоступны.
    Пользователю в UI чётко указываем: «сгенерировано без AI».
    """
    base = (template.body or "").strip()
    if not base:
        return {"ok": False, "variants": [], "model": None, "error": last_error or "template body is empty"}

    variants = [base]
    if count >= 2:
        variants.append(base.rstrip(".!") + " — what do you think?")
    if count >= 3:
        variants.append("Hey " + base[0].lower() + base[1:] if base[:1].isalpha() else base)
    if count >= 4:
        variants.append(base + "\n\nHappy to share more context if useful.")
    variants = variants[:count]
    return {
        "ok": True,
        "variants": variants,
        "model": "fallback-heuristic",
        "error": None,
        "note": "AI-провайдеры недоступны (проверьте /system/settings). Показаны простые вариации.",
    }


def serialize(row: RedditTemplate) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "body": row.body,
        "tone": row.tone,
        "placeholders": row.placeholders_json or [],
        "variants": row.variants_json or [],
        "variant_mode": row.variant_mode,
        "banned_phrases": row.banned_phrases_json or [],
    }


__all__ = [
    "KNOWN_PLACEHOLDERS",
    "TEMPLATE_KINDS",
    "TONE_PRESETS",
    "banned_phrases_hit",
    "create_template",
    "delete_template",
    "extract_placeholders",
    "generate_variations",
    "get_template",
    "list_templates",
    "render_body",
    "serialize",
    "update_template",
]
