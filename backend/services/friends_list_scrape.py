"""Скролл страницы списка друзей Facebook и извлечение ссылок (sync Playwright, как в app.py)."""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from playwright.sync_api import Page

from backend.services.fb_url_normalize import (
    normalize_facebook_group_members_url,
    normalize_facebook_profile_url,
)

logger = logging.getLogger(__name__)

EXTRACT_FRIENDS_JS = """
() => {
  // Для public-профилей (creator) список «друзей» по факту отображается как
  // подписчики внутри другого DOM-контейнера — иногда вне [role="main"].
  // Поэтому если в main мало ссылок-на-профили, падаем fallback'ом в body.
  const mainEl = document.querySelector('[role="main"]');
  const bodyEl = document.body;
  const seen = new Set();
  const rows = [];

  const cleanUrl = (raw) => {
    if (!raw || typeof raw !== 'string') return null;
    let u;
    try { u = new URL(raw, 'https://www.facebook.com'); } catch { return null; }
    const host = u.hostname.replace(/^www\\./, '');
    if (!host.endsWith('facebook.com')) return null;
    if (u.pathname === '/profile.php') {
      const id = u.searchParams.get('id');
      if (id && /^\\d+$/.test(id)) return 'https://www.facebook.com/profile.php?id=' + id;
      return null;
    }
    const path = u.pathname.replace(/\\/+$/, '');
    const pm = path.match(/^\\/people\\/[^/]+\\/(\\d+)$/i);
    if (pm && pm[1].length >= 6) {
      return 'https://www.facebook.com/profile.php?id=' + pm[1];
    }
    const gum = path.match(/^\\/groups\\/[^/]+\\/user\\/(\\d+)$/i);
    if (gum && gum[1].length >= 6) {
      return 'https://www.facebook.com/profile.php?id=' + gum[1];
    }
    const prn = path.match(/^\\/profile\\/(\\d{5,})$/i);
    if (prn) {
      return 'https://www.facebook.com/profile.php?id=' + prn[1];
    }
    const urn = path.match(/^\\/user\\/(\\d{5,})$/i);
    if (urn) {
      return 'https://www.facebook.com/profile.php?id=' + urn[1];
    }
    const m = path.match(/^\\/([^\\/]+)$/);
    if (!m) return null;
    const seg = decodeURIComponent(m[1]);
    const skip = new Set([
      'friends', 'groups', 'watch', 'reel', 'marketplace', 'events', 'gaming',
      'ads', 'pages', 'help', 'settings', 'messages', 'notifications', 'saved',
      'jobs', 'stories', 'login', 'reg', 'recover', 'policies', 'privacy',
      'legal', 'lite', 'r.php', 'checkpoint', 'photo.php', 'story.php',
      'permalink.php', 'friends_center', 'sales', 'fundraisers', 'opportunity',
    ]);
    if (skip.has(seg.toLowerCase())) return null;
    if (/^\\d+$/.test(seg)) {
      if (seg.length >= 6) return 'https://www.facebook.com/profile.php?id=' + seg;
      return null;
    }
    return 'https://www.facebook.com/' + seg;
  };

  const normText = (raw) => String(raw || '').replace(/\\s+/g, ' ').trim();
  const noise = [
    'facebook', 'добавить в друзья', 'add friend', 'message', 'сообщение',
    'follow', 'подписаться', 'following', 'подписки', 'followers', 'подписчики',
    'friends', 'друзья', 'see all', 'посмотреть все', 'view all', 'more',
    'ещё', 'нравится', 'like', 'reply', 'ответить', 'подробнее', 'learn more',
    'профиль', 'profile', 'send message', 'написать', 'watch', 'reels',
  ];
  const plausibleName = (raw) => {
    const s = normText(raw);
    if (!s || s.length < 2 || s.length > 120) return '';
    const low = s.toLowerCase();
    if (/^https?:\\/\\//.test(low) || /^\\d+$/.test(low)) return '';
    if (noise.some((x) => low === x || low.includes(x + ' ·') || low.includes('· ' + x))) return '';
    const letters = (s.match(/[A-Za-zÀ-ÖØ-öø-ÿĀ-žА-Яа-яЁёІіЇїЄєҐґ]/g) || []).length;
    if (letters < 2) return '';
    return s;
  };
  const extractName = (a) => {
    const candidates = [];
    const push = (raw, weight) => {
      const s = plausibleName(raw);
      if (!s) return;
      candidates.push({ s, weight: Number(weight || 0), len: s.length });
    };
    push(a.innerText || '', 10);
    push(a.textContent || '', 8);
    push(a.getAttribute('aria-label') || '', 9);
    push(a.getAttribute('title') || '', 7);
    const img = a.querySelector('img[alt]');
    if (img) push(img.getAttribute('alt') || '', 6);
    let cur = a;
    for (let depth = 0; depth < 4 && cur; depth += 1, cur = cur.parentElement) {
      const text = String(cur.innerText || '');
      if (!text) continue;
      const parts = text.split(/\\n+/).map((x) => x.trim()).filter(Boolean).slice(0, 6);
      for (let i = 0; i < parts.length; i += 1) {
        push(parts[i], 5 - i);
      }
    }
    if (!candidates.length) return '';
    candidates.sort((a, b) => (b.weight - a.weight) || (a.len - b.len));
    return candidates[0].s;
  };
  const countProfileLinks = (scope) => {
    const urls = new Set();
    scope.querySelectorAll('a[href]').forEach((a) => {
      const href = cleanUrl(a.getAttribute('href'));
      if (href) urls.add(href);
    });
    return urls.size;
  };
  // Стартовый scope — тот, где больше ссылок на профили.
  const mainCount = mainEl ? countProfileLinks(mainEl) : 0;
  const bodyCount = countProfileLinks(bodyEl);
  const root = mainEl && mainCount >= 4 ? mainEl : bodyEl;
  let scope = root;
  let bestScore = (root === mainEl ? mainCount : bodyCount) * 10;
  // Ищем самый «насыщенный ссылками на профили» контейнер — теперь и внутри
  // body, не только main. Это покрывает public-creator профили и группы, где
  // список вынесен в отдельный модал/drawer вне [role="main"].
  const searchRoots = mainEl ? [mainEl, bodyEl] : [bodyEl];
  const searchSeen = new Set();
  searchRoots.forEach((searchRoot) => {
    searchRoot.querySelectorAll('section, div, ul').forEach((el) => {
      if (searchSeen.has(el)) return;
      searchSeen.add(el);
      const rect = el.getBoundingClientRect();
      if (rect.width < 220 || rect.height < 80) return;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') return;
      const n = countProfileLinks(el);
      if (n < 4) return;
      const areaPenalty =
        Math.max(1, Math.min(rect.width, window.innerWidth) * Math.max(80, Math.min(rect.height, window.innerHeight * 2))) / 35000;
      const score = n * 10 - areaPenalty;
      if (score > bestScore) {
        bestScore = score;
        scope = el;
      }
    });
  });

  scope.querySelectorAll('a[href]').forEach((a) => {
    let raw = a.getAttribute('href');
    if (raw && raw.includes('l.facebook.com/l.php')) {
      try {
        const u = new URL(raw, 'https://www.facebook.com');
        const t = u.searchParams.get('u');
        if (t) raw = decodeURIComponent(t);
      } catch {}
    }
    const href = cleanUrl(raw);
    if (!href || seen.has(href)) return;
    const rawName = extractName(a);
    let name = rawName;
    if (!name) return;
    const x = rawName.toLowerCase();
    const restricted = (
      x === 'facebook user' ||
      x === 'utilisateur facebook' ||
      x === 'utilisateur de facebook' ||
      x === 'usuario de facebook' ||
      /^benutzer[\\s-]*facebook$/i.test(rawName) ||
      /пользователь\\s+facebook/i.test(rawName) ||
      /пользователь\\s+фейсбук/i.test(rawName) ||
      /facebook[-\\s]?nutzer/i.test(x)
    );
    seen.add(href);
    rows.push({ url: href, name, restricted: !!restricted });
  });

  return rows;
}
"""

