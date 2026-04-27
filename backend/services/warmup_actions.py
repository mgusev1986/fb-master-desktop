"""Playwright: лайк и опционально комментарий на странице профиля Facebook (sync)."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from playwright.sync_api import Page

from backend.services.playwright_humanize import (
    humanize_after_profile_open,
    humanize_before_comment,
    humanize_before_like,
    humanize_between_like_and_comment,
    micro_reading_pause,
    soft_scroll_feed,
)
from backend.services.throttle import is_valid_throttle_preset, resolve_delay

logger = logging.getLogger(__name__)


def _throttle_sleep(action: str, preset: str, lo_f: float, hi_f: float) -> None:
    pr = preset if is_valid_throttle_preset(preset) else "medium"
    lo, hi = resolve_delay(action, preset=pr)
    time.sleep(random.uniform(lo * lo_f, hi * hi_f))


def _throttle_preset_or_none(raw: str | None) -> str | None:
    s = (raw or "").strip().lower()
    return s if is_valid_throttle_preset(s) else None


def _normalize_profile_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if not u.startswith("http"):
        return "https://www.facebook.com/" + u.lstrip("/")
    return u


_LIKE_CLICK_JS = """({ mode, poolSize, attemptIndex, skipFirst }) => {
  function inMainColumn(el) {
    const main = document.querySelector('[role="main"]');
    if (!main) return true;
    return main.contains(el);
  }
  function isCommentOrReplyLike(el) {
    let p = el;
    for (let i = 0; i < 28 && p; i++) {
      if (p.getAttribute && p.getAttribute('data-comment-id')) return true;
      if (p.getAttribute && p.getAttribute('data-ft') && String(p.getAttribute('data-ft')).includes('comment')) return true;
      const al = ((p.getAttribute && p.getAttribute('aria-label')) || '').toLowerCase();
      if (al.includes('comment by') || al.includes('комментарий от') || al.includes('ответ на')) return true;
      const tid = (p.getAttribute && p.getAttribute('id')) || '';
      if (/^comment|^reply/i.test(tid)) return true;
      p = p.parentElement;
    }
    return false;
  }
  /* Клик по счётчику «N лайков» / списку реакций открывает модалку — не это. */
  function isReactionSummaryOrListTrigger(el) {
    if (el.closest && el.closest('[role="dialog"]')) return true;
    const al = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.innerText || '')).toLowerCase();
    if (
      al.includes('see who reacted') ||
      al.includes('people who liked') ||
      al.includes('view all reactions') ||
      al.includes('list of people') ||
      al.includes('кто поставил') ||
      al.includes('кому понравилось') ||
      al.includes('все реакции') ||
      (al.includes('просмотр') && (al.includes('лайк') || al.includes('reaction'))) ||
      al.includes('reaction list') ||
      al.includes('уточните реакц') ||
      al.includes('список людей') ||
      (al.includes('реакци') && /\\d/.test(al)) ||
      (al.includes('понравилось') && /\\d/.test(al) && !al.includes('нравится'))
    ) {
      return true;
    }
    /* «Имя, Имя и ещё N» — открывает список реагировавших, не лайк. */
    if (/и ещё\\s*\\d+|и еще\\s*\\d+|and\\s+\\d+\\s+others?/i.test(al)) return true;
    if (/\\d+\\s*(like|лайк|reactions?|реакц)/i.test(al)) return true;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'a') {
      const h = (el.getAttribute('href') || '').toLowerCase();
      if (h.includes('reaction') || h.includes('reactor') || (h.includes('ufi') && h.includes('reaction'))) return true;
    }
    return false;
  }
  /* Панель поста: рядом с «Комментировать» / «Поделиться» — приоритетнее верхушки страницы. */
  function toolbarScore(el) {
    let n = el;
    for (let d = 0; d < 10 && n; d++) {
      const chunk = ((n.innerText || '') + ' ' + (n.getAttribute('aria-label') || '')).toLowerCase();
      const hasComment = chunk.includes('комментир') || chunk.includes('comment');
      const hasShare = chunk.includes('подели') || chunk.includes('share');
      const hasLikeWord = chunk.includes('нрав') || chunk.includes('like');
      if (hasComment && (hasShare || hasLikeWord)) return 2;
      if (hasComment) return 1;
      n = n.parentElement;
    }
    return 0;
  }
  /* Пост ленты, а не вложенный article (комментарий / вложенный блок). */
  function feedPostScore(el) {
    const art = el.closest('[role="article"]');
    if (!art) return 0;
    let p = art.parentElement;
    while (p) {
      if (p.getAttribute && p.getAttribute('role') === 'article') return 0;
      p = p.parentElement;
    }
    return 4;
  }
  /* aria-label часто на обёртке; реальный клик — на [role="button"] внутри/снаружи. */
  function resolveClickTarget(hit, exactSet) {
    function labelOk(node) {
      if (!node) return false;
      const t = (node.getAttribute('aria-label') || '').trim().toLowerCase();
      return exactSet.has(t);
    }
    if (!hit) return null;
    if (hit.getAttribute('role') === 'button' && labelOk(hit)) return hit;
    if (labelOk(hit)) {
      const inner = hit.querySelector('[role="button"]');
      if (inner && labelOk(inner)) return inner;
      const outer = hit.closest('[role="button"]');
      if (outer && labelOk(outer)) return outer;
    }
    const inner2 = hit.querySelector('[role="button"]');
    if (inner2 && labelOk(inner2)) return inner2;
    const outer2 = hit.closest('[role="button"]');
    if (outer2 && labelOk(outer2)) return outer2;
    return hit;
  }
  function visibleUnpressedLikes() {
    const seen = new Set();
    const arr = [];
    const likeLabels = [
      'Like', 'Нравится', 'Подобається', 'Gefällt mir', "J'aime", 'Mi piace', '赞', 'Me gusta', 'Curtir', 'Vind ik leuk'
    ];
    const exactSet = new Set(likeLabels.map((s) => s.toLowerCase()));
    function pushCandidate(rawEl) {
      const el = resolveClickTarget(rawEl, exactSet);
      if (!el || seen.has(el)) return;
      /* Только настоящая кнопка «Нравится» / Like: иначе в список попадали любые [role=button]. */
      const alNorm = ((el.getAttribute('aria-label') || '').trim().toLowerCase());
      if (!exactSet.has(alNorm)) return;
      if (el.getAttribute('aria-pressed') === 'true') return;
      if (!inMainColumn(el)) return;
      if (isCommentOrReplyLike(el)) return;
      if (isReactionSummaryOrListTrigger(el)) return;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      /* Не только видимый экран: посты ниже сгиба тоже в пул (scrollIntoView перед кликом). */
      if (r.bottom < -500 || r.top > window.innerHeight + 8000) return;
      seen.add(el);
      arr.push({
        el: el,
        y: r.top + window.scrollY,
        tb: toolbarScore(el),
        fp: feedPostScore(el)
      });
    }
    for (const lbl of likeLabels) {
      document.querySelectorAll('[aria-label="' + lbl + '"]').forEach((hit) => pushCandidate(hit));
    }
    document.querySelectorAll('[role="button"]').forEach((n) => pushCandidate(n));
    arr.sort((a, b) => {
      if (b.fp !== a.fp) return b.fp - a.fp;
      if (b.tb !== a.tb) return b.tb - a.tb;
      return a.y - b.y;
    });
    const byArticle = new Map();
    for (const row of arr) {
      const art = row.el.closest('[role="article"]');
      const key = art || row.el;
      if (!byArticle.has(key)) byArticle.set(key, row);
    }
    const deduped = Array.from(byArticle.values());
    deduped.sort((a, b) => {
      if (b.fp !== a.fp) return b.fp - a.fp;
      if (b.tb !== a.tb) return b.tb - a.tb;
      return a.y - b.y;
    });
    return deduped.map((x) => x.el);
  }
  const m = (mode || 'first').toLowerCase();
  const skip = Math.max(0, (skipFirst | 0));
  let ordered = visibleUnpressedLikes();
  const totalBeforeSkip = ordered.length;
  ordered = ordered.slice(skip);
  if (ordered.length === 0) {
    return {
      ok: false,
      reason: skip > 0 ? 'none_after_skip' : 'none',
      diag: { mode: m, skip, totalBeforeSkip, afterSkip: 0, hint: 'no_unpressed_like_in_feed' }
    };
  }
  let target = null;
  let pool = ordered;
  let cap = ordered.length;
  if (m === 'random') {
    cap = Math.max(1, Math.min(poolSize | 0, ordered.length));
    pool = ordered.slice(0, cap);
    if (pool.length === 0) {
      return {
        ok: false,
        reason: 'empty_pool',
        diag: { mode: m, skip, totalBeforeSkip, afterSkip: ordered.length, poolCap: cap }
      };
    }
    target = pool[Math.floor(Math.random() * pool.length)];
  } else {
    const idx = attemptIndex | 0;
    if (idx >= ordered.length) {
      return {
        ok: false,
        reason: 'no_more',
        diag: { mode: m, skip, totalBeforeSkip, afterSkip: ordered.length, needIndex: idx }
      };
    }
    target = ordered[idx];
    pool = ordered;
    cap = ordered.length;
  }
  const pickIdx = pool.indexOf(target);
  const yApprox = Math.round(target.getBoundingClientRect().top + window.scrollY);
  const diag = {
    mode: m,
    skip,
    totalBeforeSkip,
    afterSkip: ordered.length,
    poolCap: m === 'random' ? (poolSize | 0) : cap,
    poolLen: pool.length,
    pickIdx,
    yApprox,
    vpH: Math.round(window.innerHeight),
    scrollY: Math.round(window.scrollY)
  };
  try {
    target.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
    target.click();
    return { ok: true, diag };
  } catch (e) {
    return { ok: false, reason: 'click_err', diag };
  }
}"""

_FB_LIKE_LOCATOR_NAMES = (
    "Нравится",
    "Подобається",
    "Like",
    "Me gusta",
    "J'aime",
    "Gefällt mir",
    "Mi piace",
    "Curtir",
    "Vind ik leuk",
)


def _scroll_feed_for_more_posts(page: Page) -> None:
    """Прокрутка основной колонки профиля, чтобы подгрузились посты ниже сгиба."""
    try:
        page.locator('[role="main"]').first.evaluate(
            """(el) => { el.scrollBy(0, 380 + Math.floor(Math.random() * 920)); }"""
        )
    except Exception:
        page.mouse.wheel(0, random.randint(500, 1500))
    page.wait_for_timeout(random.randint(520, 1100))


def _diag_to_log_fragment(diag: Any) -> str:
    if not isinstance(diag, dict):
        return ""
    parts: list[str] = []
    if diag.get("totalBeforeSkip") is not None:
        parts.append(f"preSkip={diag['totalBeforeSkip']}")
    if diag.get("afterSkip") is not None:
        parts.append(f"posts={diag['afterSkip']}")
    if diag.get("poolLen") is not None:
        parts.append(f"pool={diag['poolLen']}")
    if diag.get("pickIdx") is not None:
        parts.append(f"pick={diag['pickIdx']}")
    if diag.get("yApprox") is not None:
        parts.append(f"y≈{diag['yApprox']}")
    if diag.get("scrollY") is not None and diag.get("vpH") is not None:
        parts.append(f"scr={diag['scrollY']}px/vp{diag['vpH']}")
    if diag.get("needIndex") is not None:
        parts.append(f"needIdx={diag['needIndex']}")
    if diag.get("hint"):
        parts.append(str(diag["hint"]))
    return ";".join(parts)


def _playwright_click_unpressed_like_n(page: Page, *, skip_unpressed: int, timeout_ms: int) -> bool:
    """Клик по skip_unpressed-й свободной (aria-pressed≠true) кнопке лайка в main."""
    try:
        main = page.locator('[role="main"]').first
        for name in _FB_LIKE_LOCATOR_NAMES:
            locs = main.get_by_role("button", name=name, exact=True)
            n = locs.count()
            seen_free = 0
            for i in range(n):
                btn = locs.nth(i)
                try:
                    if (btn.get_attribute("aria-pressed") or "").lower() == "true":
                        continue
                    if seen_free < skip_unpressed:
                        seen_free += 1
                        continue
                    btn.scroll_into_view_if_needed(timeout=5_000)
                    btn.click(timeout=min(8_000, int(timeout_ms)), force=True)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _playwright_click_first_feed_like(page: Page, *, timeout_ms: int = 12_000) -> bool:
    """Запасной клик: первая свободная кнопка лайка в ленте."""
    return _playwright_click_unpressed_like_n(page, skip_unpressed=0, timeout_ms=timeout_ms)


def try_like_posts_on_profile(
    page: Page,
    *,
    mode: str = "first",
    pool_size: int = 10,
    count: int = 1,
    skip_first: int = 0,
    timeout_ms: int = 25_000,
    throttle_preset: str | None = None,
) -> tuple[bool, str]:
    """Лайки на профиле: сверху вниз (first) или случайный пост в пуле из верхних N (random).

    Между лайками прокручивает ленту, чтобы кнопки ниже сгиба попадали в кандидаты (scrollIntoView + подгрузка).

    skip_first — сколько верхних кнопок «Лайк» пропустить (например 1 = пропустить лайк поста, целиться в комментарии).
    """
    m = (mode or "first").strip().lower()
    if m not in ("first", "random"):
        m = "first"
    ps = max(1, min(int(pool_size or 10), 20))
    n_want = max(1, min(int(count or 1), 10))
    skip = max(0, min(int(skip_first or 0), 30))
    pr_eff = _throttle_preset_or_none(throttle_preset)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(20_000, int(timeout_ms)))
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(8_000, int(timeout_ms)))
    except Exception:
        pass

    if pr_eff:
        _throttle_sleep("navigate_profile", pr_eff, 0.1, 0.24)
        page.wait_for_timeout(random.randint(380, 920))
        try:
            page.keyboard.press("Escape")
            _throttle_sleep("scroll_page", pr_eff, 0.07, 0.16)
            page.keyboard.press("Escape")
            _throttle_sleep("scroll_page", pr_eff, 0.06, 0.14)
        except Exception:
            pass
        soft_scroll_feed(page, pr_eff, strength=random.uniform(0.5, 0.95))
        if random.random() < 0.48:
            micro_reading_pause(page, pr_eff)
        if m == "random" and n_want > 1:
            _scroll_feed_for_more_posts(page)
            _throttle_sleep("scroll_page", pr_eff, 0.22, 0.42)
            _scroll_feed_for_more_posts(page)
    else:
        page.wait_for_timeout(1200)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(180)
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
        page.mouse.wheel(0, 520)
        page.wait_for_timeout(700)
        page.mouse.wheel(0, 420)
        page.wait_for_timeout(500)
        if m == "random" and n_want > 1:
            _scroll_feed_for_more_posts(page)
            _scroll_feed_for_more_posts(page)

    clicked = 0
    last_reason = ""
    attempt_logs: list[str] = []
    fb_timeout = min(8_000, int(timeout_ms))

    for attempt in range(n_want):
        if attempt > 0:
            _scroll_feed_for_more_posts(page)
            if m == "random":
                _scroll_feed_for_more_posts(page)
            if pr_eff:
                _throttle_sleep("scroll_page", pr_eff, 0.3, 0.58)
            else:
                page.wait_for_timeout(random.randint(350, 700))

        res = page.evaluate(
            _LIKE_CLICK_JS,
            {"mode": m, "poolSize": ps, "attemptIndex": attempt, "skipFirst": skip},
        )
        diag = (res or {}).get("diag") if isinstance(res, dict) else None
        dfrag = _diag_to_log_fragment(diag)

        if not isinstance(res, dict) or not res.get("ok"):
            last_reason = str((res or {}).get("reason", "fail"))
            attempt_logs.append(
                f"a{attempt + 1}:js_fail:{last_reason}" + (f"({dfrag})" if dfrag else "")
            )
            fb_skip = skip + attempt if m == "first" else 0
            if _playwright_click_unpressed_like_n(page, skip_unpressed=fb_skip, timeout_ms=fb_timeout):
                attempt_logs.append(f"a{attempt + 1}:fb_fallback_ok")
                clicked += 1
                if pr_eff:
                    _throttle_sleep("like_post", pr_eff, 0.12, 0.26)
                    soft_scroll_feed(page, pr_eff, strength=random.uniform(0.35, 0.7))
                    _throttle_sleep("like_post", pr_eff, 0.1, 0.22)
                else:
                    page.wait_for_timeout(random.randint(400, 850))
                    page.mouse.wheel(0, random.randint(260, 520))
                    page.wait_for_timeout(random.randint(320, 650))
                continue
            attempt_logs.append(f"a{attempt + 1}:fb_fallback_fail")
            break

        attempt_logs.append(f"a{attempt + 1}:ok" + (f"({dfrag})" if dfrag else ""))
        clicked += 1
        if pr_eff:
            _throttle_sleep("like_post", pr_eff, 0.14, 0.28)
            soft_scroll_feed(page, pr_eff, strength=random.uniform(0.38, 0.72))
            _throttle_sleep("like_post", pr_eff, 0.11, 0.24)
        else:
            page.wait_for_timeout(random.randint(400, 850))
            page.mouse.wheel(0, random.randint(260, 520))
            page.wait_for_timeout(random.randint(320, 650))

    if pr_eff:
        _throttle_sleep("scroll_page", pr_eff, 0.07, 0.16)
    else:
        page.wait_for_timeout(400)
    tail = f"skip{skip}" if skip else ""
    msg = f"likes={clicked}/{n_want}:{m}:pool{ps}"
    if tail:
        msg += f":{tail}"
    if attempt_logs:
        msg += "|" + "|".join(attempt_logs)
    if clicked < n_want:
        msg += f"|INCOMPLETE last_err={last_reason or '—'}"

    if clicked == 0:
        return False, msg or (last_reason or "like_not_found")
    return True, msg


def try_like_first_visible_post(
    page: Page,
    *,
    timeout_ms: int = 25_000,
    throttle_preset: str | None = None,
) -> tuple[bool, str]:
    """Один лайк по первому видимому посту (совместимость с прогревом)."""
    return try_like_posts_on_profile(
        page,
        mode="first",
        pool_size=10,
        count=1,
        skip_first=0,
        timeout_ms=timeout_ms,
        throttle_preset=throttle_preset,
    )


_COMMENT_EXPAND_JS = """() => {
  const labels = [
    'write a comment', 'комментировать', 'оставить комментарий', 'ответить',
    'add a comment', 'напишите комментарий', 'напиши комментарий',
    'коментувати', 'написати коментар', 'залишити коментар', 'додати коментар',
    'view comment', 'просмотреть комментарий'
  ];
  const nodes = document.querySelectorAll('[role="button"], [role="link"], span, div');
  for (const el of nodes) {
    const t = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
    if (t.length < 8 || t.length > 140) continue;
    for (const lb of labels) {
      if (t.includes(lb)) {
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2 || r.bottom < 0) continue;
        try {
          el.click();
          return true;
        } catch (e) {}
      }
    }
  }
  return false;
}"""

_COMMENT_TARGET_PICK_JS = """({ mode, poolSize, pickIndex }) => {
  document.querySelectorAll('[data-codex-comment-target="1"]').forEach((el) => {
    try { el.removeAttribute('data-codex-comment-target'); } catch (e) {}
  });
  function isTopLevelArticle(art) {
    let p = art.parentElement;
    while (p) {
      if (p.getAttribute && p.getAttribute('role') === 'article') return false;
      p = p.parentElement;
    }
    return true;
  }
  function visibleEnough(el) {
    const r = el.getBoundingClientRect();
    return !(r.width < 2 || r.height < 2 || r.bottom < -400 || r.top > window.innerHeight + 8000);
  }
  function articleRows() {
    const main = document.querySelector('[role="main"]');
    const root = main || document;
    const articles = Array.from(root.querySelectorAll('[role="article"]'));
    const rows = [];
    for (const art of articles) {
      if (!isTopLevelArticle(art) || !visibleEnough(art)) continue;
      let t = (art.innerText || '').trim();
      t = t.replace(/\\s*\\n\\s*/g, '\\n').replace(/\\n{3,}/g, '\\n\\n');
      if (t.length < 8) continue;
      const head = t.slice(0, 160).toLowerCase();
      if (
        head.includes('people you may know') ||
        head.includes('people you know') ||
        head.includes('возможные друзья') ||
        head.includes('рекомендуемые друзья') ||
        head.includes('знакомые вам') ||
        head.includes('suggested for you')
      ) {
        continue;
      }
      rows.push({ art, y: art.getBoundingClientRect().top + window.scrollY, len: t.length });
    }
    rows.sort((a, b) => a.y - b.y);
    return rows;
  }
  function pickRow(rows) {
    if (!rows.length) return null;
    const m = (mode || 'first').toLowerCase();
    if (m === 'random') {
      const cap = Math.max(1, Math.min(poolSize | 0, rows.length));
      const pool = rows.slice(0, cap);
      const idx = Math.max(0, Math.min(pickIndex | 0, pool.length - 1));
      const pick = pool[idx];
      return {
        row: pick,
        diag: {
          totalBeforeSkip: rows.length,
          afterSkip: rows.length,
          poolLen: pool.length,
          pickIdx: idx,
          yApprox: pick ? Math.round(pick.y) : null,
          scrollY: Math.round(window.scrollY),
          vpH: Math.round(window.innerHeight)
        }
      };
    }
    return {
      row: rows[0],
      diag: {
        totalBeforeSkip: rows.length,
        afterSkip: rows.length,
        poolLen: Math.min(rows.length, 1),
        pickIdx: 0,
        yApprox: rows[0] ? Math.round(rows[0].y) : null,
        scrollY: Math.round(window.scrollY),
        vpH: Math.round(window.innerHeight)
      }
    };
  }
  function pickCommentTrigger(art) {
    const labels = [
      'write a comment', 'комментировать', 'оставить комментарий', 'ответить',
      'add a comment', 'напишите комментарий', 'напиши комментарий',
      'коментувати', 'написати коментар', 'залишити коментар', 'додати коментар',
      'comment', 'коммент', 'комент', 'reply'
    ];
    const nodes = art.querySelectorAll('[role="button"], [role="link"], button, a, span, div');
    let best = null;
    let bestScore = -1;
    for (const el of nodes) {
      if (!visibleEnough(el)) continue;
      const txt = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
      if (txt.length < 4 || txt.length > 180) continue;
      let score = -1;
      for (const lb of labels) {
        if (!txt.includes(lb)) continue;
        score = Math.max(score, lb.length);
      }
      if (score < 0) continue;
      if (el.getAttribute('role') === 'button' || (el.tagName || '').toLowerCase() === 'button') score += 10;
      if (txt.includes('write a comment') || txt.includes('add a comment') || txt.includes('напишите комментарий')) score += 14;
      if (txt.includes('comment') || txt.includes('коммент') || txt.includes('комент')) score += 4;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  }
  const rows = articleRows();
  const picked = pickRow(rows);
  if (!picked || !picked.row || !picked.row.art) {
    return { ok: false, reason: 'no_posts' };
  }
  const art = picked.row.art;
  art.setAttribute('data-codex-comment-target', '1');
  try {
    art.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
  } catch (e) {}
  const trigger = pickCommentTrigger(art);
  let opened = false;
  if (trigger) {
    try {
      trigger.click();
      opened = true;
    } catch (e) {}
  }
  return { ok: true, opened, diag: picked.diag };
}"""

_COMMENT_COMPOSER_PICK_JS = """() => {
  const targetArticle = document.querySelector('[data-codex-comment-target="1"]');
  const selectors = [
    '[contenteditable="true"][data-lexical-editor="true"]',
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[placeholder]'
  ];
  const seen = new Set();
  const scored = [];
  function pushCandidate(el) {
    if (!el || seen.has(el)) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 48 || rect.height < 10) return false;
    if (rect.bottom < 48 || rect.top > window.innerHeight + 160) return false;
    seen.add(el);
    const ph = (
      (el.getAttribute('aria-placeholder') || '') + ' ' +
      (el.getAttribute('data-placeholder') || '') + ' ' +
      (el.getAttribute('placeholder') || '')
    ).toLowerCase();
    const al = ((el.getAttribute('aria-label') || '') + '').toLowerCase();
    const hint = ph + ' ' + al;
    const inArticle = !!el.closest('[role="article"]');
    const inTarget = !!(targetArticle && targetArticle.contains(el));
    const inDialog = !!el.closest('[role="dialog"]');
    const looksComment =
      hint.includes('comment') || hint.includes('коммент') || hint.includes('комент') ||
      hint.includes('write') || hint.includes('напиш') || hint.includes('ответ');
    let score = 0;
    if (inTarget) score += 40;
    if (inDialog) score += 24;
    if (looksComment) score += 16;
    if (inArticle && rect.width > 72) score += 10;
    if (el.getAttribute('data-lexical-editor') === 'true') score += 12;
    if (el.getAttribute('role') === 'textbox') score += 6;
    if ((el.tagName || '').toLowerCase() === 'textarea') score += 8;
    if (targetArticle && !inTarget && !inDialog) score -= 20;
    scored.push({ el, score });
    return false;
  }
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      pushCandidate(el);
    }
  }
  const fallback = document.querySelectorAll('[contenteditable="true"], textarea, [role="textbox"]');
  for (const el of fallback) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 64 || rect.height < 12) continue;
    if (rect.top < 96 || rect.top > window.innerHeight + 120) continue;
    const dm = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('aria-placeholder') || '')).toLowerCase();
    if (dm.includes('search')) continue;
    if (el.closest('[role="dialog"]')) {
      if (!(dm.includes('коммент') || dm.includes('комент') || dm.includes('comment'))) continue;
    }
    pushCandidate(el);
  }
  scored.sort((a, b) => b.score - a.score);
  const best = scored[0];
  if (!best || best.score < 1) return false;
  try {
    best.el.scrollIntoView({ block: 'center', behavior: 'instant' });
    best.el.focus();
    best.el.click();
    return true;
  } catch (e) {
    return false;
  }
}"""

_COMMENT_SUBMIT_JS = """() => {
  const exact = new Set([
    'опубликовать комментарий', 'опубликовать',
    'надіслати коментар', 'опублікувати коментар',
    'відправити коментар', 'відправити',
    'отправить', 'отправить комментарий', 'send'
  ]);
  function inPostUi(el) {
    return !!(el.closest('[role="article"]') || el.closest('[role="dialog"]'));
  }
  function looksLikeCommentSend(label) {
    const t = label.toLowerCase().replace(/\\s+/g, ' ').trim();
    if (t.length < 2 || t.length > 72) return false;
    if (exact.has(t)) return true;
    if (t.includes('отправ') || t.includes('надісл') || t.includes('відправ')) return true;
    if ((t === 'send' || t.startsWith('send ')) && !t.includes('message')) return true;
    if (t.includes('post') && (t.includes('comment') || t.includes('коммент') || t.includes('комент'))) return true;
    return false;
  }
  const nodes = document.querySelectorAll('[role="button"]');
  for (const el of nodes) {
    if (!inPostUi(el)) continue;
    const raw = ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
    if (!looksLikeCommentSend(raw)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0) continue;
    try {
      el.click();
      return true;
    } catch (e) {}
  }
  return false;
}"""


def try_comment_on_timeline(
    page: Page,
    text: str,
    *,
    mode: str = "first",
    pool_size: int = 5,
    pick_index: int = 0,
    timeout_ms: int = 35_000,
    typing_delay_ms: int = 35,
) -> tuple[bool, str]:
    """Пытается оставить комментарий к выбранному посту: первому или случайному в пуле."""
    if not (text or "").strip():
        return False, "empty_text"
    safe = text.strip()[:2000]
    m = (mode or "first").strip().lower()
    if m not in ("first", "random"):
        m = "first"
    ps = max(1, min(int(pool_size or 5), 10))
    pick_idx = max(0, int(pick_index or 0))

    page.mouse.wheel(0, 280)
    page.wait_for_timeout(500)
    if m == "random":
        _scroll_feed_for_more_posts(page)
    target_res = page.evaluate(_COMMENT_TARGET_PICK_JS, {"mode": m, "poolSize": ps, "pickIndex": pick_idx})
    target_diag = (target_res or {}).get("diag") if isinstance(target_res, dict) else None
    target_frag = _diag_to_log_fragment(target_diag)
    if not isinstance(target_res, dict) or not target_res.get("ok"):
        reason = str((target_res or {}).get("reason", "target_pick_failed"))
        suffix = f":{target_frag}" if target_frag else ""
        return False, f"target_post_not_found:{reason}:{m}:pool{ps}{suffix}"[:200]
    page.evaluate(_COMMENT_EXPAND_JS)
    page.wait_for_timeout(700)

    deadline = time.monotonic() + min(int(timeout_ms), 45_000) / 1000.0
    found = False
    while time.monotonic() < deadline:
        found = bool(page.evaluate(_COMMENT_COMPOSER_PICK_JS))
        if found:
            break
        page.wait_for_timeout(420)
        page.evaluate(_COMMENT_TARGET_PICK_JS, {"mode": m, "poolSize": ps, "pickIndex": pick_idx})
        page.evaluate(_COMMENT_EXPAND_JS)
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(320)

    if not found:
        suffix = f":{target_frag}" if target_frag else ""
        return False, f"composer_not_found:{m}:pool{ps}{suffix}"[:200]

    page.wait_for_timeout(400)
    td = max(12, min(int(typing_delay_ms or 35), 400))
    try:
        page.keyboard.type(safe, delay=td)
    except Exception as e:
        logger.exception("keyboard.type comment")
        return False, f"type_failed:{e!s}"[:120]

    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1100)
    try:
        page.evaluate(_COMMENT_SUBMIT_JS)
    except Exception:
        logger.exception("comment submit click fallback")
    page.wait_for_timeout(1400)
    base = f"submitted:{m}:pool{ps}"
    if target_frag:
        base += f":{target_frag}"
    return True, base[:200]


def run_warmup_on_profile(
    page: Page,
    profile_url: str,
    *,
    do_like: bool,
    do_comment: bool,
    comment_text: str,
    throttle_preset: str = "medium",
    navigate_timeout_ms: int = 90_000,
) -> tuple[bool, str]:
    """
    Переход на профиль и выполнение отмеченных действий.
    Возвращает (успех_в_целом, текст_для_лога).
    """
    url = _normalize_profile_url(profile_url)
    if not url:
        return False, "empty_url"

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=navigate_timeout_ms)
    except Exception as e:
        logger.exception("goto profile")
        return False, f"nav:{e!s}"[:200]

    tp = (throttle_preset or "medium").strip().lower()
    if not is_valid_throttle_preset(tp):
        tp = "medium"

    parts: list[str] = []
    ok_any = False

    humanize_after_profile_open(page, tp)

    if do_like:
        humanize_before_like(page, tp)
        lk, msg = try_like_first_visible_post(page, throttle_preset=tp)
        parts.append(f"like={'ok' if lk else 'fail'}:{msg}")
        if lk:
            ok_any = True

    if do_comment and comment_text.strip():
        if do_like:
            humanize_between_like_and_comment(page, tp)
        else:
            humanize_before_comment(page, tp)
        ck, cmsg = try_comment_on_timeline(page, comment_text)
        parts.append(f"comment={'ok' if ck else 'fail'}:{cmsg}")
        if ck:
            ok_any = True

    if not do_like and not (do_comment and comment_text.strip()):
        return False, "no_actions"

    # Считаем успехом хотя бы одно запрошенное действие
    wanted = (1 if do_like else 0) + (1 if do_comment and comment_text.strip() else 0)
    if wanted == 0:
        return False, "no_actions"
    if ok_any:
        return True, "; ".join(parts)
    return False, "; ".join(parts)


def parse_storage_state(raw: str | None) -> dict[str, Any] | None:
    if not raw or not str(raw).strip():
        return None
    try:
        import json

        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def profile_url_from_person(canonical_url: str) -> str:
    u = (canonical_url or "").strip()
    if re.match(r"^https?://", u, re.I):
        return u
    return _normalize_profile_url(u)


_REEL_REACTION_JS = r"""
() => {
  // Кандидаты на «реакцию» внутри открытого reel-viewer / video-viewer:
  // 1) кнопка с aria-label, содержащим like / нравится / love / heart / реакц
  // 2) роль кнопки рядом с видео (правая панель действий)
  function score(el) {
    if (!el || el.disabled) return -1;
    const r = el.getBoundingClientRect();
    if (r.width < 16 || r.height < 16) return -1;
    if (r.bottom < 0 || r.top > window.innerHeight) return -1;
    const al = (
      (el.getAttribute('aria-label') || '') + ' ' +
      (el.getAttribute('aria-roledescription') || '') + ' ' +
      (el.getAttribute('title') || '') + ' ' +
      (el.textContent || '')
    ).toLowerCase().slice(0, 320);
    let s = 0;
    if (/(\blike\b|\bhearts?\b|\blove\b|нравится|поставить.*реакц|реакц)/.test(al)) s += 800;
    if (/(remove like|убрать.*нравит|отменить.*нравит)/.test(al)) return -1;
    // на правой стороне reel-viewer
    if (r.right > window.innerWidth * 0.55) s += 200;
    if (r.bottom > window.innerHeight * 0.30 && r.bottom < window.innerHeight * 0.95) s += 120;
    return s;
  }
  // 1) первый клик — на видео-тайл (если ещё не в viewer'е), чтобы открыть reel-viewer
  let inViewer = false;
  try {
    const dlg = document.querySelector('[role="dialog"][aria-modal="true"]');
    if (dlg) {
      const v = dlg.querySelector('video');
      if (v) inViewer = true;
    }
    if (!inViewer) {
      const v = document.querySelector('video');
      if (v) {
        const r = v.getBoundingClientRect();
        if (r.width >= 240 && r.height >= 240) inViewer = true;
      }
    }
  } catch (e) {}
  if (!inViewer) {
    // ищем первую кликабельную карточку Reel/Видео в feed
    const tiles = document.querySelectorAll(
      'a[href*="/reel/"], a[href*="/videos/"], a[href*="/watch/"]'
    );
    for (const a of tiles) {
      const r = a.getBoundingClientRect();
      if (r.width >= 80 && r.height >= 80 && r.top >= -200) {
        a.scrollIntoView({block: 'center', behavior: 'instant'});
        a.click();
        return {clicked: 'tile', href: a.getAttribute('href') || ''};
      }
    }
    return {clicked: 'none', reason: 'no_video_tiles'};
  }
  // 2) уже в viewer'е — ищем кнопку реакции
  let best = null, bestScore = -1;
  for (const el of document.querySelectorAll('div[role="button"], button, [aria-label]')) {
    const s = score(el);
    if (s > bestScore) {bestScore = s; best = el;}
  }
  if (best && bestScore > 300) {
    best.scrollIntoView({block: 'center', behavior: 'instant'});
    best.click();
    return {clicked: 'reaction', score: bestScore, label: (best.getAttribute('aria-label') || '').slice(0, 120)};
  }
  return {clicked: 'none', reason: 'reaction_btn_not_found'};
}
"""


def try_react_to_recipient_reel(
    page: Page,
    canonical_url: str,
    *,
    throttle_preset: str | None = None,
    timeout_ms: int = 25_000,
) -> tuple[bool, str]:
    """
    «Реакция на Reel/Story получателя перед ЛС» — открывает Reels-вкладку профиля, проигрывает
    первое видео и ставит реакцию. Push-уведомление в Messenger получателю помогает «разбудить»
    E2EE-pending пары (когда получатель ещё не открывал новый Messenger).

    Возвращает:
        (True, "reacted_via_reels")            — реакция поставлена через Reels-вкладку
        (True, "reacted_via_videos")           — реакция через videos
        (True, "no_reels_skipped:<reason>")    — у получателя нет публичных Reels/Videos —
                                                  это **не** ошибка, ЛС всё равно должен идти
        (False, "<reason>")                    — попытка не удалась (страница недоступна и т.п.)
    """
    url = (canonical_url or "").strip()
    if not url:
        return True, "no_reels_skipped:empty_url"
    base_url = profile_url_from_person(url)
    if not base_url:
        return True, "no_reels_skipped:bad_url"
    base_url = base_url.rstrip("/")

    # Список вкладок в порядке «качества push-уведомления» — Reels приоритетнее.
    candidates = (
        ("reels", base_url + "/reels"),
        ("videos", base_url + "/videos"),
        ("videos_by", base_url + "/videos_by"),
    )

    last_reason = "no_video_tiles"
    for label, target in candidates:
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=min(45_000, int(timeout_ms) * 2))
        except Exception as e:
            last_reason = f"nav:{e!s}"[:160]
            continue
        try:
            page.wait_for_load_state("networkidle", timeout=min(8_000, int(timeout_ms)))
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        # Лёгкий скролл — чтобы reels-карточки попали в viewport.
        try:
            page.mouse.wheel(0, 480)
            page.wait_for_timeout(700)
            page.mouse.wheel(0, 320)
            page.wait_for_timeout(500)
        except Exception:
            pass

        # 1-я попытка: открыть тайл (если ещё не в viewer'е).
        try:
            res1 = page.evaluate(_REEL_REACTION_JS)
        except Exception as e:
            last_reason = f"js_open:{e!s}"[:160]
            continue
        if isinstance(res1, dict) and res1.get("clicked") == "none":
            last_reason = str(res1.get("reason") or "no_video_tiles")[:160]
            continue
        # Дать viewer'у открыться и видео — стартануть.
        page.wait_for_timeout(random.randint(2200, 4200))
        # 2-я попытка: уже в viewer'е, нажать реакцию.
        try:
            res2 = page.evaluate(_REEL_REACTION_JS)
        except Exception as e:
            last_reason = f"js_react:{e!s}"[:160]
            continue
        if isinstance(res2, dict) and res2.get("clicked") == "reaction":
            page.wait_for_timeout(random.randint(800, 1600))
            # Закрыть viewer (Esc) — чтобы дальше DM-flow открыл профиль чисто.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:
                pass
            return True, f"reacted_via_{label}"
        if isinstance(res2, dict):
            last_reason = str(res2.get("reason") or "reaction_btn_not_found")[:160]
        # Если реакция не нашлась — закроем viewer и попробуем следующий путь.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception:
            pass

    # Ни одна вкладка не дала реакции — это нормальный путь (у получателя может не быть Reels/Videos).
    # Возвращаем True, чтобы DM-flow продолжился: задачу-минимум (попытаться разбудить) выполнили.
    return True, f"no_reels_skipped:{last_reason}"
