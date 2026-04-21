"""Скролл страницы поиска Facebook и извлечение групп / страниц / профилей (sync Playwright)."""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from urllib.parse import quote_plus

from playwright.sync_api import Page

from backend.services.friends_list_scrape import dismiss_facebook_dom_overlays

logger = logging.getLogger(__name__)

SEARCH_TYPE_URL_MAP: dict[str, str] = {
    "groups": "https://www.facebook.com/search/groups/?q={q}",
    "pages": "https://www.facebook.com/search/pages/?q={q}",
    "people": "https://www.facebook.com/search/people/?q={q}",
}


def build_search_url(keyword: str, search_type: str) -> str:
    tpl = SEARCH_TYPE_URL_MAP.get(search_type, SEARCH_TYPE_URL_MAP["groups"])
    return tpl.format(q=quote_plus(keyword.strip()))


# ── JS: извлечение результатов поиска ────────────────────

EXTRACT_SEARCH_GROUPS_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const seen = new Set();
  const results = [];

  const pickCard = (node) => {
    let cur = node;
    let best = node;
    let bestScore = -1;
    for (let d = 0; d < 10 && cur; d += 1, cur = cur.parentElement) {
      const text = (cur.innerText || '').trim();
      if (!text) continue;
      const lines = text.split('\\n').map((s) => s.trim()).filter(Boolean);
      if (text.length > 1800 || lines.length > 28) break;
      let score = 0;
      if (text.length >= 40) score += 3;
      if (lines.length >= 2) score += 3;
      if (lines.length <= 18) score += 2;
      score += Math.min(text.length, 1200) / 250;
      if (score >= bestScore) {
        best = cur;
        bestScore = score;
      }
    }
    return best || node;
  };

  const parseCompactNumber = (raw) => {
    let s = String(raw || '').replace(/\\u00A0/g, '').replace(/\\s+/g, '').replace(/,/g, '.').toLowerCase();
    let mult = 1;
    if (/(тыс\\.?|thousand|k|к)$/i.test(s)) {
      s = s.replace(/(тыс\\.?|thousand|k|к)$/i, '');
      mult = 1000;
    } else if (/(млн\\.?|million|m|м)$/i.test(s)) {
      s = s.replace(/(млн\\.?|million|m|м)$/i, '');
      mult = 1000000;
    }
    s = s.replace(/[^\\d.]/g, '');
    const n = parseFloat(s);
    return Number.isFinite(n) ? Math.round(n * mult) : null;
  };

  const isFriendsInGroupLine = (low) => (
    /друз(ей|ья).*(в группе|уже в группе)/i.test(low) ||
    /friends?.*(in (the )?group|already in group)/i.test(low) ||
    /mutual friends?/i.test(low) ||
    /общ(ий|их) друз/i.test(low)
  );

  const extractMembers = (line) => {
    const low = line.toLowerCase();
    if (isFriendsInGroupLine(low)) return null;
    const patterns = [
      /([\\d.,\\s]+(?:k|к|m|м|тыс\\.?|млн\\.?)?)\\s*(участник(?:а|ов|и)?|members?)/i,
      /([\\d.,\\s]+(?:k|к|m|м|тыс\\.?|млн\\.?)?)\\s*(людей|people)/i,
    ];
    for (const re of patterns) {
      const m = low.match(re);
      if (!m) continue;
      const n = parseCompactNumber(m[1]);
      if (n) return n;
    }
    return null;
  };

  root.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    let u;
    try { u = new URL(href, 'https://www.facebook.com'); } catch { return; }
    const host = u.hostname.replace(/^www\\./, '').replace(/^m\\./, '');
    if (!host.endsWith('facebook.com')) return;
    const parts = u.pathname.replace(/\\/+$/, '').split('/').filter(Boolean);
    if (parts.length < 2 || parts[0].toLowerCase() !== 'groups') return;
    const gid = parts[1];
    if (!gid || /^(create|discover|feed|suggested|browse|notifications|settings|joins)$/i.test(gid)) return;
    const canonical = 'https://www.facebook.com/groups/' + gid;
    if (seen.has(canonical)) return;
    seen.add(canonical);

    let name = '';
    let description = '';
    let memberCount = null;
    let category = '';

    const card = pickCard(a);
    if (card) {
      const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      if (lines.length > 0) name = lines[0];
      for (const line of lines) {
        const ml = line.toLowerCase();
        const count = extractMembers(line);
        if (count && !memberCount) {
          memberCount = count;
          continue;
        }
        if (!category && /(публичн|закрыт|private|public).{0,16}(group|груп)/i.test(ml)) {
          category = line;
          continue;
        }
        if (isFriendsInGroupLine(ml)) continue;
        if (line.length > 20 && line.length < 300 && !description && line !== name) {
          description = line;
        }
      }
    }
    if (!name) {
      name = (a.innerText || a.textContent || a.getAttribute('aria-label') || '').split('\\n')[0].trim();
    }
    if (name.length < 2) return;
    results.push({ url: canonical, name, description, memberCount, category });
  });
  return results;
}
"""

EXTRACT_SEARCH_PAGES_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const seen = new Set();
  const results = [];

  const pickCard = (node) => {
    let cur = node;
    let best = node;
    let bestScore = -1;
    for (let d = 0; d < 10 && cur; d += 1, cur = cur.parentElement) {
      const text = (cur.innerText || '').trim();
      if (!text) continue;
      const lines = text.split('\\n').map((s) => s.trim()).filter(Boolean);
      if (text.length > 1800 || lines.length > 28) break;
      let score = 0;
      if (text.length >= 40) score += 3;
      if (lines.length >= 2) score += 3;
      if (lines.length <= 18) score += 2;
      score += Math.min(text.length, 1200) / 250;
      if (score >= bestScore) {
        best = cur;
        bestScore = score;
      }
    }
    return best || node;
  };

  const parseCompactNumber = (raw) => {
    let s = String(raw || '').replace(/\\u00A0/g, '').replace(/\\s+/g, '').replace(/,/g, '.').toLowerCase();
    let mult = 1;
    if (/(тыс\\.?|thousand|k|к)$/i.test(s)) {
      s = s.replace(/(тыс\\.?|thousand|k|к)$/i, '');
      mult = 1000;
    } else if (/(млн\\.?|million|m|м)$/i.test(s)) {
      s = s.replace(/(млн\\.?|million|m|м)$/i, '');
      mult = 1000000;
    }
    s = s.replace(/[^\\d.]/g, '');
    const n = parseFloat(s);
    return Number.isFinite(n) ? Math.round(n * mult) : null;
  };

  const isFriendSnippetLine = (low) => (
    /mutual friends?/i.test(low) ||
    /общ(ий|их) друз/i.test(low) ||
    /friends?.*(already|in common)/i.test(low)
  );

  const extractAudience = (line) => {
    const low = line.toLowerCase();
    if (isFriendSnippetLine(low)) return null;
    const patterns = [
      /([\\d.,\\s]+(?:k|к|m|м|тыс\\.?|млн\\.?)?)\\s*(подписчик(?:а|ов|и)?|followers?)/i,
      /([\\d.,\\s]+(?:k|к|m|м|тыс\\.?|млн\\.?)?)\\s*(людям нравится|нравит(?:ся)?|likes?|like)/i,
    ];
    for (const re of patterns) {
      const m = low.match(re);
      if (!m) continue;
      const n = parseCompactNumber(m[1]);
      if (n) return n;
    }
    return null;
  };

  root.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    let u;
    try { u = new URL(href, 'https://www.facebook.com'); } catch { return; }
    const host = u.hostname.replace(/^www\\./, '').replace(/^m\\./, '');
    if (!host.endsWith('facebook.com')) return;

    const path = u.pathname.replace(/\\/+$/, '');
    const parts = path.split('/').filter(Boolean);
    if (parts.length !== 1) return;
    const seg = parts[0];
    const skip = new Set([
      'search', 'groups', 'friends', 'watch', 'reel', 'marketplace', 'events',
      'gaming', 'ads', 'pages', 'help', 'settings', 'messages', 'notifications',
      'saved', 'jobs', 'stories', 'login', 'reg', 'recover', 'policies', 'privacy',
      'legal', 'lite', 'profile.php', 'checkpoint', 'photo.php', 'story.php',
      'permalink.php', 'fundraisers', 'hashtag',
    ]);
    if (skip.has(seg.toLowerCase())) return;
    if (/^\\d+$/.test(seg) && seg.length < 6) return;
    const canonical = 'https://www.facebook.com/' + seg;
    if (seen.has(canonical)) return;
    seen.add(canonical);

    let name = '';
    let description = '';
    let memberCount = null;
    let category = '';

    const card = pickCard(a);
    if (card) {
      const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      if (lines.length > 0) name = lines[0];
      for (const line of lines) {
        const ml = line.toLowerCase();
        const count = extractAudience(line);
        if (count && !memberCount) {
          memberCount = count;
          continue;
        }
        if (isFriendSnippetLine(ml)) continue;
        if (line.length >= 3 && line.length <= 60 && !category && line !== name) {
          category = line;
        } else if (line.length > 20 && line.length < 300 && !description && line !== name && line !== category) {
          description = line;
        }
      }
    }
    if (!name) {
      name = (a.innerText || a.textContent || a.getAttribute('aria-label') || '').split('\\n')[0].trim();
    }
    if (name.length < 2) return;
    results.push({ url: canonical, name, description, memberCount, category });
  });
  return results;
}
"""

