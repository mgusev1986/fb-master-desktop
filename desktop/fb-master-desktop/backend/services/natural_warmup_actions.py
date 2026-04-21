"""Мягкие действия для многодневного «натурального» прогрева FB-аккаунта (группы → профили → лайки → ИИ-комментарии → заявки в друзья → глубокий browse)."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Literal

from playwright.sync_api import Page
from sqlalchemy.orm import Session

from backend.services.ai_agent_llm import LLMError
from backend.services.fb_playwright import page_requires_facebook_login
from backend.services.natural_warmup_llm import (
    generate_natural_warmup_comment_sync,
    moderate_post_for_natural_warmup_sync,
)
from backend.services.sequence_actions import try_close_facebook_messenger_popup, try_friend_request_on_profile
from backend.services.sequence_ai_comment import extract_timeline_post_text
from backend.services.warmup_actions import try_comment_on_timeline, try_like_first_visible_post

logger = logging.getLogger(__name__)

# Общее замедление пауз относительно базовых значений (максимально «человечно»).
_SLOW = 3.4

NaturalWarmupPhase = Literal["groups", "profiles", "likes", "ai_comment", "add_friend", "browse"]

WarmupActionEmit = Callable[[str, dict[str, Any]], None]


def _deadline_ok(deadline_mono: float) -> bool:
    return time.monotonic() < deadline_mono


def human_pause(page: Page, lo: float = 0.8, hi: float = 2.8) -> None:
    page.wait_for_timeout(int(random.uniform(lo * _SLOW, hi * _SLOW) * 1000))


_FB_POST_MODAL_OPEN_JS = """() => {
  try {
    const p = (window.location.pathname || '').toLowerCase();
    if (p.includes('/permalink/')) return true;
    if (p.includes('/groups/') && p.includes('/posts/')) return true;
  } catch (e) {}
  const d = document.querySelector('[role="dialog"]');
  if (!d) return false;
  const r = d.getBoundingClientRect();
  if (r.width < 220 || r.height < 220 || r.bottom < 80) return false;
  const t = (d.innerText || '').slice(0, 200).toLowerCase();
  if (t.includes('публикация') || t.includes('publication') || t.includes('anonymous')
      || t.includes('аноним') || t.includes('комментировать как')) return true;
  return false;
}"""

_SCROLL_FB_DIALOG_OR_MAIN_TO_BOTTOM_JS = """() => {
  function rectOk(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 120 && r.height > 120 && r.bottom > 60;
  }
  const dlg = Array.from(document.querySelectorAll('[role="dialog"]')).find(rectOk);
  const root = dlg || document.querySelector('[role="main"]') || document.body;
  const scrollables = [];
  const walk = (el, depth) => {
    if (!el || depth > 14 || scrollables.length > 20) return;
    try {
      const st = window.getComputedStyle(el);
      const oy = st.overflowY;
      const sh = el.scrollHeight;
      const ch = el.clientHeight;
      if ((oy === 'auto' || oy === 'scroll') && sh > ch + 120) scrollables.push(el);
    } catch (e) {}
    const ch = el.children || [];
    for (let i = 0; i < ch.length; i++) walk(ch[i], depth + 1);
  };
  walk(root, 0);
  scrollables.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  const target = scrollables[0] || root;
  const top0 = target.scrollTop;
  target.scrollTop = target.scrollHeight;
  let moved = target.scrollTop - top0;
  for (let i = 0; i < 6; i++) {
    target.scrollTop = target.scrollHeight;
    moved = Math.max(moved, target.scrollTop - top0);
  }
  return { ok: true, moved: Math.round(moved), sh: target.scrollHeight, ch: target.clientHeight };
}"""


def maybe_dismiss_facebook_post_modal_after_scroll(page: Page) -> None:
    """
    Facebook иногда открывает пост в оверлее (/permalink/ или диалог «Публикация …»).
    Докручиваем контент вниз (как «дочитали комментарии»), затем закрываем — иначе прогрев зависает.
    """
    try:
        stuck = page.evaluate(_FB_POST_MODAL_OPEN_JS)
    except Exception:
        stuck = False
    if not stuck:
        return
    logger.info("natural_warmup: закрытие модалки поста (permalink / Публикация)")
    try:
        for _ in range(7):
            page.evaluate(_SCROLL_FB_DIALOG_OR_MAIN_TO_BOTTOM_JS)
            try:
                page.mouse.wheel(0, random.randint(350, 700))
            except Exception:
                pass
            page.wait_for_timeout(random.randint(180, 420))
    except Exception:
        logger.debug("scroll modal to bottom failed", exc_info=True)
    human_pause(page, 0.35, 0.9)
    try_close_facebook_messenger_popup(page)
    human_pause(page, 0.25, 0.55)
    try:
        u = (page.url or "").lower()
        # SPA-оверлей чаще всего именно …/permalink/… — возврат назад в ленту
        if "/permalink/" in u:
            page.go_back(wait_until="domcontentloaded", timeout=28_000)
            human_pause(page, 1.0, 2.2)
    except Exception:
        logger.debug("go_back after post modal failed", exc_info=True)


def soft_scroll(page: Page, *, times: int = 1) -> None:
    for _ in range(max(1, times)):
        dy = random.randint(120, 380)
        try:
            page.mouse.wheel(0, dy)
        except Exception:
            try:
                page.evaluate(f"window.scrollBy(0, {dy})")
            except Exception:
                logger.debug("soft_scroll failed", exc_info=True)
        human_pause(page, 0.55, 1.85)
    maybe_dismiss_facebook_post_modal_after_scroll(page)


def _ensure_logged_in(page: Page) -> tuple[bool, str]:
    need, msg = page_requires_facebook_login(page)
    if need:
        return False, msg or "Требуется вход в Facebook"
    return True, ""


# ---------------------------------------------------------------------------
#  JS: Раскрытие обрезанных постов (кнопка «Ещё» / «See more»)
# ---------------------------------------------------------------------------

_SEE_MORE_CLICK_JS = """() => {
  const labels = ['ещё', 'еще', 'see more', 'більше', 'mehr anzeigen', 'ver más', 'voir plus', 'vedi altro', 'meer weergeven', 'ver mais'];
  const candidates = [];
  const seenEls = new Set();
  document.querySelectorAll('[role="button"], span, a, div[role="link"]').forEach(el => {
    if (seenEls.has(el)) return;
    const t = (el.innerText || '').trim().toLowerCase();
    if (t.length < 2 || t.length > 40) return;
    for (const lb of labels) {
      if (t === lb || t === '... ' + lb || t === '…' + lb || t === '… ' + lb) {
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > window.innerHeight + 600) return;
        const ctx = ((el.parentElement && el.parentElement.innerText) || '').toLowerCase();
        if (ctx.includes('comment') || ctx.includes('коммент') || ctx.includes('ответ') || ctx.includes('repl')) return;
        seenEls.add(el);
        candidates.push({el, y: r.top + window.scrollY});
      }
    }
  });
  if (!candidates.length) return {ok: false, reason: 'no_see_more'};
  candidates.sort((a,b) => a.y - b.y);
  const pool = candidates.slice(0, 6);
  const pick = pool[Math.floor(Math.random() * pool.length)];
  try {
    pick.el.scrollIntoView({block:'center', behavior:'smooth'});
    pick.el.click();
    return {ok: true, y: Math.round(pick.y)};
  } catch(e) {
    return {ok: false, reason: 'click_err'};
  }
}"""

# ---------------------------------------------------------------------------
#  JS: Нажатие на «Показать N ответ(ов)» / «View N reply/replies»
# ---------------------------------------------------------------------------

_SHOW_REPLIES_CLICK_JS = """() => {
  const patterns = [
    /показать.*отв/i, /посмотреть.*отв/i, /ещ[её].*отв/i,
    /view.*repl/i, /show.*repl/i, /see.*repl/i,
    /\\d+\\s*отв/i, /\\d+\\s*repl/i,
    /другие\\s+ответы/i, /предыдущ.*комментар/i, /more\\s+comments/i,
    /смотреть.*другие\\s+ответы/i, /view\\s+more\\s+comments/i,
  ];
  const candidates = [];
  const seenEls = new Set();
  document.querySelectorAll('[role="button"], span, a, div[role="link"]').forEach(el => {
    if (seenEls.has(el)) return;
    const t = (el.innerText || '').trim();
    if (t.length < 2 || t.length > 80) return;
    const tl = t.toLowerCase();
    const matched = patterns.some(p => p.test(tl));
    if (!matched) return;
    if (tl === 'ещё' || tl === 'еще' || tl === 'see more') return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > window.innerHeight + 600) return;
    seenEls.add(el);
    candidates.push({el, y: r.top + window.scrollY, text: t.slice(0, 60)});
  });
  if (!candidates.length) return {ok: false, reason: 'no_show_replies'};
  candidates.sort((a,b) => a.y - b.y);
  const pool = candidates.slice(0, 6);
  const pick = pool[Math.floor(Math.random() * pool.length)];
  try {
    pick.el.scrollIntoView({block:'center', behavior:'smooth'});
    pick.el.click();
    return {ok: true, text: pick.text};
  } catch(e) {
    return {ok: false, reason: 'click_err'};
  }
}"""

# ---------------------------------------------------------------------------
#  JS: Reels — mute / play
# ---------------------------------------------------------------------------

_REELS_MUTE_JS = """() => {
  document.querySelectorAll('video').forEach(v => {
    v.muted = true;
    v.volume = 0;
  });
  return true;
}"""

_REELS_CLICK_PLAY_JS = """() => {
  const vids = document.querySelectorAll('video');
  for (const v of vids) {
    const r = v.getBoundingClientRect();
    if (r.top > -100 && r.bottom < window.innerHeight + 200 && r.width > 50) {
      v.muted = true; v.volume = 0;
      try { v.play(); } catch(e) {}
      return {ok: true, type: 'video_play'};
    }
  }
  const reelLinks = document.querySelectorAll('a[href*="/reel/"], a[href*="/watch"], [role="link"][href*="reel"]');
  for (const a of reelLinks) {
    const r = a.getBoundingClientRect();
    if (r.top > -50 && r.bottom < window.innerHeight + 300 && r.width > 30) {
      a.scrollIntoView({block: 'center', behavior: 'smooth'});
      a.click();
      return {ok: true, type: 'reel_link'};
    }
  }
  return {ok: false, reason: 'no_reel_target'};
}"""

# ---------------------------------------------------------------------------
#  JS: Извлечение текста раскрытого поста (для паузы чтения)
# ---------------------------------------------------------------------------

_POST_TEXT_LENGTH_JS = """() => {
  const main = document.querySelector('[role="main"]') || document.body;
  const articles = main.querySelectorAll('[role="article"]');
  let maxLen = 0;
  const vpH = window.innerHeight;
  for (const art of articles) {
    const r = art.getBoundingClientRect();
    if (r.bottom < -200 || r.top > vpH + 400) continue;
    const len = (art.innerText || '').trim().length;
    if (len > maxLen) maxLen = len;
  }
  return maxLen;
}"""

# ---------------------------------------------------------------------------
#  JS: Ссылки на группы из ленты групп
# ---------------------------------------------------------------------------

_GROUP_LINKS_JS = """() => {
  const links = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/groups/"]').forEach(a => {
    let h = (a.getAttribute('href')||'').trim();
    if (!h) return;
    if (h.includes('/groups/feed') || h.includes('/groups/discover') || h.includes('/groups/joins')) return;
    if (h.includes('/create') || h.includes('/settings')) return;
    const full = h.startsWith('http') ? h : ('https://www.facebook.com' + h);
    if (!/\\/groups\\/\\d+|groups\\/[a-zA-Z][a-zA-Z0-9._-]{2,}/.test(full)) return;
    const norm = full.split('?')[0].replace(/\\/+$/, '');
    if (seen.has(norm)) return;
    seen.add(norm);
    const r = a.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    links.push(norm);
  });
  return links.slice(0, 15);
}"""

# ---------------------------------------------------------------------------
#  JS: Ссылки на профили из видимой ленты
# ---------------------------------------------------------------------------

_PROFILE_LINKS_JS = """() => {
  const out = [];
  const seen = new Set();
  const main = document.querySelector('[role="main"]') || document.body;
  main.querySelectorAll('a[href]').forEach((a) => {
    let h = (a.getAttribute('href') || '').trim();
    if (!h || h === '#' || h.startsWith('#')) return;
    if (h.includes('/groups/') || h.includes('/messages/') || h.includes('/watch') || h.includes('/marketplace')) return;
    if (h.includes('logout') || h.includes('policies') || h.includes('/help')) return;
    if (!h.includes('facebook.com') && !h.startsWith('/')) return;
    const full = h.startsWith('http') ? h : ('https://www.facebook.com' + (h.startsWith('/') ? h : '/' + h));
    if (!/facebook\\.com\\/(profile\\.php|people\\/|[a-zA-Z0-9._-]{2,}\\/?)(\\?|$)/.test(full) && !full.includes('profile.php?id=')) return;
    if (seen.has(full)) return;
    seen.add(full);
    out.push(full);
  });
  return out.slice(0, 24);
}"""


# ---------------------------------------------------------------------------
#  Глубокое чтение поста: раскрытие «Ещё» + пауза чтения
# ---------------------------------------------------------------------------

def _deep_read_post(page: Page, *, deadline_mono: float) -> bool:
    """Раскроить обрезанный пост (See more) и «прочитать» его с паузой."""
    if not _deadline_ok(deadline_mono):
        return False
    try:
        res = page.evaluate(_SEE_MORE_CLICK_JS)
    except Exception:
        return False
    if not isinstance(res, dict) or not res.get("ok"):
        return False
    # Пауза «чтения» — пропорциональна длине видимого поста
    human_pause(page, 1.5, 3.5)
    try:
        text_len = page.evaluate(_POST_TEXT_LENGTH_JS)
    except Exception:
        text_len = 200
    text_len = int(text_len or 200)
    # Длина поста → пауза: 200 символов ~ 3 сек, 1000 ~ 8 сек, 2000+ ~ 12 сек
    read_sec = min(14.0, max(2.0, text_len / 150.0))
    # Добавим случайность
    read_sec *= random.uniform(0.7, 1.4)
    human_pause(page, read_sec * 0.3, read_sec * 0.5)
    # Иногда дочитываем — скроллим чуть вниз
    if random.random() < 0.55:
        soft_scroll(page, times=1)
        human_pause(page, 1.0, 3.0)
    return True


# ---------------------------------------------------------------------------
#  Раскрытие ответов в комментариях: «Показать N ответ(ов)»
# ---------------------------------------------------------------------------

def _click_show_replies(page: Page, *, deadline_mono: float) -> bool:
    """Нажимает на одну кнопку 'Показать N ответ' в видимом посте."""
    if not _deadline_ok(deadline_mono):
        return False
    try:
        res = page.evaluate(_SHOW_REPLIES_CLICK_JS)
    except Exception:
        return False
    if not isinstance(res, dict) or not res.get("ok"):
        return False
    logger.debug("show_replies clicked: %s", res.get("text", "")[:60])
    human_pause(page, 2.0, 5.0)
    # Скроллим чуть вниз, чтобы прочитать раскрытые ответы
    soft_scroll(page, times=random.randint(1, 2))
    human_pause(page, 2.5, 6.0)
    return True


# ---------------------------------------------------------------------------
#  Просмотр Reels (без звука)
# ---------------------------------------------------------------------------

def _watch_reels(page: Page, *, deadline_mono: float, emit: WarmupActionEmit | None = None) -> bool:
    """Navigate to Reels, watch 3-8 videos (muted), scroll between them."""
    if not _deadline_ok(deadline_mono):
        return False
    reels_urls = [
        "https://www.facebook.com/reel/",
        "https://www.facebook.com/watch/reels/",
    ]
    loaded = False
    for url in reels_urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=55_000)
            loaded = True
            break
        except Exception:
            continue
    if not loaded:
        return False
    human_pause(page, 3.0, 6.0)
    # Mute all videos immediately
    try:
        page.evaluate(_REELS_MUTE_JS)
    except Exception:
        pass
    reels_watched = 0
    target_count = random.randint(3, 8)
    for i in range(target_count):
        if not _deadline_ok(deadline_mono):
            break
        # Mute again (new videos appear on scroll)
        try:
            page.evaluate(_REELS_MUTE_JS)
        except Exception:
            pass
        # Try to click/play a reel
        try:
            res = page.evaluate(_REELS_CLICK_PLAY_JS)
            if isinstance(res, dict) and res.get("ok"):
                reels_watched += 1
                # Mute after click
                page.wait_for_timeout(800)
                try:
                    page.evaluate(_REELS_MUTE_JS)
                except Exception:
                    pass
        except Exception:
            pass
        # "Watch" the reel for 5-20 seconds
        watch_time = random.uniform(5.0, 20.0)
        human_pause(page, watch_time * 0.3, watch_time * 0.5)
        # Mute again before scrolling
        try:
            page.evaluate(_REELS_MUTE_JS)
        except Exception:
            pass
        # Scroll to next reel (vertical scroll or arrow key)
        scroll_method = random.choice(["scroll", "key"])
        if scroll_method == "key":
            try:
                page.keyboard.press("ArrowDown")
            except Exception:
                soft_scroll(page, times=1)
        else:
            try:
                page.mouse.wheel(0, random.randint(500, 800))
            except Exception:
                try:
                    page.evaluate(f"window.scrollBy(0, {random.randint(500, 800)})")
                except Exception:
                    pass
        human_pause(page, 1.5, 4.0)
    if emit and reels_watched > 0:
        emit(
            "natural_warmup_action_watch_reels",
            {"reels_watched": reels_watched, "target": target_count},
        )
    logger.info("Reels watched: %d / %d", reels_watched, target_count)
    return reels_watched > 0


# ---------------------------------------------------------------------------
#  Заход в отдельную группу из ленты
# ---------------------------------------------------------------------------

def _visit_random_group(page: Page, *, deadline_mono: float, emit: WarmupActionEmit | None = None) -> bool:
    """Переход в случайную группу из ленты, скролл, чтение, возврат."""
    if not _deadline_ok(deadline_mono):
        return False
    try:
        links = page.evaluate(_GROUP_LINKS_JS)
    except Exception:
        links = []
    if not isinstance(links, list) or not links:
        return False
    pick = random.choice(links[:8])
    try:
        page.goto(pick, wait_until="domcontentloaded", timeout=55_000)
    except Exception as e:
        logger.debug("_visit_random_group nav: %s", e)
        return False
    human_pause(page, 3.0, 7.0)
    # Скролл и чтение внутри группы
    scroll_count = random.randint(3, 8)
    read_count = 0
    for _ in range(scroll_count):
        if not _deadline_ok(deadline_mono):
            break
        soft_scroll(page, times=random.randint(1, 2))
        human_pause(page, 2.0, 5.0)
        # Иногда раскрываем пост
        if random.random() < 0.4:
            if _deep_read_post(page, deadline_mono=deadline_mono):
                read_count += 1
                human_pause(page, 2.0, 5.0)
    if emit:
        emit(
            "natural_warmup_action_group_visit",
            {"group_url": pick[:200], "scrolls": scroll_count, "posts_read": read_count},
        )
    return True


# ---------------------------------------------------------------------------
#  Заход на вкладки профиля (About / Friends)
# ---------------------------------------------------------------------------

def _visit_profile_tab(page: Page, *, deadline_mono: float, emit: WarmupActionEmit | None = None) -> bool:
    """Заходит на вкладку About или Friends текущего профиля."""
    if not _deadline_ok(deadline_mono):
        return False
    current_url = page.url or ""
    if "facebook.com" not in current_url:
        return False
    # Выбираем случайную вкладку
    tabs = ["about", "friends"]
    tab = random.choice(tabs)
    # Формируем URL вкладки
    base = current_url.split("?")[0].rstrip("/")
    tab_url = f"{base}/{tab}"
    try:
        page.goto(tab_url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        return False
    human_pause(page, 3.0, 7.0)
    # Скроллим
    for _ in range(random.randint(2, 5)):
        if not _deadline_ok(deadline_mono):
            break
        soft_scroll(page, times=1)
        human_pause(page, 1.5, 4.0)
    if emit:
        emit(
            "natural_warmup_action_profile_tab",
            {"tab": tab, "profile_url": current_url[:200]},
        )
    return True


# ---------------------------------------------------------------------------
#  Навигация по разным разделам Facebook
# ---------------------------------------------------------------------------

_FB_SECTIONS = [
    ("https://www.facebook.com/", "home"),
    ("https://www.facebook.com/groups/feed/", "groups_feed"),
    ("https://www.facebook.com/friends/", "friends"),
    ("https://www.facebook.com/watch/", "watch"),
    ("https://www.facebook.com/reel/", "reels"),
    ("https://www.facebook.com/bookmarks/", "bookmarks"),
    ("https://www.facebook.com/notifications/", "notifications"),
    ("https://www.facebook.com/marketplace/", "marketplace"),
]


def _browse_random_section(page: Page, *, deadline_mono: float, emit: WarmupActionEmit | None = None) -> bool:
    """Заходит на случайный раздел Facebook, скроллит, читает."""
    if not _deadline_ok(deadline_mono):
        return False
    url, section_name = random.choice(_FB_SECTIONS)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=55_000)
    except Exception as e:
        logger.debug("_browse_random_section nav %s: %s", section_name, e)
        return False
    human_pause(page, 3.0, 7.0)
    ok, _err = _ensure_logged_in(page)
    if not ok:
        return False
    scroll_count = random.randint(6, 14)
    read_count = 0
    for _ in range(scroll_count):
        if not _deadline_ok(deadline_mono):
            break
        soft_scroll(page, times=random.randint(1, 2))
        human_pause(page, 2.0, 6.0)
        # Раскрываем посты
        if random.random() < 0.45:
            if _deep_read_post(page, deadline_mono=deadline_mono):
                read_count += 1
        # Нажимаем «Показать ответы» под комментариями
        if random.random() < 0.25 and _deadline_ok(deadline_mono):
            _click_show_replies(page, deadline_mono=deadline_mono)
        # Иногда просто «читаем» без раскрытия
        if random.random() < 0.25:
            human_pause(page, 3.0, 8.0)
    if emit:
        emit(
            "natural_warmup_action_section_browse",
            {"section": section_name, "scrolls": scroll_count, "posts_read": read_count},
        )
    return True


# ============================================================================
#  Основные фазы прогрева
# ============================================================================

def run_natural_warmup_groups_session(
    page: Page,
    *,
    deadline_mono: float,
    scroll_rounds_cap: int = 12,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Лента групп: глубокий просмотр — прокрутка, раскрытие постов, заход в группы."""
    cap = max(8, min(50, int(scroll_rounds_cap)))
    try:
        page.goto(
            "https://www.facebook.com/groups/feed/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
    except Exception as e:
        return False, f"groups_nav:{e}"[:500], {}
    human_pause(page, 2.5, 5.5)
    ok, err = _ensure_logged_in(page)
    if not ok:
        return False, err, {}
    rounds = 0
    posts_read = 0
    groups_visited = 0
    while _deadline_ok(deadline_mono) and rounds < cap:
        soft_scroll(page, times=random.randint(2, 4))
        human_pause(page, 2.0, 6.0)
        # Раскрываем обрезанные посты
        if random.random() < 0.5 and _deadline_ok(deadline_mono):
            if _deep_read_post(page, deadline_mono=deadline_mono):
                posts_read += 1
                human_pause(page, 2.0, 5.0)
        # Нажимаем «Показать ответы» в комментариях
        if random.random() < 0.3 and _deadline_ok(deadline_mono):
            _click_show_replies(page, deadline_mono=deadline_mono)
        # Иногда заходим в отдельную группу
        if random.random() < 0.25 and groups_visited < 4 and _deadline_ok(deadline_mono):
            if _visit_random_group(page, deadline_mono=deadline_mono, emit=emit):
                groups_visited += 1
                # Возвращаемся в ленту групп
                try:
                    page.goto(
                        "https://www.facebook.com/groups/feed/",
                        wait_until="domcontentloaded",
                        timeout=55_000,
                    )
                    human_pause(page, 2.5, 5.0)
                except Exception:
                    break
        # Иногда длинная пауза «задумался / читает»
        if random.random() < 0.2:
            human_pause(page, 4.0, 10.0)
        if random.random() < 0.12:
            try:
                page.keyboard.press("Home")
                human_pause(page, 1.0, 3.0)
            except Exception:
                pass
        rounds += 1
    metrics = {
        "group_feed_scroll_rounds": rounds,
        "group_posts_read_deep": posts_read,
        "groups_visited": groups_visited,
    }
    if emit:
        emit(
            "natural_warmup_action_groups_scroll",
            {"scroll_rounds": rounds, "surface": "groups_feed", "posts_read": posts_read, "groups_visited": groups_visited},
        )
    return True, f"groups_ok rounds={rounds} read={posts_read} visited={groups_visited}", metrics


def run_natural_warmup_profiles_session(
    page: Page,
    *,
    deadline_mono: float,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Главная лента: глубокая прокрутка, раскрытие постов, заход на 2–4 профиля с вкладками."""
    feed_scroll_segments = 0
    posts_read = 0
    try:
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        return False, f"home_nav:{e}"[:500], {}
    human_pause(page, 2.5, 5.5)
    ok, err = _ensure_logged_in(page)
    if not ok:
        return False, err, {}
    max_visits = random.randint(3, 5)
    visits = 0
    while _deadline_ok(deadline_mono) and visits < max_visits:
        # Скроллим ленту глубже
        nseg = random.randint(5, 12)
        for _ in range(nseg):
            if not _deadline_ok(deadline_mono):
                break
            soft_scroll(page, times=random.randint(2, 4))
            human_pause(page, 1.5, 4.5)
            feed_scroll_segments += 1
            # Раскрываем посты
            if random.random() < 0.45:
                if _deep_read_post(page, deadline_mono=deadline_mono):
                    posts_read += 1
                    human_pause(page, 1.5, 4.0)
            # Показать ответы в комментариях
            if random.random() < 0.2 and _deadline_ok(deadline_mono):
                _click_show_replies(page, deadline_mono=deadline_mono)
        if emit and nseg:
            emit(
                "natural_warmup_action_home_feed_scroll",
                {"segments": nseg, "phase": "before_profile_pick", "posts_read": posts_read},
            )
        if not _deadline_ok(deadline_mono):
            break
        try:
            hrefs = page.evaluate(_PROFILE_LINKS_JS)
        except Exception:
            hrefs = []
        if isinstance(hrefs, list) and hrefs:
            pick = random.choice(hrefs[:10])
            try:
                page.goto(pick, wait_until="domcontentloaded", timeout=55_000)
                human_pause(page, 3.0, 7.0)
                # Глубокая прокрутка профиля
                pr_scrolls = 0
                pr_posts_read = 0
                for _ in range(random.randint(6, 14)):
                    if not _deadline_ok(deadline_mono):
                        break
                    soft_scroll(page, times=random.randint(1, 3))
                    human_pause(page, 1.5, 4.0)
                    pr_scrolls += 1
                    if random.random() < 0.45:
                        if _deep_read_post(page, deadline_mono=deadline_mono):
                            pr_posts_read += 1
                    if random.random() < 0.2:
                        _click_show_replies(page, deadline_mono=deadline_mono)
                    if pr_posts_read > 0:
                        posts_read += pr_posts_read
                        pr_posts_read = 0
                # Иногда заходим на вкладку профиля
                if random.random() < 0.4 and _deadline_ok(deadline_mono):
                    _visit_profile_tab(page, deadline_mono=deadline_mono, emit=emit)
                posts_read += pr_posts_read
                visits += 1
                if emit:
                    emit(
                        "natural_warmup_action_profile_visit",
                        {
                            "profile_url": pick[:200],
                            "scroll_segments_on_profile": pr_scrolls,
                            "posts_read_on_profile": pr_posts_read,
                            "visit_index": visits,
                        },
                    )
            except Exception as e:
                logger.info("natural_warmup profile visit skip: %s", e)
        # Возвращаемся на главную
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=55_000)
            human_pause(page, 2.0, 4.5)
        except Exception:
            break
    metrics = {
        "profile_visits": visits,
        "home_feed_scroll_segments": feed_scroll_segments,
        "profiles_posts_read_deep": posts_read,
    }
    return True, f"profiles_ok visits={visits} read={posts_read}", metrics


def run_natural_warmup_likes_session(
    page: Page,
    *,
    deadline_mono: float,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Лента: очень осторожно 0–2 лайка за сессию с паузами и чтением."""
    try:
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        return False, f"likes_home:{e}"[:500], {}
    human_pause(page, 2.5, 5.5)
    ok, err = _ensure_logged_in(page)
    if not ok:
        return False, err, {}
    # Сначала поскроллим и почитаем
    pre_scrolls = random.randint(5, 10)
    pre_posts_read = 0
    for _ in range(pre_scrolls):
        if not _deadline_ok(deadline_mono):
            break
        soft_scroll(page, times=random.randint(2, 4))
        human_pause(page, 2.0, 5.0)
        if random.random() < 0.45:
            if _deep_read_post(page, deadline_mono=deadline_mono):
                pre_posts_read += 1
        if random.random() < 0.2:
            _click_show_replies(page, deadline_mono=deadline_mono)
        if random.random() < 0.3:
            human_pause(page, 2.0, 4.0)
    want = random.randint(0, 2)
    got = 0
    parts: list[str] = []
    scroll_before_likes = 0
    for i in range(want):
        if not _deadline_ok(deadline_mono):
            break
        soft_scroll(page, times=random.randint(1, 3))
        scroll_before_likes += 1
        human_pause(page, 3.0, 9.0)
        lk_ok, lk_msg = try_like_first_visible_post(page, timeout_ms=22_000)
        parts.append(f"{i + 1}:{'ok' if lk_ok else 'skip'}:{lk_msg[:80]}")
        if lk_ok:
            got += 1
            if emit:
                emit(
                    "natural_warmup_action_timeline_like",
                    {"attempt": i + 1, "surface": "home_feed", "detail": lk_msg[:120]},
                )
        elif emit:
            emit(
                "natural_warmup_action_timeline_like_skip",
                {"attempt": i + 1, "reason": lk_msg[:200]},
            )
        human_pause(page, 5.0, 14.0)
    metrics = {
        "timeline_likes": got,
        "timeline_like_attempts": want,
        "like_session_scrolls": scroll_before_likes,
        "like_session_posts_read": pre_posts_read,
    }
    return True, f"likes_ok want={want} got={got} read={pre_posts_read} | " + ";".join(parts), metrics


_SUGGESTION_PROFILE_JS = """() => {
  const out = [];
  const seen = new Set();
  const root = document.querySelector('[role="main"]') || document.body;
  root.querySelectorAll('a[href]').forEach((a) => {
    let h = (a.getAttribute('href') || '').trim();
    if (!h || h.startsWith('#')) return;
    if (h.includes('/friends') && !h.includes('profile.php')) return;
    if (h.includes('/groups/') || h.includes('/messages/')) return;
    const full = h.startsWith('http') ? h : ('https://www.facebook.com' + (h.startsWith('/') ? h : '/' + h));
    if (!/facebook\\.com\\/(profile\\.php\\?id=\\d+|people\\/[^/]+|[^/]{2,80}\\/?)(\\?|$)/i.test(full)) return;
    if (full.includes('/friends/') || full.includes('sk=')) return;
    if (seen.has(full)) return;
    seen.add(full);
    out.push(full);
  });
  return out.slice(0, 30);
}"""


def run_natural_warmup_friend_session(
    page: Page,
    *,
    deadline_mono: float,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Рекомендации друзей → один клик «Добавить в друзья» с длинными паузами."""
    urls = [
        "https://www.facebook.com/friends/center/suggestions/",
        "https://www.facebook.com/friends/suggestions/",
    ]
    loaded = False
    for u in urls:
        if not _deadline_ok(deadline_mono):
            if emit:
                emit("natural_warmup_action_friend_request", {"result": "skip_deadline", "stage": "nav"})
            return True, "skip_friend_deadline", {}
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=65_000)
            loaded = True
            break
        except Exception:
            continue
    if not loaded:
        return False, "friend_nav_fail", {}
    human_pause(page, 8.0, 22.0)
    ok, err = _ensure_logged_in(page)
    if not ok:
        return False, err, {}
    for _ in range(random.randint(4, 9)):
        if not _deadline_ok(deadline_mono):
            break
        soft_scroll(page, times=1)
        human_pause(page, 2.5, 7.0)
    try:
        hrefs = page.evaluate(_SUGGESTION_PROFILE_JS)
    except Exception:
        hrefs = []
    if not isinstance(hrefs, list) or not hrefs:
        if emit:
            emit("natural_warmup_action_friend_request", {"result": "skip_no_candidate"})
        return True, "skip_no_suggestion_profile", {}
    pick = random.choice(hrefs[:15])
    try:
        page.goto(pick, wait_until="domcontentloaded", timeout=65_000)
    except Exception as e:
        if emit:
            emit(
                "natural_warmup_action_friend_request",
                {"result": "skip_nav", "detail": str(e)[:160]},
            )
        return True, f"skip_friend_profile_nav:{e!s}"[:120], {}
    human_pause(page, 25.0, 70.0)
    if not _deadline_ok(deadline_mono):
        if emit:
            emit("natural_warmup_action_friend_request", {"result": "skip_deadline", "stage": "profile"})
        return True, "skip_friend_deadline", {}
    fr_ok, fr_msg = try_friend_request_on_profile(page, timeout_ms=90_000)
    human_pause(page, 12.0, 28.0)
    if fr_ok:
        if emit:
            emit(
                "natural_warmup_action_friend_request",
                {"result": "sent", "detail": fr_msg[:200], "profile_url": pick[:200]},
            )
        return True, f"friend_ok:{fr_msg}", {"friend_requests_sent": 1}
    if emit:
        emit(
            "natural_warmup_action_friend_request",
            {"result": "skip_button", "detail": fr_msg[:200], "profile_url": pick[:200]},
        )
    return True, f"skip_friend_btn:{fr_msg}", {}


def run_natural_warmup_ai_comment_session(
    page: Page,
    db: Session,
    *,
    deadline_mono: float,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """ИИ-комментарий к 1-му или одному из первых 5 постов ленты; чувствительные темы пропускаются."""
    try:
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        return False, f"comment_home:{e}"[:500], {}
    human_pause(page, 10.0, 28.0)
    ok, err = _ensure_logged_in(page)
    if not ok:
        return False, err, {}
    pre_scrolls = random.randint(3, 6)
    pre_posts_read = 0
    for _ in range(pre_scrolls):
        if not _deadline_ok(deadline_mono):
            if emit:
                emit("natural_warmup_action_ai_comment", {"result": "skip_deadline", "stage": "feed_scroll"})
            return True, "skip_comment_deadline", {}
        soft_scroll(page, times=1)
        human_pause(page, 3.0, 9.0)
        # Раскрываем посты по пути
        if random.random() < 0.3:
            if _deep_read_post(page, deadline_mono=deadline_mono):
                pre_posts_read += 1
    if emit:
        emit(
            "natural_warmup_action_home_feed_scroll",
            {"segments": pre_scrolls, "phase": "before_ai_comment", "posts_read": pre_posts_read},
        )
    pick_idx = random.randint(0, 4)
    post_text = extract_timeline_post_text(
        page,
        mode="random",
        pool_size=5,
        pick_index=pick_idx,
        timeout_ms=35_000,
    )
    if not (post_text or "").strip() or len(post_text.strip()) < 14:
        if emit:
            emit("natural_warmup_action_ai_comment", {"result": "skip_no_post_text", "pick_index": pick_idx})
        return True, "skip_no_post_text", {}
    if not moderate_post_for_natural_warmup_sync(db, post_text):
        if emit:
            emit(
                "natural_warmup_action_ai_comment",
                {"result": "skip_unsafe_post", "pick_index": pick_idx, "snippet": post_text.strip()[:120]},
            )
        return True, "skip_unsafe_post", {}
    human_pause(page, 15.0, 45.0)
    if not _deadline_ok(deadline_mono):
        if emit:
            emit("natural_warmup_action_ai_comment", {"result": "skip_deadline", "stage": "after_moderation"})
        return True, "skip_comment_deadline", {}
    try:
        comment = generate_natural_warmup_comment_sync(db, post_text)
    except LLMError as e:
        return False, f"llm_comment:{e!s}"[:200], {}
    except Exception as e:
        logger.exception("natural_warmup AI comment")
        return False, f"llm_comment_err:{e!s}"[:200], {}
    human_pause(page, 20.0, 55.0)
    if not _deadline_ok(deadline_mono):
        if emit:
            emit("natural_warmup_action_ai_comment", {"result": "skip_deadline", "stage": "before_type"})
        return True, "skip_comment_deadline", {}
    c_ok, c_msg = try_comment_on_timeline(
        page,
        comment,
        mode="random",
        pool_size=5,
        pick_index=pick_idx,
        timeout_ms=55_000,
        typing_delay_ms=random.randint(95, 180),
    )
    human_pause(page, 12.0, 30.0)
    if c_ok:
        if emit:
            emit(
                "natural_warmup_action_ai_comment",
                {"result": "submitted", "pick_index": pick_idx, "detail": c_msg[:200]},
            )
        return True, f"submitted:{c_msg}"[:220], {"ai_comments_posted": 1}
    if emit:
        emit(
            "natural_warmup_action_ai_comment",
            {"result": "submit_failed", "pick_index": pick_idx, "detail": c_msg[:200]},
        )
    return False, f"comment_fail:{c_msg}"[:200], {}


# ============================================================================
#  НОВАЯ ФАЗА: browse — глубокая прогулка по Facebook
# ============================================================================

def run_natural_warmup_browse_session(
    page: Page,
    *,
    deadline_mono: float,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Глубокая прогулка по FB — имитация человека, минимум 15 минут.

    Сценарий выбирается случайно из нескольких:
    1) Главная лента → глубокий скролл + чтение → профили → вкладки
    2) Группы → заходы в группы → чтение → главная
    3) Друзья → скролл → профиль → чтение
    4) Несколько разных разделов подряд
    5) Просмотр Reels (без звука) + лента
    """
    scenarios = ["feed_deep", "groups_deep", "friends_explore", "multi_section", "reels_session"]
    scenario = random.choice(scenarios)

    sections_visited = 0
    posts_read = 0
    profiles_visited = 0
    groups_visited = 0

    ok, err = True, ""

    if scenario == "feed_deep":
        # Глубокое чтение ленты + заход на профили
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            return False, f"browse_feed_nav:{e}"[:500], {}
        human_pause(page, 3.0, 7.0)
        ok, err = _ensure_logged_in(page)
        if not ok:
            return False, err, {}
        sections_visited += 1
        # Очень глубокий скролл ленты
        for _ in range(random.randint(12, 25)):
            if not _deadline_ok(deadline_mono):
                break
            soft_scroll(page, times=random.randint(2, 4))
            human_pause(page, 2.0, 6.0)
            if random.random() < 0.5:
                if _deep_read_post(page, deadline_mono=deadline_mono):
                    posts_read += 1
                    human_pause(page, 2.0, 5.0)
            # Нажимаем «Показать ответы»
            if random.random() < 0.3 and _deadline_ok(deadline_mono):
                _click_show_replies(page, deadline_mono=deadline_mono)
            # Длинная «задумчивая» пауза
            if random.random() < 0.15:
                human_pause(page, 5.0, 12.0)
        # Заход на 2–4 профиля
        for _ in range(random.randint(2, 4)):
            if not _deadline_ok(deadline_mono):
                break
            try:
                hrefs = page.evaluate(_PROFILE_LINKS_JS)
            except Exception:
                hrefs = []
            if isinstance(hrefs, list) and hrefs:
                pick = random.choice(hrefs[:10])
                try:
                    page.goto(pick, wait_until="domcontentloaded", timeout=55_000)
                    human_pause(page, 3.0, 7.0)
                    profiles_visited += 1
                    for _ in range(random.randint(5, 12)):
                        if not _deadline_ok(deadline_mono):
                            break
                        soft_scroll(page, times=random.randint(1, 3))
                        human_pause(page, 1.5, 4.0)
                        if random.random() < 0.4:
                            if _deep_read_post(page, deadline_mono=deadline_mono):
                                posts_read += 1
                        if random.random() < 0.2:
                            _click_show_replies(page, deadline_mono=deadline_mono)
                    if random.random() < 0.5 and _deadline_ok(deadline_mono):
                        _visit_profile_tab(page, deadline_mono=deadline_mono, emit=emit)
                except Exception:
                    pass
            # Возвращаемся
            try:
                page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=55_000)
                human_pause(page, 2.0, 5.0)
            except Exception:
                break

    elif scenario == "groups_deep":
        # Лента групп → заход в 1–3 группы → чтение
        try:
            page.goto("https://www.facebook.com/groups/feed/", wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            return False, f"browse_groups_nav:{e}"[:500], {}
        human_pause(page, 3.0, 6.0)
        ok, err = _ensure_logged_in(page)
        if not ok:
            return False, err, {}
        sections_visited += 1
        # Глубокий скролл ленты групп
        for _ in range(random.randint(8, 18)):
            if not _deadline_ok(deadline_mono):
                break
            soft_scroll(page, times=random.randint(2, 4))
            human_pause(page, 2.0, 5.0)
            if random.random() < 0.45:
                if _deep_read_post(page, deadline_mono=deadline_mono):
                    posts_read += 1
            if random.random() < 0.25:
                _click_show_replies(page, deadline_mono=deadline_mono)
        # Заходим в группы
        for _ in range(random.randint(2, 4)):
            if not _deadline_ok(deadline_mono):
                break
            if _visit_random_group(page, deadline_mono=deadline_mono, emit=emit):
                groups_visited += 1
                try:
                    page.goto("https://www.facebook.com/groups/feed/", wait_until="domcontentloaded", timeout=55_000)
                    human_pause(page, 2.0, 4.0)
                except Exception:
                    break
        # Потом на главную — ещё скроллим
        if _deadline_ok(deadline_mono):
            try:
                page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=55_000)
                human_pause(page, 2.0, 5.0)
                sections_visited += 1
                for _ in range(random.randint(5, 12)):
                    if not _deadline_ok(deadline_mono):
                        break
                    soft_scroll(page, times=random.randint(1, 3))
                    human_pause(page, 2.0, 5.0)
                    if random.random() < 0.35:
                        if _deep_read_post(page, deadline_mono=deadline_mono):
                            posts_read += 1
            except Exception:
                pass

    elif scenario == "friends_explore":
        # Страница друзей → скролл → заход на профиль → чтение
        friends_urls = [
            "https://www.facebook.com/friends/",
            "https://www.facebook.com/friends/suggestions/",
        ]
        try:
            page.goto(random.choice(friends_urls), wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            return False, f"browse_friends_nav:{e}"[:500], {}
        human_pause(page, 3.0, 7.0)
        ok, err = _ensure_logged_in(page)
        if not ok:
            return False, err, {}
        sections_visited += 1
        for _ in range(random.randint(6, 15)):
            if not _deadline_ok(deadline_mono):
                break
            soft_scroll(page, times=random.randint(1, 3))
            human_pause(page, 2.0, 6.0)
        # Заход на профиль друга
        try:
            hrefs = page.evaluate(_SUGGESTION_PROFILE_JS)
        except Exception:
            hrefs = []
        if isinstance(hrefs, list) and hrefs and _deadline_ok(deadline_mono):
            pick = random.choice(hrefs[:10])
            try:
                page.goto(pick, wait_until="domcontentloaded", timeout=55_000)
                human_pause(page, 3.0, 8.0)
                profiles_visited += 1
                for _ in range(random.randint(6, 12)):
                    if not _deadline_ok(deadline_mono):
                        break
                    soft_scroll(page, times=random.randint(1, 3))
                    human_pause(page, 1.5, 4.0)
                    if random.random() < 0.4:
                        if _deep_read_post(page, deadline_mono=deadline_mono):
                            posts_read += 1
                    if random.random() < 0.2:
                        _click_show_replies(page, deadline_mono=deadline_mono)
                if random.random() < 0.5 and _deadline_ok(deadline_mono):
                    _visit_profile_tab(page, deadline_mono=deadline_mono, emit=emit)
            except Exception:
                pass
        # На главную — ещё скроллим
        if _deadline_ok(deadline_mono):
            try:
                page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=55_000)
                human_pause(page, 2.0, 5.0)
                sections_visited += 1
                for _ in range(random.randint(5, 10)):
                    if not _deadline_ok(deadline_mono):
                        break
                    soft_scroll(page, times=random.randint(1, 3))
                    human_pause(page, 2.0, 5.0)
                    if random.random() < 0.4:
                        if _deep_read_post(page, deadline_mono=deadline_mono):
                            posts_read += 1
            except Exception:
                pass

    elif scenario == "multi_section":
        # Обход нескольких разных разделов
        n_sections = random.randint(4, 6)
        section_order = random.sample(_FB_SECTIONS, min(n_sections, len(_FB_SECTIONS)))
        for url, section_name in section_order:
            if not _deadline_ok(deadline_mono):
                break
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=55_000)
            except Exception:
                continue
            human_pause(page, 3.0, 6.0)
            logged_ok, _ = _ensure_logged_in(page)
            if not logged_ok:
                continue
            sections_visited += 1
            # Если попали на Reels — смотрим их
            if section_name == "reels" and _deadline_ok(deadline_mono):
                try:
                    page.evaluate(_REELS_MUTE_JS)
                except Exception:
                    pass
                for _ in range(random.randint(3, 6)):
                    if not _deadline_ok(deadline_mono):
                        break
                    try:
                        page.evaluate(_REELS_MUTE_JS)
                        page.evaluate(_REELS_CLICK_PLAY_JS)
                        page.wait_for_timeout(800)
                        page.evaluate(_REELS_MUTE_JS)
                    except Exception:
                        pass
                    human_pause(page, 4.0, 12.0)
                    try:
                        page.keyboard.press("ArrowDown")
                    except Exception:
                        soft_scroll(page, times=1)
                    human_pause(page, 1.5, 3.0)
            else:
                for _ in range(random.randint(6, 14)):
                    if not _deadline_ok(deadline_mono):
                        break
                    soft_scroll(page, times=random.randint(1, 3))
                    human_pause(page, 2.0, 6.0)
                    if random.random() < 0.4:
                        if _deep_read_post(page, deadline_mono=deadline_mono):
                            posts_read += 1
                    if random.random() < 0.2:
                        _click_show_replies(page, deadline_mono=deadline_mono)
            # Длинная «задумчивая» пауза между разделами
            if random.random() < 0.3:
                human_pause(page, 5.0, 12.0)

    elif scenario == "reels_session":
        # Сначала Reels (без звука), потом лента
        if _watch_reels(page, deadline_mono=deadline_mono, emit=emit):
            sections_visited += 1
        # После Reels — скроллим ленту
        if _deadline_ok(deadline_mono):
            try:
                page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=55_000)
                human_pause(page, 3.0, 6.0)
                sections_visited += 1
                for _ in range(random.randint(8, 18)):
                    if not _deadline_ok(deadline_mono):
                        break
                    soft_scroll(page, times=random.randint(2, 4))
                    human_pause(page, 2.0, 6.0)
                    if random.random() < 0.45:
                        if _deep_read_post(page, deadline_mono=deadline_mono):
                            posts_read += 1
                    if random.random() < 0.25:
                        _click_show_replies(page, deadline_mono=deadline_mono)
                    if random.random() < 0.15:
                        human_pause(page, 5.0, 12.0)
            except Exception:
                pass

    metrics = {
        "browse_sections_visited": sections_visited,
        "browse_posts_read": posts_read,
        "browse_profiles_visited": profiles_visited,
        "browse_groups_visited": groups_visited,
    }
    if emit:
        emit(
            "natural_warmup_action_browse_session",
            {
                "scenario": scenario,
                "sections_visited": sections_visited,
                "posts_read": posts_read,
                "profiles_visited": profiles_visited,
                "groups_visited": groups_visited,
            },
        )
    return (
        True,
        f"browse_ok scenario={scenario} sections={sections_visited} read={posts_read} profiles={profiles_visited} groups={groups_visited}",
        metrics,
    )