OWNER_PAGE_NAME_JS = """
() => {
  const clean = (s) => {
    if (!s || typeof s !== 'string') return '';
    let x = s.replace(/\\s*\\|\\s*Facebook.*$/i, '').replace(/^Facebook\\s*-?\\s*/i, '').trim();
    x = x.replace(/\\s*·\\s*Friends.*$/i, '').replace(/^Friends\\s*·\\s*/i, '').trim();
    x = x.replace(/\\s*-\\s*Friends.*$/i, '').trim();
    x = x.replace(/\\s*·\\s*(Members|Участники).*$/i, '').replace(/^(Members|Участники)\\s*·\\s*/i, '').trim();
    x = x.replace(/\\s*-\\s*(Members|Участники).*$/i, '').trim();
    return x.replace(/\\s+/g, ' ').trim();
  };
  const og = document.querySelector('meta[property="og:title"]');
  if (og && og.content) {
    const t = clean(og.content);
    if (t.length >= 2 && t.length < 100) return t;
  }
  const t = clean(document.title || '');
  if (t.length >= 2 && t.length < 100) return t;
  const h1 = document.querySelector('h1[dir="auto"], h1 span[dir="auto"], [role="main"] h1');
  if (h1) {
    const tx = clean(h1.innerText || '');
    if (tx.length >= 2 && tx.length < 100) return tx;
  }
  return '';
}
"""

SCROLL_DOC_HEIGHT_JS = """
() => Math.max(
  document.body ? document.body.scrollHeight : 0,
  document.documentElement ? document.documentElement.scrollHeight : 0,
  document.scrollingElement ? document.scrollingElement.scrollHeight : 0
)
"""

SCROLL_FRIENDS_STEP_JS = """
(delta) => {
  const main = document.querySelector('[role="main"]');
  if (main && main.scrollHeight > main.clientHeight + 100) {
    main.scrollTop = Math.min(main.scrollTop + delta, main.scrollHeight);
  }
  window.scrollBy(0, delta);
}
"""

SCROLL_TO_END_JS = """
() => {
  const main = document.querySelector('[role="main"]');
  if (main && main.scrollHeight > main.clientHeight + 80) {
    main.scrollTop = main.scrollHeight;
  }
  const se = document.scrollingElement || document.documentElement;
  window.scrollTo(0, se.scrollHeight);
}
"""

# Прокрутка вложенных overflow-областей: у нового FB список друзей часто не растягивает document.
SCROLL_FRIENDS_ENHANCED_JS = """
(delta) => {
  const tryScroll = (el, d) => {
    if (!el) return;
    if (el.scrollHeight > el.clientHeight + 40) {
      el.scrollTop = Math.min(el.scrollTop + d, el.scrollHeight);
    }
  };
  const main = document.querySelector('[role="main"]');
  tryScroll(main, delta);
  let best = null;
  let bestScore = 0;
  if (main) {
    main.querySelectorAll('div').forEach((el) => {
      const sh = el.scrollHeight;
      const ch = el.clientHeight;
      if (sh > ch + 80) {
        const st = getComputedStyle(el).overflowY;
        if (st === 'auto' || st === 'scroll' || st === 'overlay') {
          const n = el.querySelectorAll('a[href]').length;
          if (n > bestScore) { bestScore = n; best = el; }
        }
      }
    });
  }
  tryScroll(best, delta);
  window.scrollBy(0, delta);
}
"""

DISMISS_FB_DOM_OVERLAYS_JS = """
() => {
  const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const fireClick = (el) => {
    if (!el) return false;
    try {
      el.click();
      return true;
    } catch (e1) {
      try {
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        return true;
      } catch (e2) {
        return false;
      }
    }
  };
  const labelOf = (b) => {
    let t = norm(b.innerText || b.textContent || '');
    if (!t || t.length < 2) t = norm(b.getAttribute('aria-label') || '');
    if (!t || t.length < 2) {
      const sp = b.querySelector && b.querySelector('span');
      if (sp) t = norm(sp.innerText || sp.textContent || '');
    }
    return t;
  };
  const dialogRoots = () =>
    Array.from(
      document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')
    ).filter(visible);

  const dlgNormText = (root) => norm(root.innerText || root.textContent || '').slice(0, 900);

  const couldGroupInviteModal = (root) => {
    const d = dlgNormText(root);
    return (
      d.includes('отклонить приглашен') ||
      d.includes('decline invitation') ||
      (d.includes('приглашен') && d.includes('групп')) ||
      (d.includes('invitation') && (d.includes('group') || d.includes('групп')))
    );
  };

  const wantDeclineInvitePrimary = (t) => {
    if (!t) return false;
    if (t.includes('отмена') || t.includes('cancel')) return false;
    if (t === 'отклонить' || (t.includes('отклонить') && t.length < 48)) return true;
    if (t === 'decline' || (t.startsWith('decline') && t.length < 48 && !t.includes('cancel')))
      return true;
    return false;
  };

  const wantDeny = (t) => {
    if (!t) return false;
    return (
      t.includes('блокировать') ||
      (t.includes('block') && !t.includes('unblock')) ||
      t.includes('not now') || t.includes('не сейчас') || t.includes('отклонить') ||
      t.includes('decline') ||
      t.includes('maybe later') || t.includes('позже') || t === 'нет' ||
      t.includes('dismiss') || t.includes('закрыть') || t === 'close' ||
      t.includes('no thanks') || t.includes('не интересно')
    );
  };

  const clickablesIn = (root) =>
    Array.from(
      root.querySelectorAll(
        'button, a, [role="button"], div[role="button"], span[role="button"], div[tabindex="0"]'
      )
    );

  for (const root of dialogRoots()) {
    if (!couldGroupInviteModal(root)) continue;
    const nodes = clickablesIn(root).filter(visible);
    const primaries = [];
    for (const b of nodes) {
      const lb = labelOf(b);
      if (wantDeclineInvitePrimary(lb)) primaries.push({ b, lb });
    }
    primaries.sort((x, y) => x.lb.length - y.lb.length);
    if (primaries.length && fireClick(primaries[0].b)) return 'invite_decline';
    for (const b of nodes) {
      const t = labelOf(b);
      if (wantDeny(t) && fireClick(b)) return 'invite_fallback';
    }
  }

  for (const root of dialogRoots()) {
    if (!visible(root)) continue;
    for (const b of clickablesIn(root)) {
      if (!visible(b)) continue;
      const t = labelOf(b);
      if (wantDeny(t) && fireClick(b)) return 'deny';
    }
  }
  for (const b of document.querySelectorAll('div[role="banner"] button, [aria-label]')) {
    if (!visible(b)) continue;
    const t = norm(b.innerText || b.textContent || b.getAttribute('aria-label') || '');
    if (wantDeny(t) && fireClick(b)) return 'banner';
  }
  return '';
}
"""

