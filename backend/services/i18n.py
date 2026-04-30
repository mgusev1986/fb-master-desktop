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
    """Прочитать язык: query-параметр `?lang=` имеет приоритет, иначе cookie.

    Logic:
    1. Если `?lang=ru|en` в URL — используем его (middleware параллельно
       перезапишет cookie на это же значение, см. app_factory.py).
    2. Иначе — читаем cookie `lang` (если был установлен предыдущим визитом).
    3. Иначе — `DEFAULT_LANG` (ru).

    Это даёт persistence между переходами по сайдбару (пользователь нажал EN
    один раз → cookie сохранил → все следующие страницы остаются на EN, даже
    без `?lang=` в URL). При этом пользователь в любой момент может явно
    переключиться обратно через `?lang=ru` — middleware перезапишет cookie.
    """
    # 1. Query-параметр имеет наивысший приоритет
    raw_q = request.query_params.get("lang", "").strip().lower()
    if raw_q in SUPPORTED_LANGS:
        return raw_q
    # 2. Cookie от предыдущего визита (если был ?lang=)
    raw_c = request.cookies.get("lang", "").strip().lower()
    if raw_c in SUPPORTED_LANGS:
        return raw_c
    # 3. Default
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


def translate(key: str, lang: str = DEFAULT_LANG, **kwargs: object) -> str:
    """Перевести строку. Если перевода нет — вернуть оригинал (RU).

    Использование в Python::

        translate("Готов", "en")              # "Ready"
        translate("Привет, {name}", "en", name="Ivan")  # "Hello, Ivan"

    Использование в Jinja2 (через `t` global, см. app_factory.py)::

        {{ t("Готов") }}                      # автоматически берёт lang
        {{ t("Привет, {name}", name=user.name) }}

    Если key не найден в `_TRANSLATIONS["en"]` — fallback на `key` как есть
    (это RU-источник). Это позволяет постепенно покрывать переводы и не
    бояться промежуточных коммитов: непереведённые строки просто остаются
    на русском.
    """
    # Импорт лениво, чтобы избежать circular imports при первом старте
    from backend.services.i18n_dict import TRANSLATIONS

    raw = key
    if lang == "en":
        raw = TRANSLATIONS.get("en", {}).get(key, key)
    if kwargs:
        try:
            return raw.format(**kwargs)
        except (KeyError, IndexError):
            return raw
    return raw