EXTRACT_SEARCH_PEOPLE_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const seen = new Set();
  const results = [];

  const pickCard = (node) => {
    let cur = node;
    let best = node;
    let bestScore = -1;
    for (let d = 0; d < 10 && cur; d += 1, cur = cur.parentElement) {
      const text = (cur.innerText || '').trim();
      if (!text) continue;
      const lines = text.split('\\n').map((s) => s.trim()).filter(Boolean);
      if (text.length > 1800 || lines.length > 28) break;
      let score = 0;
      if (text.length >= 40) score += 3;
      if (lines.length >= 2) score += 3;
      if (lines.length <= 18) score += 2;
      score += Math.min(text.length, 1200) / 250;
      if (score >= bestScore) {
        best = cur;
        bestScore = score;
      }
    }
    return best || node;
  };

  const parseCompactNumber = (raw) => {
    let s = String(raw || '').replace(/\\u00A0/g, '').replace(/\\s+/g, '').replace(/,/g, '.').toLowerCase();
    let mult = 1;
    if (/(тыс\\.?|thousand|k|к)$/i.test(s)) {
      s = s.replace(/(тыс\\.?|thousand|k|к)$/i, '');
      mult = 1000;
    } else if (/(млн\\.?|million|m|м)$/i.test(s)) {
      s = s.replace(/(млн\\.?|million|m|м)$/i, '');
      mult = 1000000;
    }
    s = s.replace(/[^\\d.]/g, '');
    const n = parseFloat(s);
    return Number.isFinite(n) ? Math.round(n * mult) : null;
  };

  const isMutualFriendsLine = (low) => (
    /mutual friends?/i.test(low) ||
    /общ(ий|их) друз/i.test(low) ||
    /friends?.*(in common|already)/i.test(low)
  );

  const extractAudience = (line) => {
    const low = line.toLowerCase();
    if (isMutualFriendsLine(low)) return null;
    const patterns = [
      /([\\d.,\\s]+(?:k|к|m|м|тыс\\.?|млн\\.?)?)\\s*(подписчик(?:а|ов|и)?|followers?)/i,
      /([\\d.,\\s]+(?:k|к|m|м|тыс\\.?|млн\\.?)?)\\s*(друз(?:ья|ей)?|friends?)/i,
    ];
    for (const re of patterns) {
      const m = low.match(re);
      if (!m) continue;
      const n = parseCompactNumber(m[1]);
      if (n) return n;
    }
    return null;
  };

  const cleanProfileUrl = (raw) => {
    let u;
    try { u = new URL(raw, 'https://www.facebook.com'); } catch { return null; }
    const host = u.hostname.replace(/^www\\./, '').replace(/^m\\./, '');
    if (!host.endsWith('facebook.com')) return null;
    if (u.pathname === '/profile.php' || u.pathname.startsWith('/profile.php')) {
      const id = u.searchParams.get('id');
      if (id && /^\\d+$/.test(id)) return 'https://www.facebook.com/profile.php?id=' + id;
      return null;
    }
    const path = u.pathname.replace(/\\/+$/, '');
    const prn = path.match(/^\\/profile\\/(\\d{5,})$/i);
    if (prn) return 'https://www.facebook.com/profile.php?id=' + prn[1];
    const urn = path.match(/^\\/user\\/(\\d{5,})$/i);
    if (urn) return 'https://www.facebook.com/profile.php?id=' + urn[1];
    const parts = path.split('/').filter(Boolean);
    if (parts.length !== 1) return null;
    const seg = parts[0];
    const skip = new Set([
      'search', 'groups', 'friends', 'watch', 'reel', 'marketplace', 'events',
      'gaming', 'ads', 'pages', 'help', 'settings', 'messages', 'notifications',
      'saved', 'jobs', 'stories', 'login', 'reg', 'recover', 'policies', 'privacy',
      'legal', 'lite', 'checkpoint', 'photo.php', 'story.php', 'permalink.php',
      'fundraisers', 'hashtag', 'people',
    ]);
    if (skip.has(seg.toLowerCase())) return null;
    if (/^\\d+$/.test(seg)) {
      if (seg.length >= 6) return 'https://www.facebook.com/profile.php?id=' + seg;
      return null;
    }
    return 'https://www.facebook.com/' + seg;
  };

  root.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    const canonical = cleanProfileUrl(href);
    if (!canonical || seen.has(canonical)) return;
    seen.add(canonical);

    let name = '';
    let description = '';
    let memberCount = null;

    const card = pickCard(a);
    if (card) {
      const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      if (lines.length > 0) name = lines[0];
      for (const line of lines.slice(1)) {
        const low = line.toLowerCase();
        const count = extractAudience(line);
        if (count && !memberCount) {
          memberCount = count;
          continue;
        }
        if (isMutualFriendsLine(low)) continue;
        if (/^(add friend|добавить в друзья|message|сообщение|подписаться|follow)$/i.test(low)) continue;
        if (line.length > 3 && line.length < 180 && !description) {
          description = line;
        }
      }
    }
    if (!name) {
      name = (a.innerText || a.textContent || a.getAttribute('aria-label') || '').split('\\n')[0].trim();
    }
    const noise = ['add friend', 'добавить в друзья', 'message', 'сообщение', 'подписаться', 'follow'];
    if (noise.some(n => name.toLowerCase() === n)) return;
    if (name.length < 2) return;
    results.push({ url: canonical, name, description, memberCount, category: '' });
  });
  return results;
}
"""

_JS_BY_TYPE: dict[str, str] = {
    "groups": EXTRACT_SEARCH_GROUPS_JS,
    "pages": EXTRACT_SEARCH_PAGES_JS,
    "people": EXTRACT_SEARCH_PEOPLE_JS,
}

SCROLL_SEARCH_JS = """
(step) => {
  const main = document.querySelector('[role="main"]');
  if (main) main.scrollBy(0, step || 600);
  window.scrollBy(0, step || 600);
}
"""


def scroll_search_results(
    page: Page,
    search_type: str,
    *,
    max_results: int = 50,
    max_rounds: int = 600,
    cancelled: Callable[[], bool] | None = None,
    on_round: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """
    Скроллит страницу поисковой выдачи FB и возвращает список найденных сущностей.
    Каждый элемент: {url, name, description, memberCount, category}.
    """
    js = _JS_BY_TYPE.get(search_type, EXTRACT_SEARCH_GROUPS_JS)
    seen_urls: set[str] = set()
    results: list[dict] = []
    no_new_rounds = 0
    last_total = 0

    for i in range(max_rounds):
        if cancelled and cancelled():
            break

        dismiss_facebook_dom_overlays(page)
        try:
            raw = page.evaluate(js)
        except Exception:
            logger.debug("evaluate search JS failed round=%d", i, exc_info=True)
            raw = []

        if isinstance(raw, list):
            for item in raw:
                url = (item.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                results.append({
                    "url": url,
                    "name": (item.get("name") or "").strip()[:512],
                    "description": (item.get("description") or "").strip()[:2000],
                    "member_count": item.get("memberCount"),
                    "category": (item.get("category") or "").strip()[:255],
                })

        total = len(results)
        if on_round and (i % 3 == 0 or i == 0):
            on_round(i, total)

        if total >= max_results:
            break

        if total == last_total:
            no_new_rounds += 1
        else:
            no_new_rounds = 0
        last_total = total

        if no_new_rounds >= 40:
            logger.info("search scroll: %d rounds w/o new results, stopping (got %d)", no_new_rounds, total)
            break

        step = random.randint(450, 800)
        try:
            page.evaluate(SCROLL_SEARCH_JS, step)
        except Exception:
            logger.debug("search scroll JS failed round=%d", i, exc_info=True)

        time.sleep(random.uniform(0.7, 1.8))

        if no_new_rounds > 10:
            try:
                page.keyboard.press("End")
            except Exception:
                pass
            time.sleep(random.uniform(0.5, 1.2))

    return results[:max_results]
