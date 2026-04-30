"""SEO endpoints: /robots.txt, /sitemap.xml, /favicon.ico.

Без этих 3 endpoint'ов любой URL (вкл. эти стандартные) проваливается на общий
auth-guard middleware и получает 303 redirect → /auth/unlock. Это ломает SEO
(crawler'ы Google/Yandex/Bing видят редирект на login-страницу при попытке
прочитать robots.txt) и UX (браузер пытается скачать favicon и получает
12 KB HTML вместо иконки).

Handler'ы whitelisted в auth_guard middleware (см. app_factory.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

router = APIRouter(tags=["seo"])


_ROBOTS_TXT = """User-agent: *
Allow: /
Allow: /buy
Allow: /purchase
Allow: /promo
Allow: /promo3
Allow: /blog

# Личный кабинет и админка — закрыты от индексации
Disallow: /home
Disallow: /admin/
Disallow: /auth/
Disallow: /api/
Disallow: /internal/
Disallow: /webhooks/
Disallow: /billing/
Disallow: /download/

# Установщики (динамические подписанные URL — нет смысла индексировать)
Disallow: /static/releases/
Allow: /static/css/
Allow: /static/js/
Allow: /static/images/
Allow: /static/downloads/

Sitemap: https://socmaster.pro/sitemap.xml
"""


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> PlainTextResponse:
    """Стандартный robots.txt для search engines."""
    return PlainTextResponse(content=_ROBOTS_TXT, media_type="text/plain; charset=utf-8")


def _sitemap_xml() -> str:
    """Генерирует sitemap.xml со списком публичных URL — RU + EN.

    Включает: главную, лендинги (/promo, /promo3), /buy, /purchase, /blog
    + все опубликованные статьи блога (динамически из data/blog/posts/).
    Кабинет, админка и API не индексируются.

    Двуязычность: для страниц с EN-версией (главная, /buy, /purchase, /blog,
    статьи блога с заполненным post.i18n.en) добавляются отдельные `?lang=en`
    URL'ы. Для статей без EN-перевода EN-версия пропускается.
    """
    from backend.services import blog_service

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = "https://socmaster.pro"

    # (loc, lastmod, changefreq, priority, has_en)
    # has_en=True → дополнительно генерим ?lang=en URL.
    urls: list[tuple[str, str, str, str, bool]] = [
        ("/", today, "weekly", "1.0", True),
        ("/promo3", today, "weekly", "0.9", True),
        ("/promo", today, "weekly", "0.7", False),  # старый лендинг — RU only
        ("/buy", today, "monthly", "0.9", True),
        ("/purchase", today, "monthly", "0.7", True),
        ("/blog", today, "daily", "0.9", True),
    ]
    # Категории блога — каждая категория двуязычна (name_en всегда есть в categories.json)
    for cat in blog_service.list_categories():
        urls.append((f"/blog?category={cat['slug']}", today, "weekly", "0.6", True))
    # Все опубликованные статьи. EN-URL только если есть фактический перевод.
    for post in blog_service.list_posts():
        lastmod = post.get("published_at") or today
        en = (post.get("i18n") or {}).get("en") or {}
        post_has_en = bool(isinstance(en, dict) and en.get("title_en") and en.get("content_en"))
        urls.append((f"/blog/{post['slug']}", lastmod, "monthly", "0.7", post_has_en))

    items = []
    for loc, lastmod, changefreq, priority, has_en in urls:
        # RU-вариант (default)
        items.append(
            f"  <url>\n"
            f"    <loc>{base}{loc}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            f"  </url>"
        )
        if has_en:
            # EN-вариант: добавляем ?lang=en (или &lang=en если уже есть `?`)
            sep = "&" if "?" in loc else "?"
            en_loc = f"{loc}{sep}lang=en"
            items.append(
                f"  <url>\n"
                f"    <loc>{base}{en_loc}</loc>\n"
                f"    <lastmod>{lastmod}</lastmod>\n"
                f"    <changefreq>{changefreq}</changefreq>\n"
                f"    <priority>{priority}</priority>\n"
                f"  </url>"
            )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(items)
        + "\n</urlset>\n"
    )


@router.get("/sitemap.xml")
async def sitemap_xml() -> Response:
    """sitemap.xml для search engines."""
    return Response(content=_sitemap_xml(), media_type="application/xml; charset=utf-8")


# Минимальная 1×1 PNG (прозрачная) — базовая favicon для предотвращения 303
# редиректа браузера. Реальная иконка должна лежать в static/images/favicon.ico
# и быть прописана в base.html через <link rel="icon">. Это fallback на случай
# когда браузер запрашивает /favicon.ico по умолчанию.
_FAVICON_FALLBACK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63600000000200015e9d8a380000000049454e44ae426082"
)


@router.get("/favicon.ico")
async def favicon_ico() -> Response:
    """Возвращает 200 с минимальной favicon (1×1 PNG fallback).

    Браузеры запрашивают /favicon.ico по умолчанию. Без этого handler'а
    запрос проваливался на auth-guard и получал 303→/auth/unlock — что
    сломало кэш favicon в browsers и засоряло логи.
    """
    return Response(
        content=_FAVICON_FALLBACK_PNG,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
