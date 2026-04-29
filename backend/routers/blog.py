"""Blog (knowledge base) router — публичная SEO-секция /blog.

URL'ы:
  GET /blog                             — список статей (с фильтрами через query)
  GET /blog/<slug>                       — отдельная статья
  GET /blog/category/<slug>              — редирект на /blog?category=<slug>
  GET /blog/tag/<slug>                   — редирект на /blog?tag=<slug>

Все handler'ы public, не требуют auth (whitelist в app_factory.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend import config as app_config
from backend.services import blog_service

router = APIRouter(tags=["blog"])


def _common_ctx(request: Request) -> dict:
    return {
        "request": request,
        "np_price_365": app_config.NOWPAYMENTS_PRICE_USD_365,
        "all_categories": blog_service.list_categories(),
        "all_tags": blog_service.list_tags(),
    }


@router.get("/blog", response_class=HTMLResponse)
async def blog_list_page(
    request: Request,
    category: str | None = Query(None, max_length=64),
    tag: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=200),
):
    """Список статей. Поддерживает фильтры: ?category=<slug>&tag=<slug>&q=<text>."""
    posts = blog_service.list_posts(category=category, tag=tag, query=q)
    selected_category = blog_service.get_category(category) if category else None
    selected_tag = blog_service.get_tag(tag) if tag else None

    ctx = _common_ctx(request)
    ctx.update({
        "posts": posts,
        "selected_category": selected_category,
        "selected_tag": selected_tag,
        "search_query": q or "",
        "total_count": len(posts),
    })
    templates = request.app.state.templates
    return templates.TemplateResponse("blog/list.html", ctx)


@router.get("/blog/category/{slug}")
async def blog_category_redirect(slug: str):
    return RedirectResponse(f"/blog?category={slug}", status_code=302)


@router.get("/blog/tag/{slug}")
async def blog_tag_redirect(slug: str):
    return RedirectResponse(f"/blog?tag={slug}", status_code=302)


@router.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_post_page(request: Request, slug: str):
    """Отдельная статья. Если slug не найден или draft — мягкий редирект на /blog."""
    post = blog_service.get_post(slug)
    if post is None:
        return RedirectResponse("/blog", status_code=302)

    category_obj = blog_service.get_category(post.get("category", ""))
    tag_objs = [blog_service.get_tag(t) for t in (post.get("tags") or [])]
    tag_objs = [t for t in tag_objs if t]
    related = blog_service.related_posts(post, limit=3)

    ctx = _common_ctx(request)
    ctx.update({
        "post": post,
        "category_obj": category_obj,
        "tag_objs": tag_objs,
        "related_posts": related,
    })
    templates = request.app.state.templates
    return templates.TemplateResponse("blog/post.html", ctx)