# Осталось ли окно «Отклонить приглашение?» / Decline invitation — для резерва Escape.
_INVITE_DECLINE_MODAL_VISIBLE_JS = """
() => {
  const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  for (const root of document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], [aria-modal="true"]'
  )) {
    if (!visible(root)) continue;
    const d = norm(root.innerText || '').slice(0, 900);
    if (d.includes('отклонить приглашен') || d.includes('decline invitation')) return true;
  }
  return false;
}
"""


def dismiss_facebook_dom_overlays(page: Page) -> None:
    """Закрыть DOM-модалки FB (уведомления, cookie, приглашения в группу и т.д.)."""
    try:
        page.evaluate(DISMISS_FB_DOM_OVERLAYS_JS)
        if page.evaluate(_INVITE_DECLINE_MODAL_VISIBLE_JS):
            try:
                page.keyboard.press("Escape")
            except Exception:
                logger.debug("dismiss: Escape after invite modal", exc_info=True)
            page.wait_for_timeout(180)
            page.evaluate(DISMISS_FB_DOM_OVERLAYS_JS)
    except Exception:
        logger.debug("dismiss_facebook_dom_overlays failed", exc_info=True)


SCROLL_TO_END_ENHANCED_JS = """
() => {
  const main = document.querySelector('[role="main"]');
  if (main && main.scrollHeight > main.clientHeight + 80) {
    main.scrollTop = main.scrollHeight;
  }
  let best = null;
  let bestScore = 0;
  if (main) {
    main.querySelectorAll('div').forEach((el) => {
      const sh = el.scrollHeight;
      const ch = el.clientHeight;
      if (sh > ch + 80) {
        const st = getComputedStyle(el).overflowY;
        if (st === 'auto' || st === 'scroll' || st === 'overlay') {
          const n = el.querySelectorAll('a[href]').length;
          if (n > bestScore) { bestScore = n; best = el; }
        }
      }
    });
    if (best) best.scrollTop = best.scrollHeight;
  }
  const se = document.scrollingElement || document.documentElement;
  window.scrollTo(0, se.scrollHeight);
}
"""

