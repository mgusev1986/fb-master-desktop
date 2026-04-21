#!/usr/bin/env python3
"""Smoke-проверка FB Master после структурных правок.

Задача — убедиться, что текущий FB Master не сломан после каждого
milestone multi-workspace shell (M1, M2, M3, ...):

  1) FastAPI-приложение создаётся без ошибок.
  2) Все существующие роутеры подключены, ни один URL из sidebar не
     пропал из Starlette-роутинга.
  3) Feature-флаги multi-workspace по умолчанию OFF — включение
     mult-workspace shell не должно автоматически случиться на VPS.
  4) ModuleRegistry импортируется и пуст (модули регистрируются с M3+).
  5) Jinja-шаблоны компилируются (базовый base.html + новый _shell/base.html).

Запуск:
  .venv/bin/python scripts/smoke_fb_master.py

Выход:
  0 — всё ок
  1 — найдены регрессии (подробности в stdout)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# В desktop-окружении лаунчер чистит DATABASE_URL/SUPABASE_DATABASE_URL
# и задаёт FB_MASTER_ACCESS_KEY_REQUIRED=1. Smoke должен имитировать
# "голый" сервер: access-key НЕ обязателен, БД — локальная SQLite.
os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DATABASE_URL", None)
os.environ.setdefault("FB_MASTER_ACCESS_KEY_REQUIRED", "0")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# Набор URL, которые обязаны существовать в приложении — это базовые
# пункты sidebar FB Master + публичные роуты, от которых зависят
# лицензии и обновления. Список расширяется по мере добавления новых
# разделов (reddit, launcher и пр.).
REQUIRED_ROUTES: list[tuple[str, str]] = [
    # (method, path-pattern)
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/auth/unlock"),
    ("POST", "/auth/access-key/activate"),
    ("GET", "/fb-accounts"),
    ("GET", "/account-branding"),
    ("GET", "/account-warming"),
    ("GET", "/donors"),
    ("GET", "/discovery"),
    ("GET", "/parser"),
    ("GET", "/import"),
    ("GET", "/people"),
    ("GET", "/templates"),
    ("GET", "/outreach"),
    ("GET", "/scenarios"),
    ("GET", "/messenger"),
    ("GET", "/messenger2"),
    ("GET", "/crm/funnel"),
    ("GET", "/system/speed"),
    ("GET", "/system/logs"),
    ("GET", "/system/settings"),
    ("GET", "/faq"),
    ("GET", "/buy"),
    ("GET", "/purchase"),
    ("GET", "/api/public/desktop-update"),
    ("POST", "/api/public/desktop-license/activate"),
    ("GET", "/download/release"),
    # multi-workspace shell:
    ("GET", "/workspaces"),
    ("POST", "/workspaces/select/{module_id}"),
]


def _registered_routes(app) -> set[tuple[str, str]]:
    """Собрать (method, path) пары из Starlette-роутинга приложения."""
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        if methods:
            for m in methods:
                if m in ("HEAD",):
                    continue
                out.add((m, path))
        else:
            out.add(("GET", path))  # mount-у может не быть methods
    return out


def _check_route(required: tuple[str, str], registered: set[tuple[str, str]]) -> bool:
    """Есть ли роут. Допускаем variadic (/{id}) — сравниваем по префиксу."""
    method, path = required
    for m, p in registered:
        if m != method:
            continue
        if p == path:
            return True
        # допуск для /parent: считаем, что /parent/{...} тоже удовлетворяет
        if p.startswith(path + "/"):
            return True
    return False


def check_feature_flags() -> list[str]:
    """Все multi-workspace-флаги должны быть OFF по умолчанию."""
    errors: list[str] = []
    try:
        from backend.core.feature_flags import (
            launcher_is_default_entry,
            multi_workspace_enabled,
            reddit_module_enabled,
            reddit_official_api_only,
            reddit_send_approval_mode,
        )
    except Exception as exc:  # pragma: no cover
        errors.append(f"cannot import backend.core.feature_flags: {exc!r}")
        return errors

    if multi_workspace_enabled():
        errors.append("multi_workspace_enabled() must default to False on boot")
    if reddit_module_enabled():
        errors.append("reddit_module_enabled() must default to False on boot")
    if not reddit_official_api_only():
        errors.append("reddit_official_api_only() must default to True")
    if reddit_send_approval_mode() != "manual":
        errors.append(
            f"reddit_send_approval_mode() must default to 'manual', got {reddit_send_approval_mode()!r}"
        )
    if launcher_is_default_entry():
        errors.append("launcher_is_default_entry() must default to False")
    return errors


def check_module_registry_pre_app() -> list[str]:
    """До create_app() реестр должен быть пуст (модули регистрируются в create_app)."""
    errors: list[str] = []
    try:
        from backend.core.modules import module_registry
    except Exception as exc:
        errors.append(f"cannot import backend.core.modules.module_registry: {exc!r}")
        return errors
    if len(module_registry) != 0:
        errors.append(
            f"module_registry must be empty before create_app(), got {list(module_registry.ids())!r}"
        )
    return errors


def check_module_registry_post_app() -> list[str]:
    """После create_app() реестр содержит facebook + reddit (full или stub)."""
    errors: list[str] = []
    try:
        from backend.core.modules import module_registry
    except Exception as exc:
        errors.append(f"cannot import backend.core.modules.module_registry: {exc!r}")
        return errors
    ids = set(module_registry.ids())
    required = {"facebook", "reddit"}
    missing = required - ids
    if missing:
        errors.append(f"module_registry missing modules: {sorted(missing)!r}")
    if not ids.issuperset(required):
        errors.append(f"expected at least {required!r}, got {sorted(ids)!r}")
    return errors


def check_templates() -> list[str]:
    """Базовые Jinja-шаблоны компилируются."""
    errors: list[str] = []
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except Exception as exc:
        errors.append(f"cannot import jinja2: {exc!r}")
        return errors
    tmpls = PROJECT_ROOT / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tmpls)),
        autoescape=select_autoescape(("html",)),
    )
    # M1: оба файла должны ПАРСИТЬСЯ без синтаксических ошибок
    # (runtime-переменные типа `url_for`, `static_v` отсутствуют — это ок
    # для parse-теста, проверяем только синтаксис).
    for name in ("base.html", "_shell/base.html"):
        try:
            env.parse(env.loader.get_source(env, name)[0])
        except Exception as exc:
            errors.append(f"template {name!r} failed to parse: {exc!r}")
    return errors


def check_app_and_routes() -> list[str]:
    """FastAPI-приложение создаётся, и все базовые URL присутствуют."""
    errors: list[str] = []
    try:
        from backend.app_factory import create_app
    except Exception as exc:
        errors.append(f"cannot import create_app: {exc!r}")
        return errors

    try:
        app = create_app()
    except Exception as exc:
        errors.append(f"create_app() raised: {exc!r}")
        return errors

    registered = _registered_routes(app)

    missing: list[tuple[str, str]] = []
    for req in REQUIRED_ROUTES:
        if not _check_route(req, registered):
            missing.append(req)
    if missing:
        errors.append("missing routes: " + ", ".join(f"{m} {p}" for m, p in missing))

    return errors


REDDIT_ROUTES: list[tuple[str, str]] = [
    ("GET", "/reddit"),
    ("GET", "/reddit/accounts"),
    ("POST", "/reddit/accounts/connect"),
    ("POST", "/reddit/accounts/{account_id}/refresh"),
    ("POST", "/reddit/accounts/{account_id}/disconnect"),
    ("POST", "/reddit/accounts/{account_id}/delete"),
    ("POST", "/reddit/accounts/{account_id}/label"),
    ("GET", "/reddit/oauth/callback"),
    ("GET", "/reddit/settings"),
    ("POST", "/reddit/settings"),
    ("GET", "/reddit/readiness"),
    ("GET", "/reddit/subreddits"),
    ("POST", "/reddit/subreddits/delete/{subreddit_id}"),
    ("GET", "/reddit/audience"),
    ("POST", "/reddit/audience/ingest"),
    ("GET", "/reddit/activity"),
    ("GET", "/reddit/leads"),
    ("POST", "/reddit/leads/bulk"),
    ("POST", "/reddit/leads/{lead_id}/update"),
    ("GET", "/reddit/templates"),
    ("GET", "/reddit/templates/new"),
    ("POST", "/reddit/templates/save"),
    ("POST", "/reddit/templates/{template_id}/delete"),
    ("POST", "/reddit/templates/{template_id}/generate-variations"),
    ("POST", "/reddit/templates/check-banned"),
    ("GET", "/reddit/campaigns"),
    ("GET", "/reddit/campaigns/new"),
    ("POST", "/reddit/campaigns/save"),
    ("GET", "/reddit/campaigns/{campaign_id}"),
    ("POST", "/reddit/campaigns/{campaign_id}/delete"),
    ("POST", "/reddit/campaigns/{campaign_id}/build-queue"),
    ("POST", "/reddit/campaigns/{campaign_id}/bulk-approve"),
    ("POST", "/reddit/campaigns/{campaign_id}/item/{item_id}/approve"),
    ("POST", "/reddit/campaigns/{campaign_id}/item/{item_id}/reject"),
    ("POST", "/reddit/campaigns/{campaign_id}/item/{item_id}/skip"),
    ("POST", "/reddit/campaigns/{campaign_id}/item/{item_id}/edit-body"),
    ("GET", "/reddit/sequences"),
    ("GET", "/reddit/sequences/new"),
    ("POST", "/reddit/sequences/save"),
    ("GET", "/reddit/sequences/{sequence_id}"),
    ("GET", "/reddit/sequences/{sequence_id}/edit"),
    ("POST", "/reddit/sequences/{sequence_id}/delete"),
    ("POST", "/reddit/sequences/{sequence_id}/enroll"),
    ("POST", "/reddit/sequences/{sequence_id}/run/{run_id}/advance"),
    ("POST", "/reddit/sequences/{sequence_id}/run/{run_id}/cancel"),
    ("GET", "/reddit/conversations"),
    ("GET", "/reddit/conversations/compose"),
    ("POST", "/reddit/conversations/compose"),
    ("GET", "/reddit/conversations/{conversation_id}"),
    ("POST", "/reddit/conversations/{conversation_id}/draft/{draft_id}/approve"),
    ("POST", "/reddit/conversations/{conversation_id}/draft/{draft_id}/reject"),
    ("POST", "/reddit/conversations/{conversation_id}/draft/{draft_id}/edit"),
    ("POST", "/reddit/conversations/{conversation_id}/draft/{draft_id}/send"),
    ("GET", "/reddit/comments"),
    ("POST", "/reddit/comments/new"),
    ("POST", "/reddit/comments/{draft_id}/edit"),
    ("POST", "/reddit/comments/{draft_id}/approve"),
    ("POST", "/reddit/comments/{draft_id}/reject"),
    ("POST", "/reddit/comments/{draft_id}/delete"),
    ("POST", "/reddit/comments/{draft_id}/publish"),
    ("GET", "/reddit/analytics"),
    ("GET", "/reddit/compliance"),
    ("POST", "/reddit/readiness/{account_id}/probe"),
]


def check_reddit_routes_when_enabled() -> list[str]:
    """Включаем FB_MASTER_REDDIT_MODULE_ENABLED=1, пересоздаём app, проверяем
    что 15 Reddit-роутов зарегистрированы."""
    errors: list[str] = []

    # reset singleton + flag
    try:
        from backend.core.modules import module_registry
        module_registry.reset_for_tests()
    except Exception as exc:
        errors.append(f"cannot reset module_registry: {exc!r}")
        return errors

    os.environ["FB_MASTER_REDDIT_MODULE_ENABLED"] = "1"

    try:
        from backend.app_factory import create_app
        app = create_app()
    except Exception as exc:
        errors.append(f"create_app() raised with reddit_enabled=1: {exc!r}")
        return errors

    registered = _registered_routes(app)
    missing: list[tuple[str, str]] = []
    for req in REDDIT_ROUTES:
        if not _check_route(req, registered):
            missing.append(req)
    if missing:
        errors.append("missing reddit routes: " + ", ".join(f"{m} {p}" for m, p in missing))

    # Реестр: реальный RedditModule (BETA), не stub
    try:
        mod = module_registry.get("reddit")
        if mod is None:
            errors.append("reddit module not registered when enabled")
        elif mod.__class__.__name__ == "RedditComingSoonModule":
            errors.append("reddit stub registered instead of full RedditModule when flag=1")
    except Exception as exc:
        errors.append(f"module_registry inspection failed: {exc!r}")

    return errors


def main() -> int:
    # Порядок важен: module_registry_pre_app ДОЛЖЕН выполниться ДО
    # check_app_and_routes, который триггерит регистрацию модулей.
    sections = [
        ("feature_flags", check_feature_flags),
        ("module_registry_pre_app", check_module_registry_pre_app),
        ("templates", check_templates),
        ("app_and_routes", check_app_and_routes),
        ("module_registry_post_app", check_module_registry_post_app),
        ("reddit_routes_when_enabled", check_reddit_routes_when_enabled),
    ]

    total_errors = 0
    for name, fn in sections:
        try:
            errs = fn()
        except Exception as exc:
            errs = [f"{name} check crashed: {exc!r}"]
        if errs:
            total_errors += len(errs)
            print(f"[FAIL] {name}:")
            for e in errs:
                print(f"   - {e}")
        else:
            print(f"[ OK ] {name}")

    if total_errors:
        print(f"\n{total_errors} smoke-errors. FB Master regression suspected.")
        return 1
    print("\nAll smoke checks passed. FB Master не тронут.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