# ============================================================================
#  Главный диспетчер фаз
# ============================================================================

def run_natural_warmup_session(
    page: Page,
    db: Session | None,
    phase: NaturalWarmupPhase,
    *,
    session_seconds_min: int | None = None,
    session_seconds_max: int | None = None,
    groups_scroll_rounds_cap: int = 12,
    emit: WarmupActionEmit | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Одна сессия: длительность зависит от фазы (прогрев с максимальными паузами)."""
    if phase == "ai_comment":
        lo, hi = (session_seconds_min or 1400), (session_seconds_max or 3200)
    elif phase == "add_friend":
        lo, hi = (session_seconds_min or 900), (session_seconds_max or 1800)
    elif phase == "browse":
        # Фаза browse: 15–30 минут (900–1800 секунд)
        lo, hi = (session_seconds_min or 900), (session_seconds_max or 1800)
    else:
        # groups / profiles / likes: минимум 15 минут
        lo, hi = (session_seconds_min or 900), (session_seconds_max or 1800)
    sec = random.randint(lo, hi)
    deadline = time.monotonic() + float(sec)
    if phase == "groups":
        return run_natural_warmup_groups_session(
            page,
            deadline_mono=deadline,
            scroll_rounds_cap=groups_scroll_rounds_cap,
            emit=emit,
        )
    if phase == "profiles":
        return run_natural_warmup_profiles_session(page, deadline_mono=deadline, emit=emit)
    if phase == "likes":
        return run_natural_warmup_likes_session(page, deadline_mono=deadline, emit=emit)
    if phase == "add_friend":
        return run_natural_warmup_friend_session(page, deadline_mono=deadline, emit=emit)
    if phase == "browse":
        return run_natural_warmup_browse_session(page, deadline_mono=deadline, emit=emit)
    if phase == "ai_comment":
        if db is None:
            return False, "no_db_for_ai_comment", {}
        return run_natural_warmup_ai_comment_session(page, db, deadline_mono=deadline, emit=emit)
    return False, "unknown_phase", {}
