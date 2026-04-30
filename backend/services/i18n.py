"""Простой i18n-хелпер для выбора Jinja2-шаблона по `?lang=` query-параметру.

Идея: рядом с `templates/<name>.html` (RU, дефолт) лежит `templates/<name>_en.html`
(EN). Если в запросе `?lang=en` И EN-файл существует — отдаём EN, иначе RU.

Это "плоский" подход без gettext / Babel: переводимый контент живёт прямо в
шаблонах. Подходит для маркетинговых страниц / блога, где переводов мало
(2 языка) и они ручные.

Использование в роутере::

    from backend.services.i18n import select_template, get_lang

    @router.get("/")
    async def root(request: Request):
        templates = request.app.state.templates
        return templates.TemplateResponse(
            select_template(request, "promo3.html"),
            {"request": request, "lang": get_lang(request), ...},
        )
"""

from __future__ import annotations

from typing import Final

from fastapi import Request

from backend.app_factory import TEMPLATES_DIR

SUPPORTED_LANGS: Final[tuple[str, ...]] = ("ru", "en")
DEFAULT_LANG: Final[str] = "ru"


def get_lang(request: Request) -> str:
    """Прочитать язык из `?lang=` (приоритет 1) или cookie `lang` (приоритет 2).

    Возвращает только язык из `SUPPORTED_LANGS`; всё остальное → `DEFAULT_LANG`.
    """
    raw = request.query_params.get("lang", "").strip().lower()
    if raw in SUPPORTED_LANGS:
        return raw
    cookie = (request.cookies.get("lang") or "").strip().lower()
    if cookie in SUPPORTED_LANGS:
        return cookie
    return DEFAULT_LANG


def select_template(request: Request, base_name: str) -> str:
    """Вернуть имя шаблона с учётом языка из запроса.

    `base_name` — имя RU-шаблона (например, `"promo3.html"`,
    `"billing/purchase.html"`). Если `lang == "en"` и файл `<base>_en.html`
    существует — отдаём его, иначе fallback на RU.
    """
    lang = get_lang(request)
    if lang == "en":
        en_name = _en_variant(base_name)
        if (TEMPLATES_DIR / en_name).exists():
            return en_name
    return base_name


def _en_variant(base_name: str) -> str:
    """`promo3.html` → `promo3_en.html`; `billing/purchase.html` → `billing/purchase_en.html`."""
    if base_name.endswith(".html"):
        return base_name[: -len(".html")] + "_en.html"
    return base_name + "_en"