PARSE_EXPECTED_FRIENDS_COUNT_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const raw = root.innerText || '';
  const t = raw.replace(/\\u00A0/g, ' ').replace(/\\r/g, '');
  const href = String(location.href || '').toLowerCase();
  const path = String(location.pathname || '').toLowerCase();
  const mode =
    /\\/following(?:[/?#]|$)/.test(path) || /\\/following(?:[/?#]|$)/.test(href) ? 'following' :
    /\\/followers(?:[/?#]|$)/.test(path) || /\\/followers(?:[/?#]|$)/.test(href) ? 'followers' :
    /\\/friends(?:[/?#]|$)/.test(path) || /\\/friendlist(?:[/?#]|$)/.test(path) || /\\/friends_/.test(path) ? 'friends' :
    /\\/groups\\/[^/]+\\/members(?:[/?#]|$)/.test(path) || /\\/members(?:[/?#]|$)/.test(path) ? 'members' :
    'auto';

  const variants = [];
  const lines = t.split('\\n').map((x) => x.trim()).filter(Boolean);
  for (let i = 0; i < lines.length; i += 1) {
    variants.push(lines[i]);
    if (i + 1 < lines.length) variants.push(lines[i] + ' ' + lines[i + 1]);
    if (i + 2 < lines.length) variants.push(lines[i] + ' ' + lines[i + 1] + ' ' + lines[i + 2]);
  }
  variants.push(t);

  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  const pickBound = (n, min, max) => (n >= min && n <= max ? n : 0);
  const labelRe = (labels) => '(?:' + labels.map((x) => escapeRe(x)).join('|') + ')';

  const parseCountNearLabels = (labels, min, max, samples) => {
    const label = labelRe(labels);
    const src = Array.isArray(samples) && samples.length ? samples : variants;
    for (const sample of src) {
      // FB сейчас рендерит «Участники · 14 236» (LABEL · NUMBER) — этот
      // паттерн идёт первым. Раньше его не было — поэтому expected всегда =0.
      let m = sample.match(new RegExp(label + '\\\\s*[·•:\\\\-—\\\\s]+\\\\s*(\\\\d[\\\\d\\\\s,.]{1,14})', 'i'));
      if (m) {
        const rawInt = m[1].replace(/\\s/g, '');
        if (!/[,.]\\d{3,}/.test(rawInt)) {
          const n = parseInt(rawInt.replace(/,/g, ''), 10);
          const ok = pickBound(n, min, max);
          if (ok) return ok;
        }
      }
      m = sample.match(new RegExp('(\\\\d{1,4})\\\\s*тыс\\\\.?\\\\s*(?:[—\\\\-–:]\\\\s*)?' + label, 'i'));
      if (m) {
        const n = parseInt(m[1], 10) * 1000;
        const ok = pickBound(n, min, max);
        if (ok) return ok;
      }
      m = sample.match(new RegExp('(\\\\d+)[,.](\\\\d+)\\\\s*тыс[^\\\\n]{0,120}' + label, 'i'));
      if (m) {
        const n = Math.round(parseFloat(m[1] + '.' + m[2]) * 1000);
        const ok = pickBound(n, min, max);
        if (ok) return ok;
      }
      m = sample.match(new RegExp('(\\\\d+(?:[,.]\\\\d+)?)\\\\s*[kк]\\\\b[^\\\\n]{0,120}' + label, 'i'));
      if (m) {
        const n = Math.round(parseFloat(m[1].replace(',', '.')) * 1000);
        const ok = pickBound(n, min, max);
        if (ok) return ok;
      }
      m = sample.match(new RegExp('(\\\\d[\\\\d\\\\s,.]{1,14})\\\\s*(?:[—\\\\-–:]\\\\s*)?' + label, 'i'));
      if (m) {
        const rawInt = m[1].replace(/\\s/g, '');
        if (!/[,.]\\d/.test(rawInt)) {
          const n = parseInt(rawInt.replace(/,/g, ''), 10);
          const ok = pickBound(n, min, max);
          if (ok) return ok;
        }
      }
    }
    return 0;
  };

  const FRIEND_LABELS = ['друзья', 'друга', 'друзей', 'friends', 'friend'];
  const FOLLOWER_LABELS = ['подписчики', 'подписчик', 'подписчика', 'подписчиков', 'followers', 'follower'];
  const FOLLOWING_LABELS = ['подписки', 'подписок', 'following'];
  const MEMBER_LABELS = ['участники', 'участник', 'участника', 'участников', 'members', 'member', 'в группе', 'in this group', 'in group', 'group members'];
  const topVariants = [];
  const seenTop = new Set();
  const visible = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  // Узкий селектор: header'ы группы/профиля содержатся в h1/h2/h3/strong/a.
  // span/div давали много ложных срабатываний и сильно тормозили
  // (на странице группы их 5000+, querySelector + getBoundingClientRect
  // на каждом — 200-500мс на каждый вызов parse_expected_friends_count).
  root.querySelectorAll('h1, h2, h3, strong, a').forEach((el) => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect();
    if (r.top > Math.max(window.innerHeight * 1.6, 1200) || r.bottom < -40) return;
    const txt = String(el.innerText || el.textContent || '').replace(/\\u00A0/g, ' ').replace(/\\s+/g, ' ').trim();
    if (!txt || txt.length < 4 || txt.length > 90) return;
    if (seenTop.has(txt)) return;
    seenTop.add(txt);
    topVariants.push(txt);
  });

  if (mode === 'following') {
    return parseCountNearLabels(FOLLOWING_LABELS, 50, 500000, topVariants) || parseCountNearLabels(FOLLOWING_LABELS, 50, 500000) || 0;
  }
  if (mode === 'followers') {
    return parseCountNearLabels(FOLLOWER_LABELS, 50, 500000, topVariants) || parseCountNearLabels(FOLLOWER_LABELS, 50, 500000) || 0;
  }
  if (mode === 'friends') {
    return parseCountNearLabels(FRIEND_LABELS, 50, 100000, topVariants) || parseCountNearLabels(FRIEND_LABELS, 50, 100000) || 0;
  }
  if (mode === 'members') {
    return parseCountNearLabels(MEMBER_LABELS, 10, 500000, topVariants) || parseCountNearLabels(MEMBER_LABELS, 10, 500000) || 0;
  }

  const candidates = [
    parseCountNearLabels(FRIEND_LABELS, 50, 100000),
    parseCountNearLabels(FOLLOWING_LABELS, 50, 500000),
    parseCountNearLabels(FOLLOWER_LABELS, 50, 500000),
    parseCountNearLabels(MEMBER_LABELS, 10, 500000),
  ].filter(Boolean);
  if (candidates.length) {
    return candidates[0];
  }
  return 0;
}
"""

CLICK_CONNECTIONS_SEE_ALL_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const visible = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > -40 && r.top < window.innerHeight + 200;
  };
  const label = (el) => String(el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || '')
    .replace(/\\s+/g, ' ')
    .trim()
    .toLowerCase();
  const wants = (txt) => (
    txt === 'посмотреть все' ||
    txt === 'show all' ||
    txt === 'see all' ||
    txt === 'view all' ||
    txt.includes('посмотреть все') ||
    txt.includes('see all') ||
    txt.includes('view all')
  );
  const nodes = root.querySelectorAll('button, a[href], [role="button"], div[role="button"], span[role="button"]');
  for (const el of nodes) {
    if (!visible(el)) continue;
    const txt = label(el);
    if (!wants(txt)) continue;
    try {
      el.click();
      return txt || 'clicked';
    } catch (_e) {}
  }
  return '';
}
"""

CONNECTIONS_LIST_STATE_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const cleanUrl = (raw) => {
    if (!raw || typeof raw !== 'string') return null;
    let u;
    try { u = new URL(raw, 'https://www.facebook.com'); } catch { return null; }
    const host = u.hostname.replace(/^www\\./, '');
    if (!host.endsWith('facebook.com')) return null;
    if (u.pathname === '/profile.php') {
      const id = u.searchParams.get('id');
      return id && /^\\d+$/.test(id) ? ('https://www.facebook.com/profile.php?id=' + id) : null;
    }
    const path = u.pathname.replace(/\\/+$/, '');
    const pm = path.match(/^\\/people\\/[^/]+\\/(\\d+)$/i);
    if (pm && pm[1].length >= 6) return 'https://www.facebook.com/profile.php?id=' + pm[1];
    const gum = path.match(/^\\/groups\\/[^/]+\\/user\\/(\\d+)$/i);
    if (gum && gum[1].length >= 6) return 'https://www.facebook.com/profile.php?id=' + gum[1];
    const m = path.match(/^\\/([^\\/]+)$/);
    if (!m) return null;
    const seg = decodeURIComponent(m[1] || '');
    if (!seg) return null;
    return /^\\d+$/.test(seg) ? (seg.length >= 6 ? 'https://www.facebook.com/profile.php?id=' + seg : null) : ('https://www.facebook.com/' + seg);
  };
  const countProfileLinks = (scope) => {
    const seen = new Set();
    scope.querySelectorAll('a[href]').forEach((a) => {
      const href = cleanUrl(a.getAttribute('href'));
      if (href) seen.add(href);
    });
    return seen.size;
  };
  let best = root;
  let bestScore = countProfileLinks(root) * 10;
  root.querySelectorAll('section, div, ul').forEach((el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 220 || rect.height < 80) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    const n = countProfileLinks(el);
    if (n < 4) return;
    const areaPenalty = Math.max(1, Math.min(rect.width, window.innerWidth) * Math.max(80, Math.min(rect.height, window.innerHeight * 2))) / 35000;
    const score = n * 10 - areaPenalty;
    if (score > bestScore) {
      bestScore = score;
      best = el;
    }
  });
  const rect = best.getBoundingClientRect();
  let scrollEl = null;
  let scrollSpan = 0;
  const candidates = [best, ...best.querySelectorAll('div, section, ul'), root];
  for (const el of candidates) {
    if (!el) continue;
    const sh = Number(el.scrollHeight || 0);
    const ch = Number(el.clientHeight || 0);
    if (sh <= ch + 40) continue;
    const st = getComputedStyle(el).overflowY;
    if (st !== 'auto' && st !== 'scroll' && st !== 'overlay') continue;
    const span = sh - ch;
    if (span > scrollSpan) {
      scrollSpan = span;
      scrollEl = el;
    }
  }
  const atBottom = scrollEl
    ? (scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 12)
    : (rect.bottom <= window.innerHeight + 28);
  return {
    atBottom,
    containerTop: Math.round(rect.top),
    containerBottom: Math.round(rect.bottom),
    linkCount: countProfileLinks(best),
    hasScrollableContainer: !!scrollEl,
  };
}
"""


_RESTRICTED_NAME_RES = (
    re.compile(r"^facebook\s+user$", re.I),
    re.compile(r"^utilisateur\s+facebook$", re.I),
    re.compile(r"^utilisateur\s+de\s+facebook$", re.I),
    re.compile(r"^usuario\s+de\s+facebook$", re.I),
    re.compile(r"^benutzer[\s-]*facebook$", re.I),
    re.compile(r"пользователь\s+facebook", re.I),
    re.compile(r"пользователь\s+фейсбук", re.I),
    re.compile(r"facebook[-\s]?nutzer", re.I),
)


def looks_like_fb_restricted_display_name(name: str) -> bool:
    """Подпись в списке друзей для скрытого/ограниченного профиля (эвристика)."""
    x = (name or "").strip()
    if len(x) < 2 or len(x) > 120:
        return False
    return any(p.search(x) for p in _RESTRICTED_NAME_RES)


def merge_friend_scan_rows(
    merged: dict[str, str],
    rows: list,
    *,
    restricted: dict[str, bool] | None = None,
) -> None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        href = (row.get("url") or "").strip()
        name = (row.get("name") or "").strip()
        if not href or not name:
            continue
        r = bool(row.get("restricted")) or looks_like_fb_restricted_display_name(name)
        if href not in merged or len(name) > len(merged[href]):
            merged[href] = name
            if restricted is not None:
                restricted[href] = r
        elif restricted is not None:
            restricted[href] = bool(restricted.get(href, False) or r)


def _fb_path_tab_is_connection_list(seg: str) -> bool:
    """Второй сегмент пути /username/… — вкладка со списком людей (друзья, mutual, подписчики…)."""
    low = seg.lower()
    if low in ("followers", "following", "friendlist"):
        return True
    if low == "friends":
        return True
    if low.startswith("friends_"):
        return True
    return False


def _facebook_path_points_at_connection_list(path: str) -> bool:
    parts = [x for x in path.strip("/").split("/") if x]
    if len(parts) < 2:
        return False
    return _fb_path_tab_is_connection_list(parts[1])


def _fb_path_segment_skip_for_owner_slug(seg: str) -> bool:
    low = seg.lower()
    fixed = {
        "friends",
        "friendlist",
        "people",
        "sk",
        "about",
        "photos",
        "followers",
        "following",
    }
    if low in fixed:
        return True
    if low.startswith("friends_"):
        return True
    return False


def donor_friends_page_url(donor_url: str) -> str:
    """URL страницы списка людей: друзья, группа /members, followers, following."""
    s = donor_url.strip().split("#")[0]
    gm = normalize_facebook_group_members_url(s)
    if gm:
        return gm
    low = s.lower()
    if "sk=friends" in low:
        return s
    try:
        p = urlparse(s)
        if _facebook_path_points_at_connection_list(p.path):
            return s
    except Exception:
        pass
    if "/followers" in low or "/following" in low or "/friends" in low:
        return s
    if "profile.php" in low:
        if "sk=friends" in low:
            return s
        return f"{s}{'&' if '?' in s else '?'}sk=friends"
    return s.rstrip("/") + "/friends"


def _owner_slug_from_url(u: str) -> str:
    try:
        p = urlparse(u)
        q = parse_qs(p.query)
        if "profile.php" in p.path and q.get("id") and q["id"][0].isdigit():
            return f"id{q['id'][0]}"
        parts = [x for x in p.path.strip("/").split("/") if x]
        if len(parts) >= 2 and parts[0].lower() == "groups":
            gid = parts[1]
            if gid:
                return f"group_{gid}"
        for seg in parts:
            if _fb_path_segment_skip_for_owner_slug(seg):
                continue
            if seg.isdigit():
                return f"id{seg}"
            return seg
    except Exception:
        pass
    return "profile"


def _safe_filename_fragment(name: str, max_len: int = 90) -> str:
    if not name:
        return "profile"
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f\n\r\t]+', "", name)
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("._")
    if len(s) > max_len:
        s = s[:max_len].rstrip("._")
    return s or "profile"


def _split_given_family(full_name: str) -> tuple[str, str]:
    """Имя и фамилия: первое слово / остаток (как в типичных подписях FB)."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _autofit_sheet_columns(ws, *, link_col: int = 2, max_wide: float = 85.0) -> None:
    """Ширина колонок по длине текста, ссылки — до max_wide, чтобы URL не обрезались в Excel."""
    min_w = 10.0
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        maxlen = 0
        for row in range(1, ws.max_row + 1):
            cell = ws.cell(row=row, column=col)
            v = cell.value
            if v is None:
                continue
            s = str(v)
            if len(s) > maxlen:
                maxlen = len(s)
        if col == 1:
            w = min(14.0, max(7.0, maxlen * 1.05 + 2.0))
        elif col == link_col:
            w = min(max_wide, max(min_w, maxlen * 1.08 + 3.0))
        else:
            w = min(48.0, max(min_w, maxlen * 1.12 + 3.0))
        ws.column_dimensions[letter].width = w


def _build_friends_workbook(
    by_url: dict[str, str],
    *,
    crm_ids_by_canonical: dict[str, int] | None = None,
) -> Workbook:
    ordered = sorted(by_url.items(), key=lambda x: x[1].lower())
    wb = Workbook()
    ws = wb.active
    ws.title = "Друзья"
    bold = Font(bold=True)
    link_font = Font(color="0563C1", underline="single")
    ws.append(["№", "Ссылка", "Имя", "Фамилия", "ID"])
    for c in range(1, 6):
        ws.cell(1, c).font = bold
    for idx, (href, name) in enumerate(ordered, start=1):
        row = idx + 1
        given, family = _split_given_family(name)
        ws.cell(row, 1, value=idx)
        cell_link = ws.cell(row, 2, value=href)
        cell_link.hyperlink = href
        cell_link.font = link_font
        ws.cell(row, 3, value=given)
        ws.cell(row, 4, value=family)
        pid = None
        if crm_ids_by_canonical:
            canon = normalize_facebook_profile_url(href)
            if canon:
                pid = crm_ids_by_canonical.get(canon)
        ws.cell(row, 5, value=pid if pid is not None else "")
    _autofit_sheet_columns(ws, link_col=2)
    return wb


def _atomic_write_workbook(wb: Workbook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    wb.save(tmp)
    tmp.replace(path)


def flush_friends_workbook(
    path: Path,
    by_url: dict[str, str],
    *,
    crm_ids_by_canonical: dict[str, int] | None = None,
) -> None:
    if not by_url:
        return
    wb = _build_friends_workbook(by_url, crm_ids_by_canonical=crm_ids_by_canonical)
    _atomic_write_workbook(wb, path)


def friends_workbook_to_bytes(
    by_url: dict[str, str],
    *,
    crm_ids_by_canonical: dict[str, int] | None = None,
) -> bytes:
    wb = _build_friends_workbook(by_url, crm_ids_by_canonical=crm_ids_by_canonical)
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def parse_expected_friends_count(page: Page) -> int | None:
    """Ориентир по числу людей: друзья, подписчики, подписки, участники группы."""
    try:
        n = page.evaluate(PARSE_EXPECTED_FRIENDS_COUNT_JS)
        if isinstance(n, int) and n >= 50:
            return min(n, 500_000)
        if isinstance(n, float) and n >= 50:
            return min(int(n), 500_000)
    except Exception:
        logger.debug("parse_expected_friends_count failed", exc_info=True)
    return None


def click_connections_see_all(page: Page) -> bool:
    """Нажать «Посмотреть все / See all», если Facebook показывает урезанный блок списка."""
    try:
        raw = page.evaluate(CLICK_CONNECTIONS_SEE_ALL_JS)
        return bool(raw)
    except Exception:
        logger.debug("click_connections_see_all failed", exc_info=True)
    return False


def connections_list_state(page: Page) -> dict[str, int | bool]:
    try:
        raw = page.evaluate(CONNECTIONS_LIST_STATE_JS)
        if isinstance(raw, dict):
            return raw
    except Exception:
        logger.debug("connections_list_state failed", exc_info=True)
    return {"atBottom": False, "linkCount": 0, "hasScrollableContainer": False}


def _cancellable_wait_ms(
    page: Page,
    ms: int,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Ждать ms миллисекунд чанками по ~200мс с опросом cancelled().

    Возвращает True, если ожидание было прервано отменой — вызывающий
    код должен немедленно прекратить работу. Иначе False.
    Без этого хелпера wait_for_timeout удерживал поток до нескольких
    секунд и кнопка «Остановить» казалась «зависшей».
    """
    if ms <= 0:
        return bool(cancelled and cancelled())
    step = 200
    elapsed = 0
    while elapsed < ms:
        if cancelled and cancelled():
            return True
        chunk = min(step, ms - elapsed)
        try:
            page.wait_for_timeout(chunk)
        except Exception:
            return bool(cancelled and cancelled())
        elapsed += chunk
    return bool(cancelled and cancelled())


def scroll_friends_page(
    page: Page,
    *,
    live_path: Path | None = None,
    max_rounds: int = 1800,
    expected_total: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_round: Callable[[int, int], None] | None = None,
    initial_merged: dict[str, str] | None = None,
    initial_restricted: dict[str, bool] | None = None,
    on_merged_flush: Callable[[dict[str, str], dict[str, bool]], None] | None = None,
    on_expected_update: Callable[[int], None] | None = None,
) -> tuple[dict[str, str], dict[str, bool], dict[str, int | None]]:
    """
    Сливаем уникальные профили на каждом шаге (виртуальный список FB).

    initial_merged — предзаполнение (например, уже сохранённые в БД друзья донора).
    initial_restricted — флаги «закрытый профиль» для тех же URL.
    on_merged_flush — копии (merged, restricted) при тех же порогах, что и запись XLSX.

    Раньше остановка учитывала «стабильность» высоты document — у нового интерфейса
    список крутится внутри main, высота страницы почти не растёт → выход в ~350 человек.
    Теперь: только счётчик «нет новых профилей», при известном expected — дольше ждём;
    плюс прокрутка вложенных overflow-контейнеров и «толчки» End.

    Приоритет — полнота выборки (медленнее, дольше ждём подгрузку). Ограничения Meta
    (капча, скрытые профили, обрезанный список у части доноров) по-прежнему возможны.
    """
    merged: dict[str, str] = dict(initial_merged) if initial_merged else {}
    restricted: dict[str, bool] = dict(initial_restricted) if initial_restricted else {}
    # Множитель задержек: fast≈0.35 (1–2с/раунд, без длинных пауз),
    # normal=1.0 (2.2–5.2с/раунд), gentle=1.3. Читается из Setting /
    # FB_MASTER_PARSER_SPEED, чтобы клиент мог ускорить парсер, если
    # его прокси/аккаунт это выдерживают (диагностика: у клиентов на
    # M1/Windows парсер шёл ~5.5с/раунд, ~1.5ч на 700 раундов).
    try:
        from backend.database import SessionLocal
        from backend.services.parser_speed import (
            get_parser_scroll_speed,
            scroll_random_pause_prob,
            scroll_wait_multiplier,
        )

        _db_for_speed = SessionLocal()
        try:
            _speed_mode = get_parser_scroll_speed(_db_for_speed)
        finally:
            _db_for_speed.close()
        _wait_mult = scroll_wait_multiplier(_speed_mode)
        _rand_mult = scroll_random_pause_prob(_speed_mode)
    except Exception:
        _speed_mode = "normal"
        _wait_mult = 1.0
        _rand_mult = 1.0
    logger.info("friends scroll: speed_mode=%s (wait×%.2f, rand_prob×%.2f)", _speed_mode, _wait_mult, _rand_mult)

    # Стартовая пауза: дать FB дорисовать виртуализированный список
    # подписчиков/друзей/участников до первой экстракции. Для public-creator
    # профилей и групп с тысячами участников список грузится ~2-4 секунды,
    # и без этой паузы первые 10-20 раундов extractor возвращал 0 — как
    # было в 2.78. В 2.64 пауза была, и парсер «летал».
    try:
        page.wait_for_timeout(int(3000 * max(_wait_mult, 0.5)))
    except Exception:
        pass

    # Cancel watcher: page.evaluate (CDP-вызов синхронного API Playwright) блокирует
    # поток до возврата JS. Если страница виснет (виртуальный список группы 65k+),
    # cancel-флаг через _cancellable_wait_ms не доходит до пользователя — кнопка
    # «Остановить» вроде нажата, но парсер не реагирует. Решение: отдельный поток
    # каждые 250мс проверяет cancelled() и при срабатывании закрывает context —
    # любой висящий page.evaluate бросает 'Target closed', который ловит safety-net.
    import threading as _threading
    _watcher_stop = _threading.Event()

    def _cancel_watcher():
        while not _watcher_stop.is_set():
            try:
                if cancelled and cancelled():
                    # Тройной залп — закрываем сразу всё, что может блокировать
                    # Chromium: browser → context → page. browser.close()
                    # терминирует процесс Chromium мгновенно — пользователь видит
                    # как окно браузера исчезает в течение ~100мс после клика Stop.
                    try:
                        page.context.browser.close()
                    except Exception:
                        pass
                    try:
                        page.context.close()
                    except Exception:
                        pass
                    try:
                        page.close()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            _watcher_stop.wait(0.10)

    _watcher_thread = _threading.Thread(target=_cancel_watcher, daemon=True)
    if cancelled is not None:
        _watcher_thread.start()

    # Locks для async save: один XLSX-save и один DB-flush одновременно max.
    # Если уже идёт save — следующий tick пропускается (он попадёт в очередной).
    _save_lock = _threading.Lock()
    _db_save_lock = _threading.Lock()

    # last_total с 0: иначе при предзаполнении из БД первая итерация даёт «нет новых» и ранняя остановка.
    last_total = 0
    no_new_rounds = 0
    last_save_i = -1
    last_saved_total = 0
    recovery_shakes = 0
    bottom_idle_rounds = 0
    see_all_clicks = 0

    # Доля от числа на странице, ниже которой не сдаёмся при «тишине» виртуального списка.
    _TARGET_FRAC = 0.97
    # Сколько раундов подряд без прироста считаем паузой подгрузки (не концом списка).
    _IDLE_BELOW_TARGET = 340
    _IDLE_NEAR_OR_ABOVE_TARGET = 130
    _IDLE_NO_EXPECTED = 110
    _MIN_ROUNDS_BEFORE_IDLE_STOP = 140
    _MAX_SHAKES_BELOW = 36
    _MAX_SHAKES_FALLBACK = 18
    _BOTTOM_IDLE_BELOW = 24
    _BOTTOM_IDLE_FALLBACK = 14
    _SEE_ALL_MAX_CLICKS = 6

    target_floor: int | None = None
    if expected_total and expected_total > 0:
        target_floor = min(expected_total, max(1, int(expected_total * _TARGET_FRAC)))

    rounds_cap = max_rounds
    if expected_total and expected_total >= 800:
        rounds_cap = max(max_rounds, min(5000, 500 + expected_total // 3))
    elif expected_total and expected_total >= 300:
        rounds_cap = max(max_rounds, min(3200, 450 + expected_total // 4))

    # Раздельный счётчик для on_merged_flush (БД-merge): он сильно тяжелее
    # XLSX-flush, в логах 2.79 видели каждый save-tick = пауза 60с из-за
    # _filter_snapshot_by_language + _merge_friends_into_parser_batch +
    # commit (database-is-locked retry от 9 фоновых воркеров). XLSX-flush
    # быстрый — оставляем 3/50; БД-flush делаем редко (30/500), чтобы
    # цикл не блокировался каждые 3 раунда. Финальный on_merged_flush
    # вызывается после break, чтобы все собранные люди попали в CRM.
    last_db_flush_i = -1
    last_db_flushed_total = 0
    # Crash-safe finalize: safe_final_flush() вызывается ВСЕГДА —
    # и при нормальном выходе, и при крахе Chromium (Page closed,
    # browser closed, OOM, anti-bot kill). Гарантия: всё что собрали
    # в `merged` — попадает в XLSX И в БД до return/raise.
    def _safe_final_flush(reason: str = "normal") -> None:
        try:
            # Дать в-полёте async XLSX-save завершиться (≤5с)
            if _save_lock.acquire(timeout=5.0):
                _save_lock.release()
        except Exception:
            pass
        if not merged:
            return
        if live_path:
            try:
                flush_friends_workbook(live_path, merged)
                logger.info("friends scroll FINAL XLSX flushed: %d people, reason=%s", len(merged), reason)
            except Exception:
                logger.exception("FINAL XLSX flush failed (reason=%s)", reason)
        if on_merged_flush:
            try:
                on_merged_flush(dict(merged), dict(restricted))
                logger.info("friends scroll FINAL DB flushed: %d people, reason=%s", len(merged), reason)
            except Exception:
                logger.exception("FINAL DB flush failed (reason=%s)", reason)

    _crash_handled = False
    i = 0

    # Crash-safe wrapper: _safe_final_flush ВСЕГДА вызывается, даже если
    # exception вылетел из любой строки лупа (Chromium crash, OOM,
    # anti-bot kill, выкл. света). Финальный sync-flush сохраняет ВСЁ
    # что собрали в `merged` — гарантия "ни одного потерянного человека".
    _loop_completed = False
    _loop_exception: Exception | None = None
    try:
        for i in range(rounds_cap):
            if cancelled and cancelled():
                if merged:
                    if live_path:
                        flush_friends_workbook(live_path, merged)
                    if on_merged_flush:
                        on_merged_flush(dict(merged), dict(restricted))
                break
            # Детальные тайминг-логи: видно где именно тормозит каждый раунд.
            # Лог формата `extract=Xms merge=Yms total=Zms dom_rows=N merged=M`.
            # Без этих таймингов невозможно отличить «FB медленно отдаёт DOM» от
            # «наш save блокирует на 60с». Если total > 5000ms — печатаем WARN.
            import time as _t
            _round_t0 = _t.monotonic()
            # Safety-net: если page/context закрылись (Stop button / Chromium-crash),
            # выходим из цикла без exception.
            try:
                _t_extract0 = _t.monotonic()
                dismiss_facebook_dom_overlays(page)
                rows = page.evaluate(EXTRACT_FRIENDS_JS)
                _t_extract = int((_t.monotonic() - _t_extract0) * 1000)
            except Exception as e:
                msg = str(e).lower()
                if "closed" in msg or "target" in msg or "context" in msg:
                    logger.info("friends scroll: page closed (cancel/crash) at round %s, exiting", i)
                    break
                raise
            rows_n = len(rows) if isinstance(rows, list) else 0
            _t_merge0 = _t.monotonic()
            if isinstance(rows, list):
                merge_friend_scan_rows(merged, rows, restricted=restricted)
            _t_merge = int((_t.monotonic() - _t_merge0) * 1000)
            total = len(merged)
            if on_round and (i % 4 == 0 or i == 0):
                on_round(i, total)
            # Continuous re-fetch expected_total: parser_worker делает 5 retry'ев
            # ДО входа в этот цикл. Если там не подхватили — пробуем раз в
            # 30 раундов. Раньше было 0,1,3,5,8 + каждые 10 — слишком часто,
            # тормозило TURBO: parse_expected_friends_count перебирает огромный
            # DOM querySelectorAll('a, span, div, h1, h2, h3, strong').
            if expected_total is None and i > 0 and i % 30 == 0:
                try:
                    _maybe = parse_expected_friends_count(page)
                    if _maybe and _maybe > 0:
                        expected_total = _maybe
                        logger.info(
                            "friends scroll: expected_total ПОДХВАЧЕН на round %s = %s",
                            i, expected_total,
                        )
                        # Пересчитать target_floor на основе нового expected.
                        if expected_total > 0:
                            target_floor = min(expected_total, max(1, int(expected_total * _TARGET_FRAC)))
                        # Сразу обновить UI: пользователь видит «по счётчику ~N чел.»
                        if on_expected_update is not None:
                            try:
                                on_expected_update(int(expected_total))
                            except Exception:
                                pass
                except Exception:
                    pass

            # Лог каждый раунд для первых 20, потом каждые 4 — с таймингами.
            if i < 20 or i % 4 == 0:
                logger.info(
                    "friends scroll round %s: extract=%dms merge=%dms dom_rows=%s merged=%s expected=%s mode=%s",
                    i,
                    _t_extract,
                    _t_merge,
                    rows_n,
                    total,
                    expected_total or "—",
                    _speed_mode,
                )

            if total > 0:
                # XLSX-save (async через thread): каждые 2 раунда / 25 новых.
                # Уменьшено с 3/50 — пользователь жаловался что при Stop теряются
                # уже собранные люди. Теперь max потери ≈25 человек (между
                # последним save'ом и Stop). XLSX async-thread не блокирует scroll.
                need_xlsx_save = (
                    i == 0
                    or (i - last_save_i) >= 2
                    or (total - last_saved_total) >= 25
                )
                # Async save: XLSX и БД-flush идут в отдельном daemon-thread,
                # scroll-цикл не блокируется. Это убирает «рывки» —
                # пользователь видел паузы 30-60с каждые 3 раунда из-за sync save.
                # _save_lock не даёт двум save'ам идти одновременно (defer next).
                if need_xlsx_save and live_path:
                    _xlsx_snap = dict(merged)
                    _xlsx_path = live_path
                    def _async_xlsx(snap=_xlsx_snap, p=_xlsx_path, round_i=i):
                        if not _save_lock.acquire(blocking=False):
                            return  # уже идёт save — пропускаем, будет следующий tick
                        try:
                            _t0 = _t.monotonic()
                            flush_friends_workbook(p, snap)
                            _ms = int((_t.monotonic() - _t0) * 1000)
                            if _ms > 1500:
                                logger.warning("friends scroll async XLSX flush медленный = %dms (round=%s, total=%s)", _ms, round_i, len(snap))
                            elif round_i < 20:
                                logger.info("friends scroll async xlsx=%dms (round=%s)", _ms, round_i)
                        except Exception:
                            logger.debug("async XLSX flush failed", exc_info=True)
                        finally:
                            _save_lock.release()
                    _threading.Thread(target=_async_xlsx, daemon=True).start()
                    last_save_i = i
                    last_saved_total = total
                # БД-flush: SYNC, threshold 10/100 (вернул из 2.82 для скорости).
                # 2.83 был 2/25 — слишком часто, тормозило TURBO-режим.
                # Crash safety обеспечивает _safe_final_flush() в try/finally
                # ниже: при ЛЮБОМ exception все собранные люди попадают в БД.
                # Поэтому промежуточные flushes можно делать редко.
                need_db_flush = on_merged_flush is not None and (
                    i == 0
                    or (i - last_db_flush_i) >= 10
                    or (total - last_db_flushed_total) >= 100
                )
                if need_db_flush:
                    _t0 = _t.monotonic()
                    try:
                        on_merged_flush(dict(merged), dict(restricted))
                        _ms = int((_t.monotonic() - _t0) * 1000)
                        if _ms > 5000:
                            logger.warning("friends scroll sync DB flush ОЧЕНЬ медленный = %dms (round=%s, total=%s)", _ms, i, total)
                        elif i < 20:
                            logger.info("friends scroll sync db=%dms (round=%s, total=%s)", _ms, i, total)
                    except Exception:
                        logger.exception("sync DB flush failed at round %s", i)
                    last_db_flush_i = i
                    last_db_flushed_total = total

            # Финальный тайминг раунда — если общий round > 5с, явно выделяем WARN.
            _round_total = int((_t.monotonic() - _round_t0) * 1000)
            if _round_total > 5000:
                logger.warning(
                    "friends scroll round %s МЕДЛЕННЫЙ: total=%dms (extract=%dms, merge=%dms) — что-то блокирует",
                    i, _round_total, _t_extract, _t_merge,
                )

            if total == last_total:
                no_new_rounds += 1
            else:
                no_new_rounds = 0
            last_total = total

            # Crash-safe: connections_list_state делает page.evaluate внутри.
            # Если Chromium закрылся — выходим и финальный flush сохранит данные.
            try:
                list_state = connections_list_state(page)
            except Exception as _e:
                _msg = str(_e).lower()
                if "closed" in _msg or "target" in _msg or "context" in _msg:
                    logger.warning("friends scroll: page closed at list_state round %s, salvaging %d", i, len(merged))
                    _crash_handled = True
                    break
                raise
            at_bottom = bool(list_state.get("atBottom"))
            if at_bottom and no_new_rounds > 0:
                bottom_idle_rounds += 1
            else:
                bottom_idle_rounds = 0

            below_target = target_floor is not None and total < target_floor
            at_or_past_target = target_floor is not None and total >= target_floor
            if below_target:
                idle_need = _IDLE_BELOW_TARGET
            elif at_or_past_target:
                idle_need = _IDLE_NEAR_OR_ABOVE_TARGET
            else:
                idle_need = _IDLE_NO_EXPECTED

            max_shakes = _MAX_SHAKES_BELOW if below_target else _MAX_SHAKES_FALLBACK

            should_try_see_all = (
                see_all_clicks < _SEE_ALL_MAX_CLICKS
                and (no_new_rounds >= 8 or bottom_idle_rounds >= 6)
            )
            if should_try_see_all and click_connections_see_all(page):
                see_all_clicks += 1
                logger.info(
                    "friends scroll: клик по 'Посмотреть все' %s/%s (собрано %s, ссылок в списке %s)",
                    see_all_clicks,
                    _SEE_ALL_MAX_CLICKS,
                    total,
                    int(list_state.get("linkCount") or 0),
                )
                if _cancellable_wait_ms(page, int(random.uniform(3200, 7600) * _wait_mult), cancelled):
                    break
                no_new_rounds = 0
                bottom_idle_rounds = 0
                recovery_shakes = 0
                continue

            bottom_idle_need = _BOTTOM_IDLE_BELOW if below_target else _BOTTOM_IDLE_FALLBACK
            if (
                i >= _MIN_ROUNDS_BEFORE_IDLE_STOP
                and at_bottom
                and bottom_idle_rounds >= bottom_idle_need
            ):
                logger.info(
                    "friends scroll: достигнут низ списка без новых людей %s раундов подряд (собрано %s, ожидалось %s)",
                    bottom_idle_rounds,
                    total,
                    expected_total or "—",
                )
                if merged:
                    if live_path:
                        flush_friends_workbook(live_path, merged)
                    if on_merged_flush:
                        on_merged_flush(dict(merged), dict(restricted))
                break

            if i >= _MIN_ROUNDS_BEFORE_IDLE_STOP and no_new_rounds >= idle_need:
                can_shake = recovery_shakes < max_shakes and (
                    below_target
                    or (expected_total is None and total > 0)
                )
                if can_shake:
                    recovery_shakes += 1
                    logger.info(
                        "friends scroll: толчок %s/%s (собрано %s, ожидалось на странице %s)",
                        recovery_shakes,
                        max_shakes,
                        total,
                        expected_total or "—",
                    )
                    _shake_cancelled = False
                    try:
                        # Дольше ждём после «толчка», чтобы виртуальный список FB успел дорендерить.
                        # На turbo множитель сокращает паузы пропорционально, чтобы recovery
                        # не съедал скорость в режиме «как в v1».
                        page.keyboard.press("End")
                        if _cancellable_wait_ms(page, int(random.uniform(3200, 5600) * _wait_mult), cancelled):
                            _shake_cancelled = True
                        else:
                            page.evaluate(SCROLL_TO_END_ENHANCED_JS)
                            if _cancellable_wait_ms(page, int(random.uniform(4800, 9000) * _wait_mult), cancelled):
                                _shake_cancelled = True
                            else:
                                page.keyboard.press("PageDown")
                                if _cancellable_wait_ms(page, int(random.uniform(1800, 3600) * _wait_mult), cancelled):
                                    _shake_cancelled = True
                    except Exception:
                        logger.debug("recovery scroll", exc_info=True)
                    if _shake_cancelled:
                        break
                    no_new_rounds = 0
                else:
                    if merged:
                        if live_path:
                            flush_friends_workbook(live_path, merged)
                        if on_merged_flush:
                            on_merged_flush(dict(merged), dict(restricted))
                    break

            # Ниже целевого числа — мягче крутим и дольше ждём между шагами.
            quality_slow = below_target or (expected_total is None and total > 300)

            # Crash-safe: scroll-вызовы могут упасть при крахе Chromium
            # (anti-bot kill, OOM, отвал интернета). Раньше exception летел
            # мимо финального flush — теряли всё что собрали (101 чел!).
            try:
                vh = int(page.evaluate("() => window.innerHeight"))
                end_every = 4 if below_target else 5
                if i % end_every == 0:
                    page.evaluate(SCROLL_TO_END_ENHANCED_JS)
                    lo, hi = (3400, 6800) if quality_slow else (2600, 5200)
                    if _cancellable_wait_ms(page, int(random.uniform(lo, hi) * _wait_mult), cancelled):
                        break
                else:
                    step = max(180, int(vh * random.uniform(0.22, 0.52)))
                    page.evaluate(SCROLL_FRIENDS_ENHANCED_JS, step)
                lo, hi = (3000, 6200) if quality_slow else (2200, 4800)
                if _cancellable_wait_ms(page, int(random.uniform(lo, hi) * _wait_mult), cancelled):
                    break
            except Exception as _e:
                _msg = str(_e).lower()
                if "closed" in _msg or "target" in _msg or "context" in _msg:
                    logger.warning("friends scroll: page closed at scroll-eval round %s, salvaging %d people", i, len(merged))
                    _crash_handled = True
                    break
                raise

            if random.random() < (0.22 if quality_slow else 0.16) * _rand_mult:
                if _cancellable_wait_ms(page, int(random.uniform(4200, 11000) * _wait_mult), cancelled):
                    break
            if random.random() < 0.12 * _rand_mult:
                if _cancellable_wait_ms(page, int(random.uniform(800, 2800) * _wait_mult), cancelled):
                    break

        _loop_completed = True
    except Exception as _outer_e:
        _loop_exception = _outer_e
        _msg = str(_outer_e).lower()
        if "closed" in _msg or "target" in _msg or "context" in _msg:
            logger.warning("friends scroll: outer Chromium crash at round %s, salvaging %d people", i, len(merged))
            _crash_handled = True
        else:
            logger.exception("friends scroll: UNEXPECTED outer exception at round %s", i)
            _crash_handled = True
    finally:
        _watcher_stop.set()
        _safe_final_flush(reason="crash" if _crash_handled else "normal")
        if _loop_exception is not None and not _crash_handled:
            # Незнакомое исключение — пробрасываем дальше после flush
            raise _loop_exception
    meta: dict[str, int | None] = {
        "rounds": min(rounds_cap, i + 1),
        "expected": expected_total,
    }
    return merged, restricted, meta


def owner_display_name(page: Page) -> str:
    try:
        raw = page.evaluate(OWNER_PAGE_NAME_JS)
        return (raw or "").strip() if isinstance(raw, str) else ""
    except Exception:
        return ""


def live_xlsx_path_for_donor(base_dir: Path, donor_url: str, owner_from_page: str) -> Path:
    slug = _safe_filename_fragment(
        owner_from_page.strip() or _owner_slug_from_url(donor_url)
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"Friends_{slug}.xlsx"
