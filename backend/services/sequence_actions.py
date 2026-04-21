"""Выполнение одного шага сценария по дням (лайк, комментарий, друзья, ЛС) на профиле Facebook."""

from __future__ import annotations

import logging
import os
import random
import re
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page
from sqlalchemy.orm import Session

from backend.models import Template
from backend.services.message_template_render import (
    apply_template_placeholders,
    resolve_template_variant,
)
from backend.services.ai_agent_llm import LLMError
from backend.services.sequence_ai_comment import (
    extract_timeline_post_text,
    generate_dm_message_sync,
    generate_profile_post_comment_sync,
    infer_dm_output_locale_for_ai_dm,
)
from backend.services.playwright_humanize import (
    humanize_between_comment_and_friend,
    humanize_between_friend_and_dm,
    humanize_between_like_and_comment,
    humanize_for_sequence_action,
    micro_reading_pause,
    soft_scroll_feed,
)
from backend.services.throttle import is_valid_throttle_preset
from backend.services.messenger_scrape import (
    messenger_pin_restore_dialog_open,
    try_resolve_messenger_e2ee_thread_blockers,
    verify_outgoing_message_appeared_in_thread,
)
from backend.services.warmup_actions import (
    profile_url_from_person,
    try_comment_on_timeline,
    try_like_posts_on_profile,
)

logger = logging.getLogger(__name__)


def _trim_debug_text(raw: Any, *, limit: int = 2000) -> str:
    text = str(raw or "").strip()
    if len(text) <= limit:
        return text
    extra = len(text) - limit
    return text[:limit] + f"... [truncated {extra} chars]"


def _debug_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _trim_debug_text(value)
    if isinstance(value, dict):
        return {str(k): _debug_safe(v) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_debug_safe(v) for v in list(value)[:40]]
    return _trim_debug_text(value, limit=800)


def _step_debug(action_type: str, profile_url: str, step_config: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    debug: dict[str, Any] = {
        "action_type": action_type,
        "profile_url": profile_url,
        "step_config": _debug_safe(step_config if isinstance(step_config, dict) else {}),
    }
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        debug[key] = _debug_safe(value)
    return debug


def _text_from_config(
    db: Session,
    cfg: dict[str, Any] | None,
    *,
    person_display: str,
    person_first: str,
) -> str:
    c = cfg if isinstance(cfg, dict) else {}
    raw = ""
    template_id = c.get("template_id")
    if str(template_id or "").strip().isdigit():
        row = db.get(Template, int(template_id))
        if row:
            raw = resolve_template_variant(row.body or "", db=db, template_row=row).strip()
    if not raw:
        raw = (c.get("message") or c.get("comment_template") or c.get("text") or "").strip()
        raw = resolve_template_variant(raw, db=None, template_row=None)
    disp = person_display or "друг"
    return apply_template_placeholders(
        raw,
        full_name=disp,
        first_name=person_first or disp,
    )


def _reference_text_for_ai_dm(
    db: Session,
    cfg: dict[str, Any] | None,
    *,
    person_display: str,
    person_first: str,
) -> str:
    """Опорный текст для ИИ-ЛС: поле «пример» → шаблон → текст шага; плейсхолдеры под человека."""
    c = cfg if isinstance(cfg, dict) else {}
    disp = person_display or "друг"
    first = person_first or disp
    separate_dm = bool(c.get("dm_separate"))
    raw_ex = (c.get("ai_dm_example") or "").strip()
    if raw_ex:
        resolved = resolve_template_variant(raw_ex, db=None, template_row=None)
        return apply_template_placeholders(resolved, full_name=disp, first_name=first)
    template_id = c.get("dm_template_id")
    if template_id is None and not separate_dm:
        template_id = c.get("template_id")
    if str(template_id or "").strip().isdigit():
        row = db.get(Template, int(template_id))
        if row:
            raw = resolve_template_variant(row.body or "", db=db, template_row=row).strip()
            return apply_template_placeholders(raw, full_name=disp, first_name=first)
    raw_msg = (c.get("dm_message") or "").strip()
    if not raw_msg and not separate_dm:
        raw_msg = (c.get("message") or "").strip()
    if raw_msg:
        resolved = resolve_template_variant(raw_msg, db=None, template_row=None)
        return apply_template_placeholders(resolved, full_name=disp, first_name=first)
    return ""


def try_friend_request_on_profile(page: Page, *, timeout_ms: int = 60_000) -> tuple[bool, str]:
    """Кнопка «Добавить в друзья» / Add friend на открытом профиле."""
    page.wait_for_timeout(1000)
    result = page.evaluate(
        """() => {
          const labels = [
            'Add friend', 'Добавить в друзья', 'Add Friend', 'Befreunden',
            'Ajouter', 'Aggiungi agli amici'
          ];
          for (const lbl of labels) {
            const el = document.querySelector('[aria-label="' + lbl + '"]');
            if (el) { el.click(); return { ok: true, how: lbl }; }
          }
          const btns = document.querySelectorAll('[role="button"], a[role="link"]');
          for (const b of btns) {
            const t = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase();
            if (t.includes('add friend') || t.includes('добавить в друз') || t.includes('заявк')) {
              b.click();
              return { ok: true, how: 'scan' };
            }
          }
          return { ok: false, how: 'not_found' };
        }"""
    )
    if isinstance(result, dict) and result.get("ok"):
        page.wait_for_timeout(2000)
        return True, str(result.get("how", "ok"))
    return False, "friend_btn_not_found"


_DM_COMPOSER_PICK_JS = """(path) => {
  const onMessages =
    typeof path === 'string' &&
    (path.includes('/messages') || path.includes('/messenger'));
  const onMessagesNew =
    typeof path === 'string' && path.includes('/messages/new');
  const minScore = onMessages ? 1 : 5;
  function inArticle(el) {
    return !!el.closest('[role="article"]');
  }
  function placeholderText(el) {
    return (
      (el.getAttribute('aria-placeholder') || '') +
      ' ' +
      (el.getAttribute('placeholder') || '') +
      ' ' +
      (el.getAttribute('data-placeholder') || '') +
      ' ' +
      (el.getAttribute('aria-label') || '')
    ).toLowerCase();
  }
  function isCommentLike(el) {
    const ph = placeholderText(el);
    if (ph.includes('коммент') || ph.includes('comment')) return true;
    const aid = (el.getAttribute('aria-describedby') || '');
    const by = aid ? document.getElementById(aid) : null;
    const hint = ((by && by.innerText) || '').toLowerCase();
    if (hint.includes('коммент') || hint.includes('comment')) return true;
    const form = el.closest('form');
    if (form && (form.getAttribute('action') || '').includes('comment')) return true;
    return false;
  }
  function isRecipientField(el) {
    const ph = placeholderText(el);
    const r0 = el.getBoundingClientRect();
    const mid0 = (r0.top + r0.bottom) / 2;
    const msgLike0 =
      ph.includes('сообщ') ||
      ph.includes('message') ||
      ph.includes('aa') ||
      ph.includes('напиш') ||
      ph.includes('type') ||
      ph.includes('write');
    if (onMessagesNew && mid0 < window.innerHeight * 0.48 && !msgLike0) {
      return true;
    }
    if (ph.includes('кому') || ph.includes('to:') || ph.includes('to :')
        || ph.includes('recipient') || ph.includes('search') || ph.includes('поиск')
        || ph.includes('получатель') || ph.includes('адресат')
        || ph.includes('people') || ph.includes('name or group')
        || ph.includes('имя или груп')) return true;
    const closestCombobox = el.closest('[role="combobox"], [role="search"]');
    if (closestCombobox) return true;
    let p = el.parentElement;
    for (let i = 0; i < 12 && p; i++) {
      const lbl = (p.innerText || '').trim();
      if (/^(Кому|To|An|À)\\s*:/i.test(lbl)) {
        const r = p.getBoundingClientRect();
        if (r.height < 300) return true;
      }
      const role = (p.getAttribute('role') || '').toLowerCase();
      if (role === 'combobox') return true;
      if (p.querySelector && p.querySelector('[role="listbox"], [role="option"]')) {
        const r = p.getBoundingClientRect();
        if (r.height < 300 && r.top < 300) return true;
      }
      p = p.parentElement;
    }
    const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
    if (ariaLabel.includes('кому') || ariaLabel === 'to' || ariaLabel.startsWith('to ')
        || ariaLabel.includes('recipient')
        || ariaLabel.includes('получатель') || ariaLabel.includes('адресат')) return true;
    if (onMessages) {
      const r = el.getBoundingClientRect();
      const isMsgLike = ph.includes('сообщ') || ph.includes('message')
        || ph.includes('aa') || ph.includes('напиш') || ph.includes('type')
        || ph.includes('write');
      if (r.top < 200 && !isMsgLike) return true;
    }
    return false;
  }
  const nodes = Array.from(
    document.querySelectorAll(
      '[contenteditable="true"], textarea, div[role="textbox"], ' +
      '[data-lexical-editor="true"], [aria-label*="message" i], [aria-label*="сообщ"]'
    )
  );
  const seen = new Set();
  const unique = [];
  for (const el of nodes) {
    if (seen.has(el)) continue;
    seen.add(el);
    unique.push(el);
  }
  const scored = [];
  for (const el of unique) {
    if (el.tagName === 'TEXTAREA') {
      const ph = placeholderText(el);
      if (
        !ph.includes('сообщ') &&
        !ph.includes('message') &&
        !ph.includes('write') &&
        !ph.includes('type') &&
        !ph.includes('напиш')
      ) {
        continue;
      }
    } else if (el.getAttribute('role') === 'textbox' && el.getAttribute('contenteditable') !== 'true') {
      continue;
    }
    if (inArticle(el)) continue;
    if (isCommentLike(el)) continue;
    if (isRecipientField(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 8) continue;
    if (onMessages) {
      if (r.bottom < -400 || r.top > window.innerHeight + 2000) continue;
    } else {
      if (r.bottom < -40 || r.top > window.innerHeight + 200) continue;
    }
    let score = 0;
    const ph = placeholderText(el);
    if (el.getAttribute('data-lexical-editor') === 'true') score += 22;
    if (el.closest('[role="dialog"]')) score += 18;
    if (onMessages && el.closest('[role="main"]')) score += 14;
    if (onMessages && el.closest('[data-pagelet*="MW"]')) score += 8;
    if (el.getAttribute('role') === 'textbox') score += 6;
    if (el.tagName === 'TEXTAREA') score += 12;
    if (ph.includes('сообщ') || ph.includes('message') || ph.includes('напишите') || ph.includes('type a message') || ph.includes('напиш')) score += 14;
    if (ph.includes('Aa') || ph === 'aa') score += 10;
    const midY = (r.top + r.bottom) / 2;
    if (midY > window.innerHeight * 0.38) score += 10;
    if (midY < 140) score -= 12;
    if (onMessagesNew && midY < window.innerHeight * 0.35) score -= 55;
    if (onMessagesNew) {
      const msgHint =
        ph.includes('сообщ') ||
        ph.includes('message') ||
        ph.includes('напишите') ||
        ph.includes('type a message') ||
        ph.includes('напиш') ||
        ph.includes('type') ||
        ph.includes('write') ||
        ph.includes('aa');
      if (midY < window.innerHeight * 0.46 && !msgHint) continue;
    }
    if (onMessages && midY < 200) {
      const isMsgLike = ph.includes('сообщ') || ph.includes('message')
        || ph.includes('aa') || ph.includes('напиш') || ph.includes('type')
        || ph.includes('write');
      if (!isMsgLike) score -= 35;
    }
    if (onMessages && el.closest('form')) score += 6;
    scored.push({ el, score });
  }
  scored.sort((a, b) => b.score - a.score);
  if (scored.length === 0) return { ok: false, reason: 'none', candidateCount: unique.length };
  const best = scored[0];
  if (best.score < minScore) return { ok: false, reason: 'low_confidence', bestScore: best.score };
  try {
    best.el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
  } catch (e) {}
  best.el.focus();
  best.el.click();
  let n = best.el;
  let dock = null;
  for (let i = 0; i < 45 && n; i++) {
    const st = window.getComputedStyle(n);
    if (st.position === 'fixed') {
      const rr = n.getBoundingClientRect();
      if (rr.width >= 200 && rr.width <= 520 && rr.height >= 80 && rr.height <= 900) {
        dock = n;
        break;
      }
    }
    n = n.parentElement;
  }
  if (!dock) {
    n = best.el;
    for (let i = 0; i < 45 && n; i++) {
      const st = window.getComputedStyle(n);
      if (st.position === 'fixed') {
        const rr = n.getBoundingClientRect();
        if (rr.width >= 180 && rr.height >= 80) {
          dock = n;
          break;
        }
      }
      n = n.parentElement;
    }
  }
  if (dock) {
    const r = dock.getBoundingClientRect();
    const margin = 22;
    const overflow = r.bottom - (window.innerHeight - margin);
    if (overflow > 3) {
      const add = Math.min(Math.ceil(overflow) + 10, 380);
      if (!dock.dataset.fbmOrigBottom) {
        dock.dataset.fbmOrigBottom = window.getComputedStyle(dock).bottom || '0px';
      }
      const ob = dock.dataset.fbmOrigBottom;
      let base = 0;
      const m = /^([-+]?\\d*\\.?\\d+)px$/.exec(ob || '');
      if (m) base = parseFloat(m[0]) || 0;
      dock.style.bottom = base + add + 'px';
      dock.dataset.fbmLifted = '1';
    }
  } else if (onMessages) {
    let wn = best.el;
    for (let i = 0; i < 50 && wn; i++) {
      const st = window.getComputedStyle(wn);
      if (st.position === 'fixed') {
        const rr = wn.getBoundingClientRect();
        if (rr.width >= 400 && rr.height >= 60 && rr.height <= 220 && rr.bottom > window.innerHeight - 8) {
          const margin = 22;
          const overflow = rr.bottom - (window.innerHeight - margin);
          if (overflow > 3) {
            const add = Math.min(Math.ceil(overflow) + 12, 420);
            if (!wn.dataset.fbmOrigBottom) {
              wn.dataset.fbmOrigBottom = window.getComputedStyle(wn).bottom || '0px';
            }
            const ob = wn.dataset.fbmOrigBottom;
            let base = 0;
            const m = /^([-+]?\\d*\\.?\\d+)px$/.exec(ob || '');
            if (m) base = parseFloat(m[0]) || 0;
            wn.style.bottom = base + add + 'px';
            wn.dataset.fbmLifted = '1';
          }
          break;
        }
      }
      wn = wn.parentElement;
    }
  }
  return { ok: true, score: best.score };
}"""


_DM_VERIFY_COMPOSER_CLEARED_JS = """(expectedNorm) => {
  function norm(s) {
    return (String(s || '').replace(/\\s+/g, ' ')).trim();
  }
  const w = norm(expectedNorm);
  if (!w.length) return { ok: true, reason: 'empty' };
  const path = (window.location.pathname || '').toLowerCase();
  const onNew = path.includes('/messages/new');
  function isRecipientLike(el) {
    if (el.closest('[role="combobox"], [role="search"]')) return true;
    const r = el.getBoundingClientRect();
    if (onNew && r.top < window.innerHeight * 0.38) {
      const ph = (
        (el.getAttribute('aria-placeholder') || '') + ' ' +
        (el.getAttribute('placeholder') || '') + ' ' +
        (el.getAttribute('aria-label') || '')
      ).toLowerCase();
      const msgLike =
        ph.includes('сообщ') || ph.includes('message') || ph.includes('aa') ||
        ph.includes('напиш') || ph.includes('type') || ph.includes('write');
      if (!msgLike) return true;
    }
    return false;
  }
  const nodes = document.querySelectorAll(
    '[data-lexical-editor="true"], [contenteditable="true"][role="textbox"], ' +
    'div[contenteditable="true"]'
  );
  for (const el of nodes) {
    if (isRecipientLike(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 8) continue;
    if (r.bottom < -40 || r.top > window.innerHeight + 120) continue;
    const mid = (r.top + r.bottom) / 2;
    if (mid < window.innerHeight * 0.30) continue;

    const t = norm(el.innerText || el.textContent || '');
    if (!t.length) continue;

    if (w.length < 12) {
      if (t === w) return { ok: false, sample: t.slice(0, 140) };
      if (w.length <= 2 && (t === w || (t.length <= 5 && t.startsWith(w)))) {
        return { ok: false, sample: t.slice(0, 140) };
      }
      if (w.length >= 3 && t.startsWith(w) && t.length <= w.length + 5) {
        return { ok: false, sample: t.slice(0, 140) };
      }
      if (w.length >= 4 && t.includes(w) && t.length <= w.length + 10) {
        return { ok: false, sample: t.slice(0, 140) };
      }
      continue;
    }

    if (t.length < Math.min(24, w.length * 0.45)) continue;
    if (t.length >= w.length * 0.72) {
      const n = Math.min(28, w.length, t.length);
      const a = w.slice(0, n);
      const b = t.slice(0, n);
      if (a === b || (a.length >= 12 && b.startsWith(a.slice(0, 12)))) {
        return { ok: false, sample: t.slice(0, 140) };
      }
    }
  }
  return { ok: true };
}"""


def _verify_dm_send_cleared(page: Page, sent_text: str) -> tuple[bool, str]:
    """
    После Enter: если в видимом композере **остался отправленный текст** — отправку не считаем успешной.

    Раньше короткие ЛС (<12 символов) не проверялись → ложные «Отправлено». Теперь проверяются те же правила
    в JS (см. _DM_VERIFY_COMPOSER_CLEARED_JS). Несколько подряд «ok» и более длинные паузы — меньше гонок
    с медленным UI Facebook (приоритет стабильности, не скорости).
    """
    w = " ".join((sent_text or "").split()).strip()
    if not w:
        return True, ""
    last: dict[str, Any] = {}
    short = len(w) < 12
    need_ok_streak = 3 if short else 3
    iterations = 28 if short else 22
    pause_ms = 480 if short else 520
    streak = 0
    for _ in range(iterations):
        page.wait_for_timeout(pause_ms)
        try:
            last = page.evaluate(_DM_VERIFY_COMPOSER_CLEARED_JS, w)
        except Exception:
            return False, "verify_eval_failed"
        if isinstance(last, dict) and last.get("ok"):
            streak += 1
            if streak >= need_ok_streak:
                # Повторная проверка через паузу: иногда композер «мигает» пустым до реальной отправки
                page.wait_for_timeout(1850)
                try:
                    last_post = page.evaluate(_DM_VERIFY_COMPOSER_CLEARED_JS, w)
                except Exception:
                    return False, "verify_eval_failed_post"
                if isinstance(last_post, dict) and last_post.get("ok"):
                    return True, ""
                streak = 0
        else:
            streak = 0
    sample = str((last or {}).get("sample") or "")[:100]
    return False, f"dm_send_unverified:composer_still_full:{sample}"


def dm_thread_url_from_canonical(raw_url: str) -> str | None:
    """Прямой URL треда Messenger (/messages/t/…), если его можно вывести из ссылки на профиль."""
    u = (raw_url or "").strip()
    if not u:
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "https://www.facebook.com/" + u.lstrip("/")
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower()
    if "facebook.com" not in host and "fb.com" not in host and "messenger.com" not in host:
        return None
    path = parsed.path or ""
    full_pq = f"{path}?{parsed.query or ''}"
    m_th = re.search(r"/messages/(?:e2ee/)?t/([^/?#]+)", full_pq, re.I)
    if m_th:
        seg = (m_th.group(1) or "").strip()
        if seg:
            return f"https://www.facebook.com/messages/t/{seg}"
    if "messenger.com" in host:
        m_m = re.search(r"/(?:e2ee/)?t/([^/?#]+)", path, re.I)
        if m_m:
            seg = (m_m.group(1) or "").strip()
            if seg:
                return f"https://www.facebook.com/messages/t/{seg}"
    skip_roots = {
        "groups",
        "pages",
        "events",
        "watch",
        "marketplace",
        "reel",
        "stories",
        "messages",
        "login",
        "reg",
    }
    if "profile.php" in path:
        uid = (parse_qs(parsed.query).get("id") or [None])[0]
        if uid and str(uid).isdigit():
            return f"https://www.facebook.com/messages/t/{uid}"
        return None
    segments = [x for x in path.strip("/").split("/") if x]
    if not segments:
        return None
    first = segments[0].lower()
    if first in skip_roots:
        return None
    thread = segments[0]
    if not str(thread).isdigit():
        # vanity URL → /messages/t/slug часто уводит на «Новое сообщение»; открываем через профиль
        return None
    return f"https://www.facebook.com/messages/t/{thread}"


def _numeric_messenger_thread_id_from_meta(meta: Any) -> str | None:
    """
    Только числовой id **в Facebook** (страница / профиль / тред Messenger) из people.raw_meta.

    Внутренний id контакта в БД приложения (Person.id, person_id и т.п.) сюда **никогда** не подставляем —
    только ключи с явным смыслом Meta (префиксы facebook_ / fb_ / messenger_).
    """
    if meta is None or not isinstance(meta, dict):
        return None
    keys = (
        "facebook_page_id",
        "facebook_user_id",
        "facebook_profile_id",
        "facebook_id",
        "fb_page_id",
        "fb_user_id",
        "fb_id",
        "fb_numeric_id",
        "messenger_thread_id",
    )
    for k in keys:
        v = meta.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s.isdigit():
            continue
        if len(s) < 8:
            continue
        return s
    return None


def dm_direct_thread_url_for_person(
    canonical_url: str | None,
    raw_meta: Mapping[str, Any] | None = None,
) -> str | None:
    """URL `facebook.com/messages/t/<id>`: из профиля, из ссылки-треда или из raw_meta числового id."""
    u = dm_thread_url_from_canonical((canonical_url or "").strip())
    if u:
        return u
    mid = _numeric_messenger_thread_id_from_meta(raw_meta)
    if mid:
        return f"https://www.facebook.com/messages/t/{mid}"
    return None


def _messages_new_search_term_from_canonical_and_meta(
    url: str,
    meta: Mapping[str, Any] | None,
    *,
    thread_url: str | None = None,
) -> str:
    """
    Строка для «Кому» на /messages/new — id **Facebook** получателя (не Person.id приложения).

    Порядок:
    1) Числовой id из canonical_url (в т.ч. profile.php?id=… — как на открытой странице профиля).
    2) Числовой id из уже известного thread_url (/messages/t/… после PIN/редиректа).
    3) Числовой id из people.raw_meta (только ключи facebook_* / fb_* / messenger_thread_id).
    4) Иначе vanity/текст из canonical_url.
    """
    from_canon = (_messages_new_search_term_from_canonical(url) or "").strip()
    if from_canon.isdigit():
        return from_canon
    from_thread = (_messages_new_search_term_from_thread_url(thread_url) or "").strip()
    if from_thread.isdigit():
        return from_thread
    mid = _numeric_messenger_thread_id_from_meta(meta)
    if mid:
        return mid
    return from_canon or from_thread


def _messages_new_search_term_from_canonical(url: str) -> str:
    """Строка поиска адресата на /messages/new (id или короткий vanity из URL профиля)."""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://www.facebook.com/" + u.lstrip("/")
    parsed = urlparse(u)
    if "profile.php" in (parsed.path or "").lower():
        ids = parse_qs(parsed.query or "").get("id") or []
        if ids and str(ids[0]).isdigit():
            return str(ids[0])
    segments = [x for x in (parsed.path or "").strip("/").split("/") if x]
    skip = {
        "www",
        "web",
        "m",
        "mbasic",
        "touch",
        "l",
        "people",
        "messages",
        "new",
        "compose",
        "inbox",
        "e2ee",
        "t",
        "sent",
        "archived",
        "requests",
        "marketplace",
    }
    for seg in reversed(segments):
        if seg.lower() in skip:
            continue
        if seg.isdigit() and len(seg) >= 8:
            return seg
        if re.match(r"^[A-Za-z0-9._-]{1,80}$", seg) and not seg.isdigit():
            return seg
    return ""


def _search_hint_from_peer_or_profile_url(url: str | None) -> str:
    if not (url or "").strip():
        return ""
    u = url.strip()
    m = re.search(r"/messages/t/([^/?#]+)", u, re.I)
    if m:
        part = m.group(1)
        if part.isdigit():
            return part
    return _messages_new_search_term_from_canonical(u)


def _messages_new_search_term_from_thread_url(thread_url: str | None) -> str:
    """Числовой id или vanity из уже открытого /messages/t/… — если canonical/meta пустые."""
    if not (thread_url or "").strip():
        return ""
    m = re.search(r"/messages/(?:e2ee/)?t/([^/?#]+)", (thread_url or "").strip(), re.I)
    if not m:
        return ""
    part = (m.group(1) or "").strip()
    low = part.lower()
    if not part or low in ("new", "compose", "inbox"):
        return ""
    return part


def _select_all_in_focused_field(page: Page) -> None:
    try:
        if sys.platform == "darwin":
            page.keyboard.press("Meta+A")
        else:
            page.keyboard.press("Control+A")
    except Exception:
        pass


def _try_click_messages_new_autocomplete_first_option(page: Page, search_term: str = "") -> bool:
    """
    После ввода ID в «Кому» Meta показывает список («Другие люди» и строка с именем/аватаром).

    Кликаем по строке контакта (часто [role=option] с img), не по заголовку секции.
    Учитываем: несколько listbox (левый сайдбар), порталы без role=listbox, клик через elementFromPoint.
    """
    term = (search_term or "").strip().lower()

    def _playwright_click_main_autocomplete_row() -> bool:
        """Сначала main — не брать listbox из левой колонки чатов."""
        roots = (
            page.locator('[role="main"]'),
            page.locator('[data-pagelet*="MW"]'),
            page.locator("#mount_0_0"),
        )
        for root in roots:
            try:
                if root.count() == 0:
                    continue
                r0 = root.first
                with_img = r0.locator('[role="listbox"] [role="option"]').filter(has=page.locator("img"))
                n = min(with_img.count(), 8)
                for i in range(n):
                    el = with_img.nth(i)
                    try:
                        if el.is_visible(timeout=900):
                            el.scroll_into_view_if_needed(timeout=2000)
                            el.click(timeout=5000, force=True)
                            logger.info(
                                "autocomplete: клик [role=option] с аватаром (root=%s i=%s)",
                                "main" if root == roots[0] else "pagelet",
                                i,
                            )
                            return True
                    except Exception:
                        continue
                opts = r0.locator('[role="option"]')
                cnt = min(opts.count(), 12)
                for i in range(cnt):
                    el = opts.nth(i)
                    try:
                        if not el.is_visible(timeout=700):
                            continue
                        tx = (el.inner_text() or "").replace("\n", " ").strip()
                        low = tx.lower()
                        if not tx or len(tx) < 2:
                            continue
                        if re.match(
                            r"^(другие люди|other people|ваши контакты|your contacts|люди|people)\s*$",
                            low,
                            re.I,
                        ):
                            continue
                        el.scroll_into_view_if_needed(timeout=2000)
                        el.click(timeout=5000, force=True)
                        logger.info("autocomplete: клик [role=option] по тексту (i=%s)", i)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    if _playwright_click_main_autocomplete_row():
        return True

    try:
        opts = page.get_by_role("option")
        n = min(opts.count(), 20)
        for i in range(n):
            el = opts.nth(i)
            try:
                if not el.is_visible(timeout=600):
                    continue
                bb = el.bounding_box()
                if not bb or bb["x"] < 180:
                    continue
                tx = (el.inner_text() or "").strip()
                if len(tx) < 2:
                    continue
                if re.match(
                    r"^(другие люди|other people|ваши контакты|your contacts)$",
                    tx.lower(),
                    re.I,
                ):
                    continue
                el.click(timeout=5000, force=True)
                logger.info("autocomplete: клик get_by_role(option) глобально i=%s", i)
                return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        if bool(
            page.evaluate(
                """() => {
          const W = window.innerWidth;
          const H = window.innerHeight;
          const sidebarCut = Math.min(320, W * 0.28);
          function isSectionHeader(t) {
            const s = (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            return /^(другие люди|other people|ваши контакты|your contacts|люди|people)$/.test(s);
          }
          function clickAt(el) {
            try {
              el.scrollIntoView({ block: 'nearest', behavior: 'instant' });
              const r = el.getBoundingClientRect();
              if (r.width < 8 || r.height < 8) return false;
              const cx = r.left + Math.min(r.width * 0.5, 120);
              const cy = r.top + r.height * 0.5;
              let hit = document.elementFromPoint(cx, cy);
              if (!hit || !el.contains(hit)) hit = el;
              hit.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
              hit.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
              hit.click();
              return true;
            } catch (e) {
              return false;
            }
          }
          const tried = new Set();
          const containers = document.querySelectorAll(
            '[role="listbox"], [role="list"], [role="menu"], [role="grid"]'
          );
          for (const lb of containers) {
            const br = lb.getBoundingClientRect();
            if (br.left < sidebarCut && br.width < W * 0.42) continue;
            if (br.bottom < 72 || br.top > H * 0.92) continue;
            const opts = lb.querySelectorAll('[role="option"], [role="menuitem"], [role="row"]');
            const rows = [];
            for (const o of opts) {
              const r = o.getBoundingClientRect();
              if (r.width < 40 || r.height < 18) continue;
              if (r.bottom < 60 || r.top > H - 10) continue;
              const txt = (o.innerText || o.textContent || '').replace(/\\s+/g, ' ').trim();
              if (!txt || txt.length < 2) continue;
              if (isSectionHeader(txt)) continue;
              const hasImg = !!o.querySelector('img,svg image');
              let sc = hasImg ? 500 : 0;
              if (r.left >= sidebarCut) sc += 200;
              if (r.top > 100 && r.top < H * 0.62) sc += 120;
              rows.push({ o, sc, hasImg });
            }
            rows.sort((a, b) => b.sc - a.sc);
            for (const { o } of rows) {
              if (tried.has(o)) continue;
              tried.add(o);
              if (clickAt(o)) return true;
            }
          }
          const loose = document.querySelectorAll(
            '[role="dialog"] [role="option"], [role="presentation"] li, ' +
            'div[role="listbox"] div[role="option"]'
          );
          for (const o of loose) {
            const r = o.getBoundingClientRect();
            if (r.left < sidebarCut || r.width < 80 || r.height < 22) continue;
            if (r.top < 70 || r.bottom > H - 6) continue;
            const txt = (o.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!txt || isSectionHeader(txt)) continue;
            if (!o.querySelector('img')) continue;
            if (tried.has(o)) continue;
            tried.add(o);
            if (clickAt(o)) return true;
          }
          return false;
        }"""
            )
        ):
            return True
    except Exception:
        logger.debug("_try_click_messages_new_autocomplete_first_option (evaluate)", exc_info=True)
    try:
        lb = page.locator('[role="main"] [role="listbox"]').first
        if lb.count() == 0:
            lb = page.locator('[role="listbox"]').first
        if lb.count() == 0:
            return False
        row = lb.locator('[role="option"]').filter(has=page.locator("img")).first
        if row.count() == 0:
            row = lb.locator('[role="option"]').nth(1)
        if row.count() == 0:
            row = lb.locator('[role="option"]').first
        if row.count() == 0:
            return False
        row.wait_for(state="visible", timeout=2500)
        row.click(timeout=4000, force=True)
        return True
    except Exception:
        logger.debug("_try_click_messages_new_autocomplete_first_option (locator)", exc_info=True)
    try:
        if bool(
            page.evaluate(
                """(term) => {
          const active = document.activeElement;
          const activeRect = active && active.getBoundingClientRect ? active.getBoundingClientRect() : null;
          const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const overlapX = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left);
          const clickNode = (node) => {
            if (!node) return false;
            try {
              node.scrollIntoView({ block: 'nearest', behavior: 'instant' });
              node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
              node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
              node.click();
              return true;
            } catch (e) {
              return false;
            }
          };
          const nodes = Array.from(document.querySelectorAll(
            '[role="option"], [role="listbox"] [role="button"], [role="listbox"] [role="link"], ' +
            '[role="listbox"] [tabindex], [role="dialog"] [role="button"], [role="dialog"] [role="link"], ' +
            '[aria-selected="true"], [aria-selected="false"], li, div[tabindex], a[tabindex], ' +
            'div[role="button"], a[role="link"]'
          ));
          let best = null;
          let bestScore = -1e9;
          for (const el of nodes) {
            if (active && (el === active || active.contains(el))) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 120 || r.height < 24) continue;
            if (r.bottom < 48 || r.top > window.innerHeight * 0.9) continue;
            const txt = norm(el.innerText || el.textContent);
            if (!txt) continue;
            if (/^(кому|to|an|à)\\s*:?$/.test(txt)) continue;
            let score = 0;
            const role = norm(el.getAttribute('role'));
            if (role === 'option') score += 500;
            if (el.closest('[role="listbox"]')) score += 260;
            if (el.querySelector('img, image, svg')) score += 90;
            if (txt.includes('другие люди') || txt.includes('other people')) score -= 120;
            if (txt.length >= 2 && txt.length <= 180) score += 24;
            if (r.height >= 28 && r.height <= 120) score += 40;
            if (activeRect) {
              const ox = overlapX(r, activeRect);
              if (ox > 18) score += Math.min(220, ox);
              else score -= 260;
              if (r.top >= activeRect.bottom - 10 && r.top <= activeRect.bottom + 420) score += 180;
              else if (r.top < activeRect.top - 20) score -= 220;
            }
            if (term) {
              if (txt.includes(term)) score += 150;
              const parts = term.split(/\\s+/).filter(Boolean);
              let hits = 0;
              for (const p of parts) {
                if (p.length >= 2 && txt.includes(p)) hits += 1;
              }
              score += hits * 24;
            }
            if (score > bestScore) {
              bestScore = score;
              best = el;
            }
          }
          if (!best || bestScore < 140) return false;
          return clickNode(best);
        }""",
                term,
            )
        ):
            return True
    except Exception:
        logger.debug("_try_click_messages_new_autocomplete_first_option (popup scan)", exc_info=True)
    return False


def _focused_field_contains_typed_term(page: Page, term: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """(t) => {
          const el = document.activeElement;
          if (!el || !t) return false;
          const raw = (el.innerText || el.textContent || el.value || '').trim();
          if (raw.length < 1) return false;
          return raw.includes(t) || (raw.length >= 3 && t.startsWith(raw));
        }""",
                term,
            )
        )
    except Exception:
        return False


def _try_set_focused_recipient_text_via_dom(page: Page, term: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """(t) => {
          const el = document.activeElement;
          if (!el || !t) return false;
          const fire = (type, extra = {}) => {
            try {
              let ev = null;
              if (type === 'beforeinput' || type === 'input') {
                ev = new InputEvent(type, {
                  bubbles: true,
                  cancelable: true,
                  data: extra.data || null,
                  inputType: extra.inputType || 'insertText',
                });
              } else {
                ev = new Event(type, { bubbles: true, cancelable: true });
              }
              el.dispatchEvent(ev);
            } catch (_e) {}
          };
          try {
            el.focus();
          } catch (_e) {}
          if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
            el.value = '';
            fire('input', { inputType: 'deleteContentBackward', data: null });
            el.value = t;
            fire('input', { inputType: 'insertText', data: t });
            fire('change');
            return true;
          }
          const isCe = String(el.getAttribute('contenteditable') || '').toLowerCase() === 'true';
          if (!isCe) return false;
          try {
            el.textContent = '';
            const sel = window.getSelection();
            if (sel) {
              sel.removeAllRanges();
              const range = document.createRange();
              range.selectNodeContents(el);
              range.collapse(true);
              sel.addRange(range);
            }
          } catch (_e) {}
          fire('beforeinput', { inputType: 'deleteContentBackward', data: null });
          fire('input', { inputType: 'deleteContentBackward', data: null });
          let inserted = false;
          try {
            inserted = !!(document.execCommand && document.execCommand('insertText', false, t));
          } catch (_e) {}
          if (!inserted) {
            el.textContent = t;
          }
          try {
            const sel = window.getSelection();
            if (sel) {
              sel.removeAllRanges();
              const range = document.createRange();
              range.selectNodeContents(el);
              range.collapse(false);
              sel.addRange(range);
            }
          } catch (_e) {}
          fire('beforeinput', { inputType: 'insertText', data: t });
          fire('input', { inputType: 'insertText', data: t });
          fire('change');
          return true;
        }""",
                term,
            )
        )
    except Exception:
        logger.debug("_try_set_focused_recipient_text_via_dom failed", exc_info=True)
        return False


def _try_select_recipient_on_messages_new_page(page: Page, search_term: str, *, timeout_ms: int = 20000) -> bool:
    """На /messages/new: фокус в «Кому», ввод поиска, клик по подсказке (в т.ч. поиск страницы по числовому id).

    Если адресат уже выбран и композер доступен — сразу True без повторного ввода (избегает «второго круга»).
    После выбора закрывает выпадающий список (Escape), чтобы не блокировать поиск поля сообщения.
    """
    term = (search_term or "").strip()
    if not term:
        return False
    logger.info("Messenger /messages/new: попытка ввода в «Кому» term=%r", term[:48])
    _ = timeout_ms
    try:
        try:
            page.evaluate("() => { window.scrollTo(0, 0); }")
        except Exception:
            pass
        page.wait_for_timeout(350)
        _ensure_messenger_viewport(page)
        _scroll_dm_composer_candidates_into_view(page)
        page.wait_for_timeout(400)

        if _messages_new_recipient_ready(page):
            logger.info(
                "Messenger /messages/new: адресат уже выбран / композер доступен — не дублируем ввод в «Кому» (term=%r)",
                term[:48],
            )
            _messages_new_dismiss_autocomplete(page)
            _scroll_dm_composer_candidates_into_view(page)
            page.wait_for_timeout(500)
            return True

        for _wait in range(8):
            try:
                if page.locator('[role="combobox"]').count() > 0:
                    break
            except Exception:
                pass
            page.wait_for_timeout(250)

        # ── Фаза 1: найти и кликнуть поле «Кому» через Playwright (надёжнее чем JS focus) ──
        focused = False
        for name_re in (
            re.compile(r"кому", re.I),
            re.compile(r"^to\b", re.I),
            re.compile(r"name or group", re.I),
            re.compile(r"имя или груп", re.I),
            re.compile(r"recipient", re.I),
        ):
            try:
                loc = page.get_by_role("combobox", name=name_re)
                if loc.count() == 0:
                    continue
                tgt = loc.first
                if tgt.is_visible(timeout=1800):
                    tgt.scroll_into_view_if_needed(timeout=2000)
                    tgt.click(timeout=5000)
                    focused = True
                    logger.info("Messenger /messages/new: «Кому» найден через Playwright combobox (%s)", name_re.pattern)
                    break
            except Exception:
                continue
        if not focused:
            try:
                loc = page.locator('[role="main"]').locator('[contenteditable="true"]')
                for idx in range(min(loc.count(), 4)):
                    el = loc.nth(idx)
                    try:
                        bb = el.bounding_box()
                        if bb and bb.get("y", 9999) < 280 and bb.get("width", 0) > 60:
                            el.click(timeout=5000)
                            focused = True
                            logger.info("Messenger /messages/new: «Кому» найден через contenteditable (y=%.0f)", bb.get("y", -1))
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if not focused:
            focused = page.evaluate(
                """() => {
              function headPh(el) {
                return (
                  (el.getAttribute('aria-label') || '') + ' ' +
                  (el.getAttribute('aria-placeholder') || '') + ' ' +
                  (el.getAttribute('placeholder') || '')
                ).toLowerCase();
              }
              function tryFocus(el) {
                if (!el) return false;
                try {
                  el.scrollIntoView({ block: 'center', behavior: 'instant' });
                  el.focus();
                  el.click();
                  return true;
                } catch (e) {
                  return false;
                }
              }
              const main = document.querySelector('[role="main"]') || document.body;
              const labels = main.querySelectorAll('span, div, label');
              for (const el of labels) {
                const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (/^Кому\\s*:?$/i.test(t) || /^To\\s*:?$/i.test(t) || /^An\\s*:?$/i.test(t)) {
                  let root = el.parentElement;
                  for (let depth = 0; depth < 14 && root; depth++) {
                    const ce = root.querySelector(
                      '[contenteditable="true"],[role="combobox"],[role="searchbox"],input[type="text"],input:not([type])'
                    );
                    if (ce) {
                      const r = ce.getBoundingClientRect();
                      if (r.width > 10 && r.height > 2 && r.top < window.innerHeight * 0.55) {
                        return tryFocus(ce);
                      }
                    }
                    root = root.parentElement;
                  }
                }
              }
              const nodes = Array.from(
                main.querySelectorAll(
                  '[role="combobox"],[role="searchbox"],' +
                  'div[contenteditable="true"][role="textbox"],' +
                  'div[contenteditable="true"],input[type="text"],textarea'
                )
              );
              let best = null;
              let bestScore = -1e9;
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 16 || r.height < 3 || r.bottom < 6) continue;
                if (r.top > window.innerHeight * 0.78) continue;
                const tag = (el.tagName || '').toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const h = headPh(el);
                const isCe = tag === 'div' && el.getAttribute('contenteditable') === 'true';
                if (isCe && r.top > 240 && !h.includes('кому') && !h.includes('to') && !h.includes('recipient')) {
                  const msgLike = h.includes('сообщ') || h.includes('message') || h.includes('напиш')
                    || h.includes('type') || h.includes('write') || h === 'aa' || h.includes(' aa');
                  if (msgLike) continue;
                }
                let sc = 220 - r.top;
                if (r.top < 220) sc += 4200;
                if (h.includes('кому') || h.includes('to:') || h.includes(' to') ||
                    h.includes('recipient') || h.includes('name or group') || h.includes('имя или груп') ||
                    h.includes('search')) {
                  sc += 5200;
                }
                if (h.includes('сообщ') || h.includes('message') || h.includes('напиш')) sc -= 8000;
                if (role === 'combobox' || role === 'searchbox') sc += 900;
                if (isCe && r.top < 200 && r.width > 80) sc += 600;
                if (sc > bestScore) {
                  bestScore = sc;
                  best = el;
                }
              }
              if (!best) return false;
              return tryFocus(best);
            }"""
            )
        if not focused:
            logger.warning(
                "Messenger /messages/new: поле «Кому» не найдено, строка для поиска не введена (term=%r)",
                term[:48],
            )
            return False

        # ── Фаза 2: ввод текста — keyboard.type() как основной (запускает React-события) ──
        page.wait_for_timeout(350)
        _select_all_in_focused_field(page)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)

        page.keyboard.type(term, delay=35)
        # Meta подгружает «Другие люди» асинхронно — дать списку появиться
        page.wait_for_timeout(1200 if term.isdigit() else 800)

        has_typed = _focused_field_contains_typed_term(page, term)

        if not has_typed:
            logger.info("Messenger /messages/new: текст не появился после type(), повторный клик + type (term=%r)", term[:48])
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                for name_re in (re.compile(r"кому", re.I), re.compile(r"^to\b", re.I)):
                    try:
                        loc = page.get_by_role("combobox", name=name_re)
                        if loc.count() > 0 and loc.first.is_visible(timeout=1200):
                            loc.first.click(timeout=3000)
                            break
                    except Exception:
                        continue
                page.wait_for_timeout(250)
                _select_all_in_focused_field(page)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(150)
                page.keyboard.type(term, delay=45)
                page.wait_for_timeout(600)
            except Exception:
                logger.debug("retry type in Кому", exc_info=True)
            has_typed = _focused_field_contains_typed_term(page, term)

        if not has_typed and _try_set_focused_recipient_text_via_dom(page, term):
            page.wait_for_timeout(900)
            has_typed = _focused_field_contains_typed_term(page, term)

        # ── Фаза 3: ожидание и клик по автокомплиту ──
        picked = False
        for _aw in range(22):
            page.wait_for_timeout(450)
            if _messages_new_recipient_ready(page):
                picked = True
                logger.info("Messenger /messages/new: адресат уже готов до клика по dropdown")
                break
            interacted = False
            try:
                lb = page.locator('[role="listbox"]')
                if lb.count() > 0:
                    opt = lb.first.locator('[role="option"]').first
                    if opt.count() > 0 and opt.is_visible(timeout=600):
                        opt.click(timeout=4000, force=True)
                        interacted = True
            except Exception:
                pass
            if not interacted and _try_click_messages_new_autocomplete_first_option(page, term):
                interacted = True
            if interacted:
                page.wait_for_timeout(950)
                if _messages_new_recipient_ready(page):
                    picked = True
                    logger.info("Messenger /messages/new: адресат выбран через dropdown click")
                    break
        if not picked:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(400)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1200)
            picked = _messages_new_recipient_ready(page)
            if picked:
                logger.info("Messenger /messages/new: адресат выбран через ArrowDown+Enter")

        if not picked:
            logger.warning(
                "Messenger /messages/new: автокомплит не появился / адресат не выбран (term=%r)",
                term[:48],
            )
            return False

        _messages_new_dismiss_autocomplete(page)
        _scroll_dm_composer_candidates_into_view(page)
        page.wait_for_timeout(2400)
        return True
    except Exception:
        logger.debug("_try_select_recipient_on_messages_new_page failed", exc_info=True)
        return False


def _recipient_chip_appeared(page: Page) -> bool:
    """Проверяет, что в «Кому» появился токен/чип выбранного адресата (а не просто введённый текст)."""
    try:
        return bool(
            page.evaluate(
                """() => {
            const main = document.querySelector('[role="main"]') || document.body;
            const chips = main.querySelectorAll(
              '[data-testid*="recipient"], [data-testid*="token"], ' +
              'span[role="gridcell"], div[role="gridcell"], ' +
              'span[data-text="true"][contenteditable="false"], ' +
              'div.x1i10hfl[role="button"]'
            );
            for (const c of chips) {
              const r = c.getBoundingClientRect();
              if (r.width > 20 && r.height > 10 && r.top < 280) return true;
            }
            // Кнопка «Удалить» / Remove у выбранного чипа получателя
            const removeLabels = main.querySelectorAll(
              '[aria-label*="Remove" i], [aria-label*="Удалить"], [aria-label*="Видалити"], ' +
              '[aria-label*="Eliminar"]'
            );
            for (const b of removeLabels) {
              const r = b.getBoundingClientRect();
              if (r.width > 4 && r.height > 4 && r.top < 300 && r.bottom > 0) return true;
            }
            const labels = main.querySelectorAll('span, div');
            for (const el of labels) {
              const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
              if (/^Кому\\s*:?$/i.test(t) || /^To\\s*:?$/i.test(t)) {
                let root = el.parentElement;
                for (let d = 0; d < 8 && root; d++) {
                  const btns = root.querySelectorAll('[role="button"], [aria-label]');
                  for (const b of btns) {
                    const br = b.getBoundingClientRect();
                    if (br.width > 24 && br.height > 16 && br.top < 280) {
                      const txt = (b.textContent || '').trim();
                      if (txt.length >= 2 && txt.length < 100 && !/^(Кому|To|An)\\s*:?$/.test(txt)) {
                        return true;
                      }
                    }
                  }
                  root = root.parentElement;
                }
              }
            }
            return false;
          }"""
            )
        )
    except Exception:
        return False


def _messages_new_dismiss_autocomplete(page: Page) -> None:
    """Закрыть выпадающий список под «Кому» (Escape), не снимая уже выбранный чип — иначе композер не находится и скрипт снова вводит ID."""
    try:
        for _ in range(2):
            page.keyboard.press("Escape")
            page.wait_for_timeout(220)
        page.wait_for_timeout(350)
    except Exception:
        pass


def _messages_new_recipient_ready(page: Page) -> bool:
    """
    На /messages/new адресат считается выбранным, если появился chip в «Кому»
    или уже открылся нижний композер сообщения.
    """
    if _recipient_chip_appeared(page):
        return True
    try:
        return bool(
            page.evaluate(
                """() => {
            const main = document.querySelector('[role="main"]') || document.body;
            const nodes = main.querySelectorAll(
              '[data-lexical-editor="true"], ' +
              'div[contenteditable="true"][role="textbox"], ' +
              'div[contenteditable="true"], textarea, div[role="textbox"]'
            );
            const phText = (el) => (
              (el.getAttribute('aria-placeholder') || '') + ' ' +
              (el.getAttribute('placeholder') || '') + ' ' +
              (el.getAttribute('data-placeholder') || '') + ' ' +
              (el.getAttribute('aria-label') || '')
            ).toLowerCase();
            for (const el of nodes) {
              const r = el.getBoundingClientRect();
              if (r.width < 40 || r.height < 8) continue;
              if (r.bottom < 24 || r.top > window.innerHeight + 120) continue;
              if (r.top < window.innerHeight * 0.34) continue;
              if (el.closest('[role="combobox"], [role="search"]')) continue;
              const ph = phText(el);
              const msgLike =
                ph.includes('сообщ') || ph.includes('message') || ph.includes('aa') ||
                ph.includes('напиш') || ph.includes('type') || ph.includes('write');
              if (!msgLike && r.top < window.innerHeight * 0.56) continue;
              return true;
            }
            return false;
          }"""
            )
        )
    except Exception:
        return False


def _dm_page_looks_restricted(page: Page) -> bool:
    """Только явные тексты блокировки в области основного контента (не весь body — меньше ложных срабатываний)."""
    try:
        return bool(
            page.evaluate(
                """() => {
          const root =
            document.querySelector('[role="main"]') ||
            document.querySelector('[data-pagelet="MWPage"]') ||
            document.body;
          const t = (root && root.innerText) ? root.innerText.toLowerCase() : '';
          return (
            t.includes("doesn't have access to this chat") ||
            t.includes("can't reply to this conversation") ||
            t.includes("can't message") ||
            t.includes("this person is unavailable") ||
            t.includes("logs into messenger") ||
            t.includes("you will be able to send") ||
            t.includes('нет доступа к этому чату') ||
            t.includes('пока нет доступа') ||
            t.includes('войдет в messenger') ||
            t.includes('войдёт в messenger') ||
            t.includes('сможете отправлять') ||
            t.includes('у вас нет доступа к этому чату') ||
            t.includes('вы не можете писать в этот чат') ||
            t.includes('невозможно ответить в этом чате')
          );
        }"""
            )
        )
    except Exception:
        return False


def page_shows_messenger_message_request_limit(page: Page) -> bool:
    """
    Лимит Meta на запросы в переписку (часто 24 ч): вместо поля ввода — плашка в чате / popup.
    RU: «Достигнут лимит числа запросов на переписку».
    """
    try:
        return bool(
            page.evaluate(
                """() => {
          function strong(t) {
            const s = String(t || '').toLowerCase();
            if (!s) return false;
            if (s.includes('достигнут лимит') && s.includes('переписк')) return true;
            if (s.includes('лимит числа запросов')) return true;
            if (s.includes('message request limit')) return true;
            if (s.includes('too many') && s.includes('message request')) return true;
            if (s.includes('limit') && s.includes('message requests') && (s.includes('24') || s.includes('hours')))
              return true;
            return false;
          }
          const roots = document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"], [data-pagelet="ChatDock"]'
          );
          for (const el of roots) {
            if (strong(el.innerText || '')) return true;
          }
          return strong(document.body ? document.body.innerText : '');
        }"""
            )
        )
    except Exception:
        return False


def _scroll_dm_composer_candidates_into_view(page: Page) -> None:
    """Прокрутка к нижнему полю ввода — на /messages/ оно часто обрезано низом окна (Electron)."""
    try:
        if messenger_pin_restore_dialog_open(page):
            return
    except Exception:
        pass
    try:
        page.evaluate(
            """() => {
  const roots = [
    document.querySelector('[role="main"]'),
    document.querySelector('[data-pagelet*="MW"]'),
    document.querySelector('[data-pagelet="MWInboxMain"]'),
    document.getElementById('mount_0_0'),
    document.body,
  ].filter(Boolean);
  const seen = new Set();
  for (const mainArea of roots) {
    const scrollables = mainArea.querySelectorAll('*');
    for (const el of scrollables) {
      if (seen.has(el)) continue;
      const st = window.getComputedStyle(el);
      if ((st.overflowY === 'auto' || st.overflowY === 'scroll' || st.overflowY === 'overlay')
          && el.scrollHeight > el.clientHeight + 40) {
        seen.add(el);
        el.scrollTop = el.scrollHeight;
      }
    }
  }
  window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
  function placeholderText(el) {
    return (
      (el.getAttribute('aria-placeholder') || '') +
      ' ' + (el.getAttribute('placeholder') || '') +
      ' ' + (el.getAttribute('data-placeholder') || '') +
      ' ' + (el.getAttribute('aria-label') || '')
    ).toLowerCase();
  }
  const nodes = document.querySelectorAll(
    '[data-lexical-editor="true"], [contenteditable="true"][role="textbox"], textarea, [role="textbox"][contenteditable="true"]'
  );
  let best = null;
  let bestScore = -1e9;
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width < 36 || r.height < 6) continue;
    const ph = placeholderText(el);
    let score = r.bottom;
    if (ph.includes('сообщ') || ph.includes('message') || ph.includes('aa')
        || ph.includes('напиш') || ph.includes('type') || ph.includes('write')) {
      score += 5000;
    }
    if (el.closest('[role="combobox"], [role="search"]')) {
      score -= 10000;
    }
    if (score > bestScore) {
      bestScore = score;
      best = el;
    }
  }
  if (best) {
    let p = best.parentElement;
    for (let i = 0; i < 16 && p; i++) {
      const st = window.getComputedStyle(p);
      if ((st.overflowY === 'auto' || st.overflowY === 'scroll' || st.overflowY === 'overlay')
          && p.scrollHeight > p.clientHeight + 30) {
        p.scrollTop = p.scrollHeight;
      }
      p = p.parentElement;
    }
    best.scrollIntoView({ block: 'center', behavior: 'instant' });
    best.focus();
    let n = best;
    for (let i = 0; i < 40 && n; i++) {
      const st = window.getComputedStyle(n);
      if (st.position === 'fixed') {
        const rr = n.getBoundingClientRect();
        if (rr.width >= 180 && rr.height >= 80) {
          const margin = 24;
          const overflow = rr.bottom - (window.innerHeight - margin);
          if (overflow > 3) {
            if (!n.dataset.fbmOrigBottom) {
              n.dataset.fbmOrigBottom = st.bottom || '0px';
            }
            const ob = n.dataset.fbmOrigBottom || '0px';
            const m = /^([-+]?\\d*\\.?\\d+)px$/.exec(ob);
            const base = m ? (parseFloat(m[1]) || 0) : 0;
            n.style.bottom = (base + Math.min(Math.ceil(overflow) + 12, 420)) + 'px';
            n.dataset.fbmLifted = '1';
          }
          break;
        }
      }
      n = n.parentElement;
    }
  }
}"""
        )
    except Exception:
        pass


def _try_click_messenger_dock_send_button(page: Page) -> bool:
    """
    Клик по синей кнопке «отправить» у композера (док-чат на профиле или Messenger внизу).

    У Meta часто `div[role="button"]` + `svg > title` с текстом вроде
    «Нажмите Enter, чтобы отправить» — один Enter в Lexical не отправляет сообщение.
    """
    try:
        for pat in (
            re.compile(r"нажмите enter", re.I),
            re.compile(r"press enter", re.I),
            re.compile(r"щоб надіслати", re.I),
            re.compile(r"отправ", re.I),
            re.compile(r"\bsend\b", re.I),
            re.compile(r"envoyer", re.I),
            re.compile(r"enviar", re.I),
        ):
            try:
                loc = page.get_by_role("button", name=pat)
                n = min(loc.count(), 20)
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible(timeout=700):
                            continue
                        bb = el.bounding_box()
                        if not bb:
                            continue
                        if bb["y"] < 160:
                            continue
                        el.click(timeout=4500)
                        logger.info(
                            "messenger dock send: клик [role=button] name~%r y=%.0f",
                            pat.pattern,
                            bb["y"],
                        )
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass

    try:
        return bool(
            page.evaluate(
                """() => {
          const bottom = window.innerHeight * 0.38;
          const needles = [
            'нажмите enter', 'press enter', 'drücke enter', 'drücken sie enter',
            'отправить', 'отправ', ' send', 'send ', 'enviar', 'envoyer'
          ];
          function matches(s) {
            const x = (s || '').toLowerCase();
            for (const n of needles) { if (x.includes(n.trim())) return true; }
            return false;
          }
          for (const svg of document.querySelectorAll('svg')) {
            const tit = svg.querySelector('title');
            const t = (tit && tit.textContent) || '';
            if (!matches(t)) continue;
            let n = svg.parentElement;
            for (let d = 0; d < 12 && n; d++) {
              const role = (n.getAttribute('role') || '').toLowerCase();
              const tab = n.getAttribute('tabindex');
              if (role === 'button' || tab === '0') {
                const r = n.getBoundingClientRect();
                if (r.width >= 4 && r.height >= 4 && r.top >= bottom - 80) {
                  try {
                    n.scrollIntoView({ block: 'nearest', behavior: 'instant' });
                    n.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    n.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    n.click();
                    return true;
                  } catch (e) {}
                }
              }
              n = n.parentElement;
            }
          }
          for (const d of document.querySelectorAll('div[role="button"]')) {
            const svg = d.querySelector(':scope svg');
            const tit = svg && svg.querySelector('title');
            const t = (tit && tit.textContent) || '';
            const al = (d.getAttribute('aria-label') || '').toLowerCase();
            if (!matches(t) && !matches(al)) continue;
            const r = d.getBoundingClientRect();
            if (r.width < 4 || r.height < 4 || r.top < bottom - 80) continue;
            try {
              d.scrollIntoView({ block: 'nearest', behavior: 'instant' });
              d.click();
              return true;
            } catch (e) {}
          }
          return false;
        }"""
            )
        )
    except Exception:
        logger.debug("_try_click_messenger_dock_send_button evaluate", exc_info=True)
        return False


def _submit_messenger_composer_after_typing(page: Page, sent_text: str) -> tuple[bool, str]:
    """
    Отправка набранного текста: пауза как у человека (≈12–22 с после набора, до клика «Отправить»),
    затем клик по синей кнопке / Enter, проверка очистки композера и появления текста в ленте чата.

    Слишком быстрый «набор + отправка» даёт ложное «успех» в UI при том, что сервер Facebook
    не успевает принять сообщение — отчёт «Отправлено» не совпадает с папкой «Отправленные».
    Параметры смещены в сторону стабильности (дольше ждём и чаще перепроверяем тред).
    """
    pre_send_ms = random.randint(12_000, 22_000)
    logger.info(
        "Messenger: пауза %s мс перед отправкой (имитация просмотра текста перед кнопкой)",
        pre_send_ms,
    )
    page.wait_for_timeout(pre_send_ms)
    page.wait_for_timeout(320)
    if not _try_click_messenger_dock_send_button(page):
        page.keyboard.press("Enter")
    page.wait_for_timeout(1450)
    ok, reason = _verify_dm_send_cleared(page, sent_text)
    if ok:
        ok_thread, treason = verify_outgoing_message_appeared_in_thread(
            page, sent_text, max_attempts=52, pause_ms=680
        )
        if ok_thread:
            return True, ""
        logger.info(
            "Messenger: композер пуст, пузырь в треде пока не найден — доп. ожидание и повтор проверки треда"
        )
        page.wait_for_timeout(4200)
        ok_thread2, treason2 = verify_outgoing_message_appeared_in_thread(
            page, sent_text, max_attempts=44, pause_ms=620
        )
        if ok_thread2:
            return True, ""
        return False, (treason2 or treason or "not_visible_in_thread")[:220]
    retry_pause = random.randint(4500, 9000)
    logger.info("Messenger: повтор отправки после паузы %s мс", retry_pause)
    page.wait_for_timeout(retry_pause)
    if _try_click_messenger_dock_send_button(page):
        page.wait_for_timeout(920)
    else:
        page.keyboard.press("Enter")
        page.wait_for_timeout(780)
    ok2, reason2 = _verify_dm_send_cleared(page, sent_text)
    if ok2:
        ok_thread, treason = verify_outgoing_message_appeared_in_thread(
            page, sent_text, max_attempts=52, pause_ms=680
        )
        if ok_thread:
            return True, ""
        logger.info(
            "Messenger: после повтора: композер пуст, пузырь не найден — доп. ожидание и повтор проверки треда"
        )
        page.wait_for_timeout(4200)
        ok_thread2, treason2 = verify_outgoing_message_appeared_in_thread(
            page, sent_text, max_attempts=44, pause_ms=620
        )
        if ok_thread2:
            return True, ""
        return False, (treason2 or treason or "not_visible_in_thread")[:220]
    return False, (reason2 or reason or "unverified")[:200]


def _dismiss_profile_share_or_post_composer_dialog(page: Page) -> None:
    """
    Закрыть модалку «Поделиться в ленту» / Share, если открылась по ошибке (часто после скролла по ленте).
    Иначе фокус и клавиатура уходят в поле «Расскажите об этом…», а не в Messenger.
    """
    try:
        closed = page.evaluate(
            """() => {
          const needles = [
            'поделиться', 'расскажите об этом', 'лента', 'друзья',
            'share to your feed', 'tell your story', 'share to',
            'create post', 'создать публикацию'
          ];
          function norm(s) { return (s || '').toLowerCase().replace(/\\s+/g, ' ').trim(); }
          const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
          for (const d of dialogs) {
            const r = d.getBoundingClientRect();
            if (r.width < 120 || r.height < 80) continue;
            const t = norm(d.innerText || '').slice(0, 900);
            let hits = 0;
            for (const n of needles) { if (t.includes(n)) hits++; }
            if (hits < 2 && !t.includes('поделиться') && !t.includes('share to')) continue;
            const closers = d.querySelectorAll(
              '[aria-label*="Close" i], [aria-label*="Закрыть"], [aria-label*="Скасувати"], ' +
              '[data-testid="dialog_title_close_button"], [aria-label="Закрыть"][role="button"]'
            );
            for (const c of closers) {
              const cr = c.getBoundingClientRect();
              if (cr.width > 2 && cr.height > 2) {
                try { c.click(); return true; } catch (e) {}
              }
            }
          }
          return false;
        }"""
        )
        if closed:
            page.wait_for_timeout(500)
    except Exception:
        logger.debug("dismiss share dialog evaluate", exc_info=True)
    try:
        if page.evaluate(
            """() => {
          const d = document.querySelector('[role="dialog"]');
          if (!d) return false;
          const t = (d.innerText || '').toLowerCase();
          return t.includes('поделиться') || t.includes('share to') || t.includes('расскажите');
        }"""
        ):
            for _ in range(4):
                page.keyboard.press("Escape")
                page.wait_for_timeout(320)
    except Exception:
        pass


# Те же границы и фильтры, что в _click_profile_header_message_button_js — только проверка наличия (без клика).
_PROFILE_HEADER_DM_ENTRY_PRESENT_JS = """() => {
  const vH = window.innerHeight;
  const TOP_NAV = Math.max(56, vH * 0.06);
  const MAX_Y = vH * 0.82;
  function inArticle(el) {
    let n = el;
    for (let i = 0; i < 22 && n; i++) {
      if (n.getAttribute('role') === 'article') return true;
      n = n.parentElement;
    }
    return false;
  }
  function badLabel(al) {
    const s = (al || '').trim().toLowerCase();
    if (!s) return false;
    if (s.includes('поделиться')) return true;
    if (s.includes('share') && !s.includes('message')) return true;
    if (s.includes('коммент') || s.includes('comment')) return true;
    if (s.includes('поиск') || s.includes('search') || s.includes('найти')) return true;
    if (s === 'мессенджер' || s === 'messenger') return true;
    return false;
  }
  function inBand(r) {
    return r.width >= 4 && r.height >= 4 && r.top >= TOP_NAV && r.top <= MAX_Y && r.bottom > TOP_NAV + 8;
  }
  for (const a of document.querySelectorAll(
    'a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]'
  )) {
    const r = a.getBoundingClientRect();
    if (inBand(r) && !inArticle(a)) return true;
  }
  const exactLabels = [
    'Сообщение', 'Message', 'Nachricht', 'Envoyer un message',
    'Send message', 'Написать сообщение'
  ];
  for (const lbl of exactLabels) {
    for (const el of document.querySelectorAll('[aria-label="' + lbl + '"]')) {
      const r = el.getBoundingClientRect();
      if (inBand(r) && !inArticle(el)) return true;
    }
  }
  for (const b of document.querySelectorAll('[role="button"]')) {
    const al = (b.getAttribute('aria-label') || '').trim().toLowerCase();
    const r = b.getBoundingClientRect();
    if (!inBand(r) || inArticle(b) || badLabel(al)) continue;
    if (al === 'message' || al === 'сообщение') return true;
    if (al.startsWith('сообщение') && al.length < 48 && !al.includes('запрос')) return true;
    const tx = ((b.innerText || '') + '').trim().toLowerCase().replace(/\\s+/g, ' ');
    if ((tx === 'сообщение' || tx === 'message') && !badLabel(al)) return true;
  }
  for (const span of document.querySelectorAll('span, div')) {
    const raw = (span.innerText || '').trim().replace(/\\s+/g, ' ');
    const low = raw.toLowerCase();
    if (low !== 'сообщение' && low !== 'message') continue;
    let p = span.parentElement;
    for (let d = 0; d < 10 && p; d++) {
      if (p.getAttribute('role') === 'button') {
        const r = p.getBoundingClientRect();
        if (inBand(r) && !inArticle(p) && !badLabel(p.getAttribute('aria-label') || '')) return true;
        break;
      }
      p = p.parentElement;
    }
  }
  return false;
}"""


def _click_profile_header_message_button_js(page: Page) -> bool:
    """
    Клик по кнопке «Сообщение» на профиле (не внутри поста article, не верхняя иконка «Мессенджер»).

    Раньше отсекали всё с r.top > 44% высоты окна — на профилях с большой обложкой кнопка «Сообщение»
    оказывается ниже и клик никогда не срабатывал (message_button_click_failed).
    """
    try:
        return bool(
            page.evaluate(
                """() => {
          const vH = window.innerHeight;
          const TOP_NAV = Math.max(56, vH * 0.06);
          const MAX_Y = vH * 0.82;
          function inArticle(el) {
            let n = el;
            for (let i = 0; i < 22 && n; i++) {
              if (n.getAttribute('role') === 'article') return true;
              n = n.parentElement;
            }
            return false;
          }
          function badLabel(al) {
            const s = (al || '').trim().toLowerCase();
            if (!s) return false;
            if (s.includes('поделиться')) return true;
            if (s.includes('share') && !s.includes('message')) return true;
            if (s.includes('коммент') || s.includes('comment')) return true;
            if (s.includes('поиск') || s.includes('search') || s.includes('найти')) return true;
            if (s === 'мессенджер' || s === 'messenger') return true;
            return false;
          }
          function clickNode(node) {
            if (!node) return false;
            try {
              node.scrollIntoView({ block: 'center', behavior: 'instant' });
              node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
              node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
              node.click();
              return true;
            } catch (e) { return false; }
          }
          function inBand(r) {
            return r.width >= 4 && r.height >= 4 && r.top >= TOP_NAV && r.top <= MAX_Y && r.bottom > TOP_NAV + 8;
          }
          for (const a of document.querySelectorAll(
            'a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]'
          )) {
            const r = a.getBoundingClientRect();
            if (!inBand(r) || inArticle(a)) continue;
            if (clickNode(a)) return true;
          }
          const exactLabels = [
            'Сообщение', 'Message', 'Nachricht', 'Envoyer un message',
            'Send message', 'Написать сообщение'
          ];
          for (const lbl of exactLabels) {
            for (const el of document.querySelectorAll('[aria-label="' + lbl + '"]')) {
              const r = el.getBoundingClientRect();
              if (!inBand(r) || inArticle(el)) continue;
              if (clickNode(el)) return true;
            }
          }
          for (const b of document.querySelectorAll('[role="button"]')) {
            const al = (b.getAttribute('aria-label') || '').trim().toLowerCase();
            const r = b.getBoundingClientRect();
            if (!inBand(r) || inArticle(b) || badLabel(al)) continue;
            if (al === 'message' || al === 'сообщение') {
              if (clickNode(b)) return true;
            }
            if (al.startsWith('сообщение') && al.length < 48 && !al.includes('запрос')) {
              if (clickNode(b)) return true;
            }
            const tx = ((b.innerText || '') + '').trim().toLowerCase().replace(/\\s+/g, ' ');
            if ((tx === 'сообщение' || tx === 'message') && !badLabel(al)) {
              if (clickNode(b)) return true;
            }
          }
          for (const span of document.querySelectorAll('span, div')) {
            const raw = (span.innerText || '').trim().replace(/\\s+/g, ' ');
            const low = raw.toLowerCase();
            if (low !== 'сообщение' && low !== 'message') continue;
            let p = span.parentElement;
            for (let d = 0; d < 10 && p; d++) {
              if (p.getAttribute('role') === 'button') {
                const r = p.getBoundingClientRect();
                if (inBand(r) && !inArticle(p) && !badLabel(p.getAttribute('aria-label') || '')) {
                  if (clickNode(p)) return true;
                }
                break;
              }
              p = p.parentElement;
            }
          }
          return false;
        }"""
            )
        )
    except Exception:
        logger.debug("_click_profile_header_message_button_js", exc_info=True)
        return False


def _scroll_profile_feed_before_dm(page: Page, throttle_preset: str) -> None:
    """
    Имитация просмотра профиля: плавный скролл вниз (3–5 постов) перед кликом «Сообщение».
    Несколько scroll + пауз «чтения» — как у реального пользователя, который заходит на профиль
    и просматривает записи перед тем, как написать.
    """
    pr = throttle_preset if is_valid_throttle_preset(throttle_preset) else "medium"
    scroll_steps = random.randint(3, 5)
    for _i in range(scroll_steps):
        soft_scroll_feed(page, pr, strength=random.uniform(0.7, 1.2))
        if random.random() < 0.6:
            micro_reading_pause(page, pr)
    page.wait_for_timeout(random.randint(400, 900))
    try:
        page.evaluate("() => { window.scrollTo({ top: 0, behavior: 'smooth' }); }")
    except Exception:
        pass
    page.wait_for_timeout(random.randint(800, 1600))


def dismiss_messenger_first_chat_start_gate_if_present(
    page: Page, *, max_passes: int = 3, log_label: str = "messenger"
) -> int:
    """
    У страниц / витрин при первом ЛС Meta показывает экран вроде «Коснитесь, чтобы отправить» с кнопкой
    «Начать» (или Start / Get started). Пока её не нажать, поле ввода в доке или в полноэкранном Messenger не появляется.

    Возвращает число успешных кликов по gate (0 — если кнопки не было).
    """
    total = 0
    for _ in range(max(1, int(max_passes))):
        clicked = False
        for rx in (
            re.compile(r"^Начать$", re.I),
            re.compile(r"^Start$", re.I),
            re.compile(r"^Get started$", re.I),
            re.compile(r"^Empezar$", re.I),
            re.compile(r"^Iniciar$", re.I),
            re.compile(r"^Commencer$", re.I),
            re.compile(r"^Loslegen$", re.I),
            re.compile(r"^Начать чат$", re.I),
        ):
            try:
                loc = page.get_by_role("button", name=rx)
                n = min(loc.count(), 10)
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible(timeout=1400):
                            continue
                        el.scroll_into_view_if_needed(timeout=3000)
                        el.click(timeout=7000)
                        logger.info(
                            "%s: снят gate первого сообщения — клик по кнопке (%s)",
                            log_label,
                            rx.pattern,
                        )
                        clicked = True
                        total += 1
                        break
                    except Exception:
                        continue
                if clicked:
                    break
            except Exception:
                continue
        if not clicked:
            try:
                did = page.evaluate(
                    """() => {
                  const want = new Set([
                    'начать', 'start', 'get started', 'empezar', 'iniciar',
                    'commencer', 'loslegen', 'начать чат'
                  ]);
                  function norm(s) {
                    return (s || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                  }
                  function inArticle(el) {
                    let n = el;
                    for (let i = 0; i < 20 && n; i++) {
                      if (n.getAttribute('role') === 'article') return true;
                      n = n.parentElement;
                    }
                    return false;
                  }
                  function ancestorText(el) {
                    let s = '';
                    let n = el;
                    for (let i = 0; i < 16 && n; i++) {
                      s += ' ' + (n.innerText || '').slice(0, 500);
                      n = n.parentElement;
                    }
                    return s.toLowerCase();
                  }
                  function clickNode(el) {
                    try {
                      el.scrollIntoView({ block: 'center', behavior: 'instant' });
                      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                      el.click();
                      return true;
                    } catch (e) { return false; }
                  }
                  const candidates = [];
                  for (const el of document.querySelectorAll('[role="button"], button, a[role="button"]')) {
                    if (inArticle(el)) continue;
                    const t = norm(el.innerText || '');
                    if (!t || !want.has(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 6 || r.height < 4) continue;
                    candidates.push({ el, r, t });
                  }
                  for (const span of document.querySelectorAll('span, div')) {
                    if (inArticle(span)) continue;
                    const raw = norm(span.innerText || '');
                    if (!raw || !want.has(raw)) continue;
                    let p = span.parentElement;
                    for (let d = 0; d < 10 && p; d++) {
                      const role = (p.getAttribute('role') || '').toLowerCase();
                      if (role === 'button') {
                        const r = p.getBoundingClientRect();
                        if (r.width >= 6 && r.height >= 4) candidates.push({ el: p, r, t: raw });
                        break;
                      }
                      p = p.parentElement;
                    }
                  }
                  const vH = window.innerHeight;
                  let best = null;
                  let bestScore = -1e9;
                  for (const { el, r } of candidates) {
                    if (r.bottom < 80 || r.top > vH + 40) continue;
                    const at = ancestorText(el);
                    let score = 0;
                    if (r.top > vH * 0.38) score += 800;
                    if (
                      at.includes('коснитесь') || at.includes('tap to send') || at.includes('when you send') ||
                      at.includes('когда вы отправите') || at.includes('общедоступн') ||
                      at.includes('public information') || at.includes('messenger') || at.includes('message')
                    ) {
                      score += 1200;
                    }
                    if (at.includes('поделиться') || at.includes('share to') || at.includes('create post')) {
                      score -= 5000;
                    }
                    if (score > bestScore) {
                      bestScore = score;
                      best = el;
                    }
                  }
                  if (best && bestScore >= 400 && clickNode(best)) return true;
                  for (const { el, r } of candidates) {
                    if (r.top > vH * 0.42 && r.bottom < vH + 20 && clickNode(el)) return true;
                  }
                  return false;
                }"""
                )
                if did:
                    logger.info("%s: снят gate первого сообщения — DOM-fallback", log_label)
                    clicked = True
                    total += 1
            except Exception:
                logger.debug("%s: gate DOM-fallback", log_label, exc_info=True)
        if not clicked:
            break
        page.wait_for_timeout(random.randint(700, 1300))
    return total


def _find_popup_chat_composer(page: Page) -> bool:
    """
    Сфокусировать поле ввода только в доке / popup Messenger, не в комментарии к посту и не в «Поделиться».
    """
    for _pw in range(20):
        if _pw < 8:
            dismiss_messenger_first_chat_start_gate_if_present(
                page, max_passes=2, log_label="popup-dm"
            )
        try:
            found = page.evaluate(
                """() => {
          const BAD = /коммент|comment|ответить|\\breply\\b|о чем вы думаете|what.*on your mind|search|найти|расскажите об этом|tell your story|create post|создайте публикац|write on his|напишите на стене/i;
          const GOOD = /\\baa\\b|сообщен|message(?!s and)|напиш|type a message|write a message|schreib|envoyer un message/i;
          function ctxPenalty(el) {
            let n = el, pen = 0;
            for (let i = 0; i < 22 && n; i++) {
              if (n.getAttribute('role') === 'article') pen += 10000;
              if (n.getAttribute('role') === 'dialog') {
                const dt = ((n.innerText || '') + ' ' + (n.getAttribute('aria-label') || '')).slice(0, 500).toLowerCase();
                if (dt.includes('поделиться') || dt.includes('share to') || dt.includes('расскажите') || dt.includes('лента'))
                  pen += 12000;
              }
              n = n.parentElement;
            }
            return pen;
          }
          function ctxBonus(el) {
            let n = el;
            for (let i = 0; i < 24 && n; i++) {
              const chunk = ((n.innerText || '') + ' ' + (n.getAttribute('aria-label') || '')).slice(0, 280).toLowerCase();
              if (chunk.includes('end-to-end') || chunk.includes('сквозн') || chunk.includes('зашифрован') || chunk.includes('шифрован'))
                return 3500;
              n = n.parentElement;
            }
            return 0;
          }
          const vH = window.innerHeight;
          const vW = window.innerWidth;
          let best = null;
          let bestScore = -1e9;
          for (const el of document.querySelectorAll('div[contenteditable="true"]')) {
            const role = (el.getAttribute('role') || '').toLowerCase();
            const lex = el.hasAttribute('data-lexical-editor');
            if (role !== 'textbox' && !lex) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 52 || r.height < 6) continue;
            const ph = (
              (el.getAttribute('aria-placeholder') || '') + ' ' +
              (el.getAttribute('placeholder') || '') + ' ' +
              (el.getAttribute('data-placeholder') || '') + ' ' +
              (el.getAttribute('aria-label') || '')
            );
            const phl = ph.toLowerCase();
            if (BAD.test(phl)) continue;
            let score = ctxBonus(el) - ctxPenalty(el);
            if (r.top > vH * 0.40) score += 600;
            if (r.bottom > vH * 0.68) score += 450;
            if (r.left > vW * 0.30) score += 220;
            if (GOOD.test(phl)) score += 900;
            if (lex && r.top > vH * 0.38) score += 200;
            if (score > bestScore) {
              bestScore = score;
              best = el;
            }
          }
          if (!best || bestScore < 350) return false;
          best.scrollIntoView({ block: 'nearest', behavior: 'instant' });
          best.focus();
          best.click();
          return true;
        }"""
            )
            if found:
                logger.info("popup-dm: выбран композер Messenger (score-эвристика), попытка %s", _pw + 1)
                return True
        except Exception:
            logger.debug("_find_popup_chat_composer evaluate", exc_info=True)
        page.wait_for_timeout(550)

    logger.warning("popup-dm: композер Messenger не найден после ожидания")
    return False


def send_dm_via_profile_popup_chat(
    page: Page,
    text: str,
    *,
    canonical_url: str | None = None,
    timeout_ms: int = 90_000,
    throttle_preset: str = "medium",
) -> tuple[bool, str]:
    """
    Основной путь отправки ЛС для рассылки:
      1. Находимся на странице профиля (уже загружена).
      2. Проверяем кнопку «Сообщение» — если нет, сразу пропуск.
      3. Скроллим профиль вниз (3–5 постов), как будто просматриваем записи.
      4. Возвращаемся наверх, жмём «Сообщение» → появляется popup-окно чата.
      5. Ждём появления поля ввода в popup, печатаем текст шаблона.
      6. Пауза 5–15 сек (как человек перечитывает перед отправкой).
      7. Жмём «Отправить», верифицируем отправку.
      8. Только после успешной верификации закрываем popup (с паузой для синхронизации).
      9. Если кнопки нет или popup не открылся — пропуск без попытки через /messages/t/.
    """
    if not (text or "").strip():
        return False, "empty_dm"
    safe = text.strip()[:4000]

    try:
        page.evaluate("() => { window.scrollTo(0, 0); }")
    except Exception:
        pass
    page.wait_for_timeout(400)

    try_close_facebook_messenger_popup(page)
    page.wait_for_timeout(400)

    if not profile_page_has_dm_entry_visible(page):
        logger.info("popup-dm: кнопка «Сообщение» не найдена на профиле — пропуск")
        return False, "message_button_not_found"

    logger.info("popup-dm: кнопка «Сообщение» есть, скроллим профиль (имитация просмотра)")
    _scroll_profile_feed_before_dm(page, throttle_preset)
    _dismiss_profile_share_or_post_composer_dialog(page)

    try_close_facebook_messenger_popup(page)
    page.wait_for_timeout(300)

    logger.info("popup-dm: клик по «Сообщение»")
    if not _try_click_message_entry_on_profile(page):
        logger.info(
            "popup-dm: клик по «Сообщение» не сработал — пропуск (нет кнопки или закрытый профиль)"
        )
        return False, "message_button_not_found"

    page.wait_for_timeout(random.randint(2500, 4000))
    _dismiss_profile_share_or_post_composer_dialog(page)

    try:
        cur = (page.url or "").lower()
        if "/messages/" in cur:
            logger.info("popup-dm: Facebook перешёл в полноэкранный Messenger — пропуск")
            return False, "redirected_to_messenger"
    except Exception:
        pass

    dismiss_messenger_first_chat_start_gate_if_present(page, max_passes=3, log_label="popup-dm")
    page.wait_for_timeout(500)

    logger.info("popup-dm: ожидаем popup-окно чата")
    if not _find_popup_chat_composer(page):
        if page_shows_messenger_message_request_limit(page):
            logger.warning("popup-dm: лимит запросов Messenger Meta — останавливаем попытку")
            return False, "facebook_message_request_limit"
        logger.warning("popup-dm: поле ввода в popup не найдено")
        return False, "popup_composer_not_found"

    def _focus_is_messenger_composer_safe() -> bool:
        try:
            return bool(
                page.evaluate(
                    """() => {
              const el = document.activeElement;
              if (!el) return false;
              let n = el;
              for (let i = 0; i < 18 && n; i++) {
                if (n.getAttribute('role') === 'article') return false;
                if (n.getAttribute('role') === 'dialog') {
                  const t = (n.innerText || '').toLowerCase();
                  if (t.includes('поделиться') || t.includes('share to') || t.includes('расскажите'))
                    return false;
                }
                n = n.parentElement;
              }
              return true;
            }"""
                )
            )
        except Exception:
            return True

    if not _focus_is_messenger_composer_safe():
        logger.warning("popup-dm: фокус в ленте/модалке — закрываем и ищем композер снова")
        _dismiss_profile_share_or_post_composer_dialog(page)
        page.wait_for_timeout(600)
        if not _find_popup_chat_composer(page):
            if page_shows_messenger_message_request_limit(page):
                logger.warning("popup-dm: лимит запросов Messenger (контекст композера)")
                return False, "facebook_message_request_limit"
            return False, "popup_composer_wrong_context"

    logger.info("popup-dm: composer найден, набираем текст")
    page.wait_for_timeout(random.randint(500, 1200))

    try:
        _type_messenger_composer_natural(page, safe, throttle_preset=throttle_preset)
    except Exception as e:
        return False, f"popup_type:{e!s}"[:120]

    pre_send_ms = random.randint(5000, 15_000)
    logger.info("popup-dm: пауза %s мс перед отправкой (имитация перечитывания)", pre_send_ms)
    page.wait_for_timeout(pre_send_ms)

    page.wait_for_timeout(random.randint(200, 500))
    if not _try_click_messenger_dock_send_button(page):
        page.keyboard.press("Enter")
    page.wait_for_timeout(1500)

    ok, reason = _verify_dm_send_cleared(page, safe)
    if not ok:
        retry_pause = random.randint(4000, 8000)
        logger.info("popup-dm: повтор отправки после %s мс", retry_pause)
        page.wait_for_timeout(retry_pause)
        if not _try_click_messenger_dock_send_button(page):
            page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        ok, reason = _verify_dm_send_cleared(page, safe)

    if not ok:
        if page_shows_messenger_message_request_limit(page):
            logger.warning("popup-dm: лимит запросов Messenger после попытки отправки")
            return False, "facebook_message_request_limit"
        logger.warning("popup-dm: отправка не подтверждена — %s", reason)
        return False, f"popup_send_unverified:{reason}"[:200]

    ok_thread, treason = verify_outgoing_message_appeared_in_thread(
        page, safe, max_attempts=48, pause_ms=640
    )
    if not ok_thread:
        logger.info("popup-dm: пузырь не найден сразу — доп. ожидание")
        page.wait_for_timeout(4500)
        ok_thread, treason = verify_outgoing_message_appeared_in_thread(
            page, safe, max_attempts=40, pause_ms=600
        )

    if ok_thread:
        logger.info("popup-dm: сообщение подтверждено в треде")
    else:
        logger.info(
            "popup-dm: композер пуст (сообщение ушло), пузырь пока не виден — %s; "
            "считаем успехом (композер очищен)",
            treason,
        )

    settle_ms = messenger_post_dm_close_settle_ms(profile_dock_or_profile_path=True)
    logger.info("popup-dm: пауза %s мс перед закрытием popup", settle_ms)
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:
        pass

    try_close_facebook_messenger_popup(page)
    page.wait_for_timeout(400)

    logger.info("popup-dm: сообщение отправлено, popup закрыт")
    return True, "dm_sent_via_profile_popup"


def _try_send_via_profile_popup_chat(
    page: Page,
    text: str,
    *,
    canonical_url: str | None = None,
    timeout_ms: int = 90_000,
    throttle_preset: str = "medium",
) -> tuple[bool, str]:
    """
    Fallback-отправка ЛС (legacy): профиль → кнопка «Сообщение» → popup-окно чата.
    Используется только как fallback из try_send_dm_from_profile при ошибке /messages/t/.
    """
    prof = profile_url_from_person((canonical_url or "").strip()) if canonical_url else ""
    if not prof:
        return False, "no_profile_url"
    try:
        logger.info("popup-chat fallback: переход на профиль %s", prof[:80])
        page.goto(prof, wait_until="domcontentloaded", timeout=max(30_000, timeout_ms))
        page.wait_for_timeout(2000)
    except Exception as e:
        return False, f"profile_nav:{e!s}"[:120]

    try:
        page.evaluate("() => { window.scrollTo(0, 0); }")
    except Exception:
        pass
    page.wait_for_timeout(400)

    try_close_facebook_messenger_popup(page)
    page.wait_for_timeout(400)

    if not _try_click_message_entry_on_profile(page):
        return False, "message_button_not_found"

    page.wait_for_timeout(3000)

    try:
        cur = (page.url or "").lower()
        if "/messages/" in cur:
            logger.info("popup-chat fallback: Facebook перешёл в полноэкранный Messenger — отмена popup")
            return False, "redirected_to_messenger"
    except Exception:
        pass

    dismiss_messenger_first_chat_start_gate_if_present(page, max_passes=3, log_label="popup-chat fallback")
    page.wait_for_timeout(500)

    if not _find_popup_chat_composer(page):
        if page_shows_messenger_message_request_limit(page):
            logger.warning("popup-chat fallback: лимит запросов Messenger Meta")
            return False, "facebook_message_request_limit"
        logger.warning("popup-chat fallback: popup composer не найден на профиле")
        return False, "popup_composer_not_found"

    logger.info("popup-chat fallback: composer найден, набираем текст")
    page.wait_for_timeout(400)

    try:
        _type_messenger_composer_natural(page, text, throttle_preset=throttle_preset)
    except Exception as e:
        return False, f"popup_type:{e!s}"[:120]

    ok_clear, vreason = _submit_messenger_composer_after_typing(page, text)
    if not ok_clear:
        if page_shows_messenger_message_request_limit(page):
            return False, "facebook_message_request_limit"
        return False, f"popup_send_unverified:{vreason}"[:200]

    page.wait_for_timeout(2800)
    logger.info("popup-chat fallback: сообщение отправлено через popup-чат на профиле")
    return True, "dm_sent_via_profile_popup"


def _try_click_message_entry_on_profile(page: Page) -> bool:
    """
    Кнопка «Сообщение» только в шапке профиля.
    Не использовать широкий матч по подстроке «message» — ловятся «Поделиться», Messenger в посте и т.д.
    """
    if _click_profile_header_message_button_js(page):
        return True

    try:
        vp = page.viewport_size
        vh = float(vp["height"] if vp else 900)
        header_y_max = vh * 0.82
        header_y_min = max(52.0, vh * 0.06)
    except Exception:
        header_y_max = 720.0
        header_y_min = 52.0

    exact_names = (
        re.compile(r"^Сообщение$", re.I),
        re.compile(r"^Message$", re.I),
        re.compile(r"^Nachricht$", re.I),
        re.compile(r"^Envoyer un message$", re.I),
        re.compile(r"^Send message$", re.I),
        re.compile(r"^Написать сообщение$", re.I),
    )
    for role in ("button", "link"):
        for pat in exact_names:
            try:
                loc = page.get_by_role(role, name=pat)
                n = min(loc.count(), 8)
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible(timeout=900):
                            continue
                        bb = el.bounding_box()
                        y = bb.get("y", 9999) if bb else 9999
                        if not bb or y < header_y_min or y > header_y_max:
                            continue
                        el.click(timeout=8000)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue

    for lbl in (
        "Сообщение",
        "Message",
        "Nachricht",
        "Envoyer un message",
        "Send message",
        "Написать сообщение",
    ):
        loc = page.locator(f'[aria-label="{lbl}"]')
        try:
            cnt = min(loc.count(), 6)
            for i in range(cnt):
                el = loc.nth(i)
                if not el.is_visible(timeout=1000):
                    continue
                bb = el.bounding_box()
                y = bb.get("y", 9999) if bb else 9999
                if bb and header_y_min <= y <= header_y_max:
                    el.click(timeout=8000)
                    return True
        except Exception:
            continue

    try:
        for sel in ('a[href*="/messages/t/"]', 'a[href*="/messages/e2ee/t/"]'):
            loc = page.locator(sel)
            cnt = min(loc.count(), 8)
            for i in range(cnt):
                link = loc.nth(i)
                try:
                    if not link.is_visible(timeout=800):
                        continue
                    bb = link.bounding_box()
                    y = bb.get("y", 9999) if bb else 9999
                    if bb and header_y_min <= y <= header_y_max:
                        link.click(timeout=8000)
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    return _click_profile_header_message_button_js(page)


_RESET_FB_CHAT_DOCK_LIFT_JS = """() => {
  document.querySelectorAll('[data-fbm-lifted="1"]').forEach((d) => {
    const ob = d.dataset.fbmOrigBottom;
    if (ob != null && ob !== '') {
      d.style.bottom = ob;
    } else {
      d.style.removeProperty('bottom');
    }
    delete d.dataset.fbmOrigBottom;
    delete d.dataset.fbmLifted;
  });
}"""


def _reset_fb_chat_dock_lift(page: Page) -> None:
    """Снять временный подъём док-чата Messenger (см. _DM_COMPOSER_PICK_JS)."""
    try:
        page.evaluate(_RESET_FB_CHAT_DOCK_LIFT_JS)
    except Exception:
        pass


_CLOSE_FB_CHAT_POPUP_JS = """() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && r.bottom > 36 && r.top < window.innerHeight + 80;
  }
  function looksLikeCloseChatLabel(al) {
    const s = (al || '').toLowerCase().trim();
    if (!s) return false;
    if (s.includes('minimize') || s.includes('свернуть') || s.includes('згорнути')) return false;
    if (s.includes('close chat') || s.includes('закрыть чат') || s.includes('закрити чат')) return true;
    if (s.includes('cerrar') && s.includes('chat')) return true;
    if (s === 'close' || s.startsWith('close ') && s.length < 34) return true;
    if ((s.includes('закрыть') || s.includes('закрити')) && s.length < 40) return true;
    if (s.includes('close') && s.length < 30 && !s.includes('closed') && !s.includes('photo')) return true;
    return false;
  }
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
  for (let i = dialogs.length - 1; i >= 0; i--) {
    const d = dialogs[i];
    if (!visible(d)) continue;
    const nodes = d.querySelectorAll('[role="button"][aria-label], button[aria-label], a[role="link"][aria-label]');
    for (const b of nodes) {
      const al = (b.getAttribute('aria-label') || '').toLowerCase().trim();
      if (!al) continue;
      if (
        al === 'close' ||
        al.includes('close chat') ||
        al.includes('закрыть') ||
        al.includes('закрити') ||
        (al.includes('close') && al.length < 28)
      ) {
        if (visible(b)) {
          b.click();
          return 'dialog_close';
        }
      }
    }
  }
  const globalClose = document.querySelector(
    '[data-testid="dialog_title_close_button"], [aria-label="Close" i][role="button"], [aria-label="Закрыть"][role="button"]'
  );
  if (globalClose && visible(globalClose)) {
    globalClose.click();
    return 'global';
  }
  const candidates = document.querySelectorAll(
    '[role="button"][aria-label], div[role="button"][aria-label], span[role="button"][aria-label], a[aria-label]'
  );
  const h = window.innerHeight;
  for (const b of candidates) {
    if (!visible(b)) continue;
    const r = b.getBoundingClientRect();
    const al = (b.getAttribute('aria-label') || '').trim();
    const lowerArea = r.top > h * 0.22;
    if (!lowerArea && !looksLikeCloseChatLabel(al)) continue;
    if (looksLikeCloseChatLabel(al)) {
      b.click();
      return 'dock_' + al.slice(0, 28);
    }
  }
  return 'none';
}"""


def messenger_post_dm_close_settle_ms(*, profile_dock_or_profile_path: bool) -> int:
    """
    Пауза перед try_close_facebook_messenger_popup после отправки ЛС: клиенту Facebook
    и синхронизации списка чатов нужно время; мгновенное закрытие overlay с профиля
    часто совпадает с «сообщение ушло, в FB Master не видно».

    Переопределение: MESSENGER_POST_DM_CLOSE_DELAY_MS (целое мс), диапазон 2000–120000.
    Если не задано: ~14.5 с для профиля/док-чата, ~9 с для сценария с прямым /messages/t/…
    """
    try:
        raw = (os.environ.get("MESSENGER_POST_DM_CLOSE_DELAY_MS") or "").strip()
        if raw.isdigit():
            return max(2000, min(120_000, int(raw)))
    except Exception:
        pass
    if profile_dock_or_profile_path:
        return random.randint(7000, 15_000)
    return 9000


def try_close_facebook_messenger_popup(page: Page) -> None:
    """Закрыть всплывающее окно / док чата Messenger, чтобы не перекрывало профиль и кнопки."""
    try:
        page.wait_for_timeout(350)
        for _round in range(4):
            for _ in range(2):
                page.keyboard.press("Escape")
                page.wait_for_timeout(160)
            page.wait_for_timeout(200)
            hit = page.evaluate(_CLOSE_FB_CHAT_POPUP_JS)
            page.wait_for_timeout(260)
            if hit and hit != "none":
                page.wait_for_timeout(200)
                page.keyboard.press("Escape")
                page.wait_for_timeout(120)
            else:
                page.keyboard.press("Escape")
                page.wait_for_timeout(120)
    except Exception:
        pass


def profile_page_has_dm_entry_visible(page: Page) -> bool:
    """
    На странице профиля есть кнопка/ссылка «Сообщение» в зоне шапки (как при реальном клике).

    Не используем широкий матч по подстроке «message» — он ловит «Messenger» в верхней панели
    и даёт ложное «кнопка есть», хотя у закрытого профиля входа в ЛС с лендинга нет.
    """
    try:
        page.evaluate("() => { window.scrollTo(0, 0); }")
    except Exception:
        pass
    page.wait_for_timeout(400)
    try_close_facebook_messenger_popup(page)
    page.wait_for_timeout(200)

    try:
        if bool(page.evaluate(_PROFILE_HEADER_DM_ENTRY_PRESENT_JS)):
            return True
    except Exception:
        logger.debug("profile_page_has_dm_entry_visible: evaluate", exc_info=True)

    try:
        vp = page.viewport_size
        vh = float(vp["height"] if vp else 900)
        header_y_max = vh * 0.82
        header_y_min = max(52.0, vh * 0.06)
    except Exception:
        header_y_max = 720.0
        header_y_min = 52.0

    exact_names = (
        re.compile(r"^Сообщение$", re.I),
        re.compile(r"^Message$", re.I),
        re.compile(r"^Nachricht$", re.I),
        re.compile(r"^Envoyer un message$", re.I),
        re.compile(r"^Send message$", re.I),
        re.compile(r"^Написать сообщение$", re.I),
    )
    for role in ("button", "link"):
        for pat in exact_names:
            try:
                loc = page.get_by_role(role, name=pat)
                n = min(loc.count(), 8)
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible(timeout=900):
                            continue
                        bb = el.bounding_box()
                        y = bb.get("y", 9999) if bb else 9999
                        if bb and header_y_min <= y <= header_y_max:
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

    for lbl in (
        "Сообщение",
        "Message",
        "Nachricht",
        "Envoyer un message",
        "Send message",
        "Написать сообщение",
    ):
        loc = page.locator(f'[aria-label="{lbl}"]')
        try:
            cnt = min(loc.count(), 6)
            for i in range(cnt):
                el = loc.nth(i)
                if not el.is_visible(timeout=1000):
                    continue
                bb = el.bounding_box()
                y = bb.get("y", 9999) if bb else 9999
                if bb and header_y_min <= y <= header_y_max:
                    return True
        except Exception:
            continue

    try:
        for sel in ('a[href*="/messages/t/"]', 'a[href*="/messages/e2ee/t/"]'):
            loc = page.locator(sel)
            cnt = min(loc.count(), 8)
            for i in range(cnt):
                link = loc.nth(i)
                try:
                    if not link.is_visible(timeout=800):
                        continue
                    bb = link.bounding_box()
                    y = bb.get("y", 9999) if bb else 9999
                    if bb and header_y_min <= y <= header_y_max:
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    return False


_IS_ON_MESSAGES_NEW_DOM_JS = """() => {
  const main = document.querySelector('[role="main"]')
    || document.querySelector('[data-pagelet*="MW"]')
    || document.body;
  if (!main) return false;
  const spans = main.querySelectorAll('span, label, div');
  for (const el of spans) {
    const r = el.getBoundingClientRect();
    if (r.top > 250 || r.width < 15 || r.height < 6) continue;
    const t = (el.innerText || el.textContent || '').trim();
    if (/^(Кому|To|An|À)\\s*:?\\s*$/.test(t) && t.length < 20) return true;
  }
  const comboboxes = main.querySelectorAll('[role="combobox"]');
  for (const cb of comboboxes) {
    const r = cb.getBoundingClientRect();
    if (r.top < 250 && r.width > 100) return true;
  }
  return false;
}"""


def _is_on_messages_new(page: Page) -> bool:
    """Check if Facebook redirected us to /messages/new instead of a thread.

    Combines URL check with DOM heuristic — the URL may still show /messages/t/
    even after Facebook's client-side redirect to /messages/new.
    """
    try:
        current_url = page.url or ""
        if "/messages/new" in current_url.lower():
            return True
    except Exception:
        return False
    try:
        return bool(page.evaluate(_IS_ON_MESSAGES_NEW_DOM_JS))
    except Exception:
        return False


def _recover_messenger_thread_if_stuck_on_messages_new(
    page: Page,
    *,
    canonical_url: str,
    person_raw_meta: Mapping[str, Any] | None,
    thread_url: str | None,
    timeout_ms: int,
) -> None:
    """
    После ввода PIN / E2EE Facebook часто оставляет «Новое сообщение» без адресата.
    Повторно открываем /messages/t/… и при необходимости снова заполняем «Кому».

    Важно: URL вида /messages/new?initial_e2ee_toggle_position=true при «холодной»
    вставке в адресную строку не содержит выбранного контакта — чип «Кому» появляется
    только после клиентского перехода с /messages/t/… (кнопка «Продолжить» в треде).
    Поэтому на пустом /messages/new полагаемся на id/vanity и поле «Кому», а не на query.
    """
    if not _is_on_messages_new(page):
        return

    term = _messages_new_search_term_from_canonical_and_meta(
        canonical_url or "",
        person_raw_meta,
        thread_url=thread_url,
    )
    filled = False
    if term:
        if _messages_new_recipient_ready(page):
            logger.info(
                "Messenger /messages/new: recover — адресат уже готов, не повторяем ввод в «Кому»"
            )
            _messages_new_dismiss_autocomplete(page)
            filled = True
        else:
            filled = bool(
                _try_select_recipient_on_messages_new_page(page, term, timeout_ms=min(28_000, timeout_ms))
            )
        if filled:
            _scroll_dm_composer_candidates_into_view(page)
            page.wait_for_timeout(1600)
        for _ in range(3):
            if not try_resolve_messenger_e2ee_thread_blockers(page):
                break
            page.wait_for_timeout(380)
    if filled:
        return

    nav_to = max(15_000, int(timeout_ms))
    if thread_url:
        try:
            logger.info(
                "Messenger: /messages/new без «Кому» — повторный переход к треду %s",
                thread_url[:80],
            )
            page.goto(thread_url, wait_until="domcontentloaded", timeout=nav_to)
            page.wait_for_timeout(2200)
            for _ in range(6):
                if not try_resolve_messenger_e2ee_thread_blockers(page):
                    break
                page.wait_for_timeout(450)
        except Exception:
            logger.debug("_recover_messenger_thread_if_stuck_on_messages_new: goto", exc_info=True)

    if not _is_on_messages_new(page):
        return

    if not term:
        logger.warning(
            "Messenger: остаёмся на /messages/new без адресата; нет **числового id Facebook** для «Кому» "
            "(raw_meta: только ключи facebook_*/fb_*/messenger_thread_id; либо id в canonical_url)."
        )
        return
    if _try_select_recipient_on_messages_new_page(page, term, timeout_ms=min(28_000, timeout_ms)):
        _scroll_dm_composer_candidates_into_view(page)
        page.wait_for_timeout(1400)
    for _ in range(4):
        if not try_resolve_messenger_e2ee_thread_blockers(page):
            break
        page.wait_for_timeout(400)


def _ensure_messenger_viewport(page: Page) -> dict[str, Any] | None:
    """
    Увеличить viewport для страницы мессенджера, чтобы поле ввода сообщений
    было видимым и не обрезалось (в Electron окно часто ниже контента).
    Возвращает прежний viewport для восстановления.
    """
    try:
        if messenger_pin_restore_dialog_open(page):
            logger.info(
                "Messenger: окно PIN/E2EE — пропуск увеличения viewport и скролла (модалка уезжала вниз)"
            )
            return None
        vp = page.viewport_size
        cur_w = vp["width"] if vp else 1280
        cur_h = vp["height"] if vp else 960
        need_w = max(cur_w, 1440)
        need_h = max(cur_h, 2200)
        try:
            u = (page.url or "").lower()
            if "/messages" in u or "messenger.com" in u:
                need_h = max(need_h, 2400)
        except Exception:
            pass
        if need_h > cur_h or need_w > cur_w:
            page.set_viewport_size({"width": need_w, "height": need_h})
            page.wait_for_timeout(450)
            _scroll_dm_composer_candidates_into_view(page)
            page.wait_for_timeout(200)
            return {"width": cur_w, "height": cur_h}
    except Exception:
        logger.debug("_ensure_messenger_viewport failed", exc_info=True)
    return None


def _restore_viewport(page: Page, saved: dict[str, Any] | None) -> None:
    """Восстановить прежний viewport."""
    if not saved:
        return
    try:
        page.set_viewport_size(saved)
    except Exception:
        pass


_DM_TYPING_DELAY_MS: dict[str, tuple[int, int]] = {
    "ulitka": (18, 48),
    "slow": (12, 36),
    "medium": (7, 22),
    "fast": (3, 12),
    "very_fast": (2, 8),
}


def _type_messenger_composer_natural(
    page: Page, text: str, *, throttle_preset: str = "medium"
) -> None:
    """
    Ввод в композер Messenger через key events: неравномерные паузы и редкие «рваные»
    серии нажатий — ближе к быстрому ручному набору, чем к вставке целого шаблона.
    """
    raw = (text or "").strip()
    if not raw:
        return
    pr = (throttle_preset or "medium").strip().lower()
    if not is_valid_throttle_preset(pr):
        pr = "medium"
    lo, hi = _DM_TYPING_DELAY_MS.get(pr, _DM_TYPING_DELAY_MS["medium"])
    burst_left = 0
    for ch in raw:
        if ch == "\r":
            continue
        if ch == "\n":
            page.keyboard.press("Shift+Enter")
            page.wait_for_timeout(random.randint(45, 130))
            burst_left = 0
            continue
        page.keyboard.type(ch)
        if burst_left > 0:
            burst_left -= 1
            delay = random.randint(max(1, lo - 1), min(hi, lo + 6))
        elif random.random() < 0.26:
            burst_left = random.randint(2, 5)
            delay = random.randint(lo, hi)
        else:
            delay = random.randint(lo, hi)
        if random.random() < 0.07:
            delay += random.randint(55, 190)
        if random.random() < 0.12:
            delay = max(1, int(delay * random.uniform(0.55, 0.88)))
        page.wait_for_timeout(delay)
        if ch in ".!?…:;," and random.random() < 0.42:
            page.wait_for_timeout(random.randint(28, 120))
        if ch == " " and random.random() < 0.32:
            page.wait_for_timeout(random.randint(12, 75))


def try_send_dm_from_profile(
    page: Page,
    text: str,
    *,
    canonical_url: str | None = None,
    person_raw_meta: Mapping[str, Any] | None = None,
    timeout_ms: int = 90_000,
    throttle_preset: str = "medium",
) -> tuple[bool, str]:
    """
    Приоритет: полноэкранный Messenger — https://www.facebook.com/messages/t/<id>
    (числовой id из профиля, из ссылки-треда или из person_raw_meta). Запас: профиль → «Сообщение».

    После E2EE «Продолжить» Facebook может оставить на /messages/new — там тоже отправляем в поле внизу.

    Поле «Кому»: только числовой id **в Facebook** — из person_raw_meta по ключам facebook_page_id,
    facebook_user_id, facebook_profile_id, facebook_id, fb_page_id, fb_user_id, fb_id, fb_numeric_id,
    messenger_thread_id (не внутренний Person.id и не person_id из БД приложения). Иначе id из
    profile.php?id= или vanity из canonical_url.

    Перед закрытием overlay выполняется пауза (см. messenger_post_dm_close_settle_ms); при необходимости
    задайте MESSENGER_POST_DM_CLOSE_DELAY_MS, чтобы ускорить или ещё удлинить ожидание.
    """
    if not (text or "").strip():
        return False, "empty_dm"
    safe = text.strip()[:4000]

    page.wait_for_timeout(600)
    try:
        page.evaluate("() => { window.scrollTo(0, 0); }")
    except Exception:
        pass
    page.wait_for_timeout(400)
    try_close_facebook_messenger_popup(page)
    page.wait_for_timeout(450)

    thread_url = dm_direct_thread_url_for_person(canonical_url, person_raw_meta)
    opened = False
    used_direct_thread = False
    saved_vp: dict[str, Any] | None = None

    if thread_url:
        try:
            page.goto(thread_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)

            if _is_on_messages_new(page):
                if _dm_page_looks_restricted(page):
                    opened = False
                else:
                    # Сразу /messages/new (E2EE и т.п.) — остаёмся в Messenger, не уходим на профиль.
                    opened = True
                    used_direct_thread = True
            else:
                page.wait_for_timeout(1500)
                if _is_on_messages_new(page):
                    if _dm_page_looks_restricted(page):
                        opened = False
                    else:
                        opened = True
                        used_direct_thread = True
                elif _dm_page_looks_restricted(page):
                    opened = False
                else:
                    opened = True
                    used_direct_thread = True
        except Exception:
            opened = False

    if not opened:
        prof = profile_url_from_person((canonical_url or "").strip()) if canonical_url else ""
        if prof:
            try:
                page.goto(prof, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1200)
            except Exception:
                pass
            if _try_click_message_entry_on_profile(page):
                opened = True
                used_direct_thread = False

    if not opened:
        return False, "message_button_not_found"

    sent_via_profile_popup_fallback = False
    try:
        # Увеличиваем viewport, чтобы поле ввода сообщений было видимым
        saved_vp = _ensure_messenger_viewport(page)

        if not used_direct_thread:
            page.wait_for_timeout(2200)
        else:
            page.wait_for_timeout(1200)

        def _url_or_dom_is_messages_new() -> bool:
            try:
                if "/messages/new" in (page.url or "").lower():
                    return True
            except Exception:
                pass
            return bool(_is_on_messages_new(page))

        # Сначала E2EE: на /messages/t часто синяя «Продолжить», после клика — редирект на /messages/new.
        for _e2ee in range(14):
            did = try_resolve_messenger_e2ee_thread_blockers(page)
            if not did:
                break
            page.wait_for_timeout(450)
            try:
                if "/messages/new" in (page.url or "").lower():
                    if _messages_new_recipient_ready(page):
                        _messages_new_dismiss_autocomplete(page)
                    else:
                        _recover_messenger_thread_if_stuck_on_messages_new(
                            page,
                            canonical_url=canonical_url or "",
                            person_raw_meta=person_raw_meta,
                            thread_url=thread_url,
                            timeout_ms=min(35_000, timeout_ms),
                        )
            except Exception:
                logger.debug("recover after E2EE iter (14)", exc_info=True)

        # «Новое сообщение»: при необходимости дополняем «Кому» (на E2EE-редиректе адресат уже может быть выбран).
        if _url_or_dom_is_messages_new():
            term = _messages_new_search_term_from_canonical_and_meta(
                canonical_url or "",
                person_raw_meta,
                thread_url=thread_url,
            )
            if term:
                filled_ok = False
                if _messages_new_recipient_ready(page):
                    logger.info(
                        "Messenger /messages/new: «Кому» уже заполнен до цикла попыток — не дублируем ввод"
                    )
                    _messages_new_dismiss_autocomplete(page)
                    filled_ok = True
                else:
                    for rid in range(5):
                        if _try_select_recipient_on_messages_new_page(
                            page, term, timeout_ms=min(28_000, timeout_ms)
                        ):
                            filled_ok = True
                            break
                        if rid < 4:
                            logger.info(
                                "Messenger /messages/new: повтор заполнения «Кому» (попытка %s/5)",
                                rid + 2,
                            )
                            page.wait_for_timeout(1800)
                            _ensure_messenger_viewport(page)
                            _scroll_dm_composer_candidates_into_view(page)
                if filled_ok:
                    _scroll_dm_composer_candidates_into_view(page)
                    page.wait_for_timeout(1600)
                    _ensure_messenger_viewport(page)
            else:
                logger.warning(
                    "Messenger /messages/new: пропуск ввода в «Кому» — в person.raw_meta нет числового id "
                    "**Facebook** (только ключи: facebook_page_id, facebook_user_id, facebook_profile_id, "
                    "facebook_id, fb_page_id, fb_user_id, fb_id, fb_numeric_id, messenger_thread_id; "
                    "без person_id / id записи в БД) и не извлечь id из canonical_url или thread_url."
                )
                page.wait_for_timeout(900)

        for _e2ee in range(8):
            did = try_resolve_messenger_e2ee_thread_blockers(page)
            if not did:
                break
            page.wait_for_timeout(400)
            try:
                if "/messages/new" in (page.url or "").lower():
                    if _messages_new_recipient_ready(page):
                        _messages_new_dismiss_autocomplete(page)
                    else:
                        _recover_messenger_thread_if_stuck_on_messages_new(
                            page,
                            canonical_url=canonical_url or "",
                            person_raw_meta=person_raw_meta,
                            thread_url=thread_url,
                            timeout_ms=min(35_000, timeout_ms),
                        )
            except Exception:
                logger.debug("recover after E2EE iter (8)", exc_info=True)

        _recover_messenger_thread_if_stuck_on_messages_new(
            page,
            canonical_url=canonical_url or "",
            person_raw_meta=person_raw_meta,
            thread_url=thread_url,
            timeout_ms=min(35_000, timeout_ms),
        )

        _ensure_messenger_viewport(page)
        _scroll_dm_composer_candidates_into_view(page)
        page.wait_for_timeout(350)

        dismiss_messenger_first_chat_start_gate_if_present(
            page, max_passes=3, log_label="try_send_dm_from_profile"
        )
        page.wait_for_timeout(400)

        _selector_set = '[contenteditable="true"], textarea[placeholder], div[role="textbox"], [data-lexical-editor="true"]'
        try:
            page.wait_for_selector(_selector_set, timeout=min(26_000, timeout_ms))
        except Exception:
            pass

        pick: dict[str, Any] | None = None
        did_composer_mid_recovery = False
        for attempt in range(5):
            if attempt < 3:
                dismiss_messenger_first_chat_start_gate_if_present(
                    page, max_passes=2, log_label="try_send_dm_from_profile"
                )
            try_resolve_messenger_e2ee_thread_blockers(page)
            _scroll_dm_composer_candidates_into_view(page)
            page.wait_for_timeout(380)
            try:
                if "/messages/new" in (page.url or "").lower():
                    if _messages_new_recipient_ready(page):
                        _messages_new_dismiss_autocomplete(page)
                    else:
                        _recover_messenger_thread_if_stuck_on_messages_new(
                            page,
                            canonical_url=canonical_url or "",
                            person_raw_meta=person_raw_meta,
                            thread_url=thread_url,
                            timeout_ms=min(35_000, timeout_ms),
                        )
            except Exception:
                logger.debug("recover in dm composer loop", exc_info=True)
            try:
                path = page.evaluate("() => window.location.pathname || ''")
            except Exception:
                path = ""

            if (
                attempt >= 2
                and not did_composer_mid_recovery
                and isinstance(path, str)
                and "/messages/new" in path.lower()
            ):
                if _messages_new_recipient_ready(page):
                    _messages_new_dismiss_autocomplete(page)
                else:
                    _recover_messenger_thread_if_stuck_on_messages_new(
                        page,
                        canonical_url=canonical_url or "",
                        person_raw_meta=person_raw_meta,
                        thread_url=thread_url,
                        timeout_ms=min(35_000, timeout_ms),
                    )
                did_composer_mid_recovery = True
                try:
                    path = page.evaluate("() => window.location.pathname || ''")
                except Exception:
                    path = ""

            pick = page.evaluate(_DM_COMPOSER_PICK_JS, path)
            if isinstance(pick, dict) and pick.get("ok"):
                break
            page.wait_for_timeout(1400)

        if not isinstance(pick, dict) or not pick.get("ok"):
            try:
                vp = page.viewport_size
                cur_h = vp["height"] if vp else 960
                new_h = max(cur_h + 300, 1400)
                page.set_viewport_size({"width": vp["width"] if vp else 1280, "height": new_h})
                page.wait_for_timeout(1200)
                _scroll_dm_composer_candidates_into_view(page)
                page.wait_for_timeout(600)
                try:
                    path = page.evaluate("() => window.location.pathname || ''")
                except Exception:
                    path = ""
                pick = page.evaluate(_DM_COMPOSER_PICK_JS, path)
                if not (isinstance(pick, dict) and pick.get("ok")):
                    page.set_viewport_size({"width": vp["width"] if vp else 1280, "height": cur_h})
            except Exception:
                pass

        if not isinstance(pick, dict) or not pick.get("ok"):
            if page_shows_messenger_message_request_limit(page):
                return False, "facebook_message_request_limit"
            reason = str((pick or {}).get("reason", "fail"))
            if _dm_page_looks_restricted(page):
                return False, f"dm_restricted_or_no_access:{reason}"
            # ── Fallback: popup-чат на профиле (кнопка «Сообщение» → маленькое окно) ──
            logger.info(
                "try_send_dm_from_profile: DM composer не найден в Messenger; "
                "fallback → popup-чат на профиле (canonical=%s)",
                (canonical_url or "")[:80],
            )
            sent_via_profile_popup_fallback = True
            popup_ok, popup_msg = _try_send_via_profile_popup_chat(
                page,
                safe,
                canonical_url=canonical_url,
                timeout_ms=timeout_ms,
                throttle_preset=throttle_preset,
            )
            if popup_ok:
                return True, popup_msg
            if popup_msg == "facebook_message_request_limit":
                return False, "facebook_message_request_limit"
            return False, f"dm_composer_not_found:{reason};popup_fallback:{popup_msg}"

        # Прокрутить к полю ввода, чтобы пользователь видел в Electron
        try:
            page.evaluate("""() => {
                const active = document.activeElement;
                if (active) {
                    active.scrollIntoView({ block: 'center', behavior: 'smooth' });
                }
            }""")
            page.wait_for_timeout(300)
        except Exception:
            pass

        # Verify the focused field is NOT the recipient field
        try:
            focused_is_recipient = page.evaluate("""() => {
              const el = document.activeElement;
              if (!el) return false;
              const cb = el.closest('[role="combobox"], [role="search"]');
              if (cb) return true;
              const r = el.getBoundingClientRect();
              if (r.top < 200) {
                const ph = ((el.getAttribute('aria-placeholder') || '')
                  + ' ' + (el.getAttribute('placeholder') || '')
                  + ' ' + (el.getAttribute('data-placeholder') || '')
                  + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
                const isMsgLike = ph.includes('сообщ') || ph.includes('message')
                  || ph.includes('aa') || ph.includes('напиш')
                  || ph.includes('type') || ph.includes('write');
                if (!isMsgLike) return true;
              }
              return false;
            }""")
            if focused_is_recipient:
                logger.warning("try_send_dm_from_profile: focused field looks like recipient input — fallback popup")
                sent_via_profile_popup_fallback = True
                popup_ok, popup_msg = _try_send_via_profile_popup_chat(
                    page,
                    safe,
                    canonical_url=canonical_url,
                    timeout_ms=timeout_ms,
                    throttle_preset=throttle_preset,
                )
                if popup_ok:
                    return True, popup_msg
                if popup_msg == "facebook_message_request_limit":
                    return False, "facebook_message_request_limit"
                return False, f"focused_on_recipient_field;popup_fallback:{popup_msg}"
        except Exception:
            pass

        try:
            _type_messenger_composer_natural(page, safe, throttle_preset=throttle_preset)
        except Exception as e:
            return False, f"type:{e!s}"[:120]

        page.wait_for_timeout(450)

        ok_clear, vreason = _submit_messenger_composer_after_typing(page, safe)
        if not ok_clear:
            if page_shows_messenger_message_request_limit(page):
                return False, "facebook_message_request_limit"
            return False, vreason[:200]

        page.wait_for_timeout(600)

        return True, "dm_sent"
    finally:
        _restore_viewport(page, saved_vp)
        _reset_fb_chat_dock_lift(page)
        long_settle = (not used_direct_thread) or sent_via_profile_popup_fallback
        settle_ms = messenger_post_dm_close_settle_ms(profile_dock_or_profile_path=long_settle)
        logger.info(
            "Messenger: пауза %s мс перед закрытием overlay (direct_thread=%s, popup_fallback=%s)",
            settle_ms,
            used_direct_thread,
            sent_via_profile_popup_fallback,
        )
        try:
            page.wait_for_timeout(settle_ms)
        except Exception:
            pass
        try_close_facebook_messenger_popup(page)
        page.wait_for_timeout(350)


def try_send_dm_on_open_thread_page(
    page: Page,
    text: str,
    *,
    timeout_ms: int = 90_000,
    peer_url: str | None = None,
    throttle_preset: str = "medium",
) -> tuple[bool, str]:
    """
    Уже открыта переписка /messages/t/… (после open_thread).
    После попытки отправки закрывает всплывающие панели чата (как и с профиля), чтобы не мешали дальнейшей автоматизации;
    перед закрытием — пауза (messenger_post_dm_close_settle_ms), чтобы синхронизация успела подтянуть чат.

    peer_url — если Facebook показал /messages/new, по URL треда можно взять числовой id для поля «Кому».
    """
    if not (text or "").strip():
        return False, "empty_dm"
    safe = text.strip()[:4000]
    saved_vp: dict[str, Any] | None = None
    try:
        saved_vp = _ensure_messenger_viewport(page)

        page.wait_for_timeout(900)

        def _open_thread_url_or_dom_is_messages_new() -> bool:
            try:
                if "/messages/new" in (page.url or "").lower():
                    return True
            except Exception:
                pass
            return bool(_is_on_messages_new(page))

        peer = (peer_url or "").strip()
        thread_goto = dm_thread_url_from_canonical(peer) if peer else None

        for _e2ee in range(14):
            did = try_resolve_messenger_e2ee_thread_blockers(page)
            if not did:
                break
            page.wait_for_timeout(450)
            try:
                if "/messages/new" in (page.url or "").lower():
                    if _messages_new_recipient_ready(page):
                        _messages_new_dismiss_autocomplete(page)
                    else:
                        _recover_messenger_thread_if_stuck_on_messages_new(
                            page,
                            canonical_url=peer,
                            person_raw_meta=None,
                            thread_url=thread_goto,
                            timeout_ms=min(35_000, timeout_ms),
                        )
            except Exception:
                logger.debug("recover after E2EE iter open_thread (14)", exc_info=True)

        if _open_thread_url_or_dom_is_messages_new():
            hint = _search_hint_from_peer_or_profile_url(peer_url)
            if hint:
                if not _messages_new_recipient_ready(page):
                    _try_select_recipient_on_messages_new_page(page, hint, timeout_ms=min(28_000, timeout_ms))
                else:
                    logger.info(
                        "Messenger try_send_dm_on_open_thread_page: «Кому» уже готов — пропуск повторного ввода"
                    )
                    _messages_new_dismiss_autocomplete(page)
                _scroll_dm_composer_candidates_into_view(page)
                page.wait_for_timeout(1600)
            else:
                page.wait_for_timeout(900)

        for _e2ee in range(8):
            did = try_resolve_messenger_e2ee_thread_blockers(page)
            if not did:
                break
            page.wait_for_timeout(400)
            try:
                if "/messages/new" in (page.url or "").lower():
                    if _messages_new_recipient_ready(page):
                        _messages_new_dismiss_autocomplete(page)
                    else:
                        _recover_messenger_thread_if_stuck_on_messages_new(
                            page,
                            canonical_url=peer,
                            person_raw_meta=None,
                            thread_url=thread_goto,
                            timeout_ms=min(35_000, timeout_ms),
                        )
            except Exception:
                logger.debug("recover after E2EE iter open_thread (8)", exc_info=True)

        _recover_messenger_thread_if_stuck_on_messages_new(
            page,
            canonical_url=peer,
            person_raw_meta=None,
            thread_url=thread_goto,
            timeout_ms=min(35_000, timeout_ms),
        )

        # Увеличиваем viewport для видимости поля ввода
        dismiss_messenger_first_chat_start_gate_if_present(
            page, max_passes=3, log_label="try_send_dm_on_open_thread_page"
        )
        page.wait_for_timeout(400)

        _selector_set = '[contenteditable="true"], textarea[placeholder], div[role="textbox"], [data-lexical-editor="true"]'
        try:
            page.wait_for_selector(_selector_set, timeout=min(26_000, timeout_ms))
        except Exception:
            pass
        pick: dict[str, Any] | None = None
        did_open_thread_mid_recovery = False
        for _attempt in range(6):
            if _attempt < 4:
                dismiss_messenger_first_chat_start_gate_if_present(
                    page, max_passes=2, log_label="try_send_dm_on_open_thread_page"
                )
            try_resolve_messenger_e2ee_thread_blockers(page)
            _scroll_dm_composer_candidates_into_view(page)
            page.wait_for_timeout(380)
            try:
                if "/messages/new" in (page.url or "").lower():
                    if _messages_new_recipient_ready(page):
                        _messages_new_dismiss_autocomplete(page)
                    else:
                        _recover_messenger_thread_if_stuck_on_messages_new(
                            page,
                            canonical_url=peer,
                            person_raw_meta=None,
                            thread_url=thread_goto,
                            timeout_ms=min(35_000, timeout_ms),
                        )
            except Exception:
                logger.debug("recover in open_thread composer loop", exc_info=True)
            try:
                path = page.evaluate("() => window.location.pathname || ''")
            except Exception:
                path = ""

            if (
                _attempt >= 2
                and not did_open_thread_mid_recovery
                and isinstance(path, str)
                and "/messages/new" in path.lower()
            ):
                if _messages_new_recipient_ready(page):
                    _messages_new_dismiss_autocomplete(page)
                else:
                    _recover_messenger_thread_if_stuck_on_messages_new(
                        page,
                        canonical_url=peer,
                        person_raw_meta=None,
                        thread_url=thread_goto,
                        timeout_ms=min(35_000, timeout_ms),
                    )
                did_open_thread_mid_recovery = True
                try:
                    path = page.evaluate("() => window.location.pathname || ''")
                except Exception:
                    path = ""

            pick = page.evaluate(_DM_COMPOSER_PICK_JS, path)
            if isinstance(pick, dict) and pick.get("ok"):
                break
            page.wait_for_timeout(1600)
        if not isinstance(pick, dict) or not pick.get("ok"):
            try:
                vp = page.viewport_size
                cur_h = vp["height"] if vp else 960
                new_h = max(cur_h + 300, 1400)
                page.set_viewport_size({"width": vp["width"] if vp else 1280, "height": new_h})
                page.wait_for_timeout(1200)
                _scroll_dm_composer_candidates_into_view(page)
                page.wait_for_timeout(600)
                try:
                    path = page.evaluate("() => window.location.pathname || ''")
                except Exception:
                    path = ""
                pick = page.evaluate(_DM_COMPOSER_PICK_JS, path)
                if not (isinstance(pick, dict) and pick.get("ok")):
                    page.set_viewport_size({"width": vp["width"] if vp else 1280, "height": cur_h})
            except Exception:
                pass
        if not isinstance(pick, dict) or not pick.get("ok"):
            if page_shows_messenger_message_request_limit(page):
                return False, "facebook_message_request_limit"
            reason = str((pick or {}).get("reason", "fail"))
            if _dm_page_looks_restricted(page):
                return False, f"dm_restricted_or_no_access:{reason}"
            return False, f"dm_composer_not_found:{reason}"

        # Прокрутить к полю ввода, чтобы пользователь видел в Electron
        try:
            page.evaluate("""() => {
                const active = document.activeElement;
                if (active) {
                    active.scrollIntoView({ block: 'center', behavior: 'smooth' });
                }
            }""")
            page.wait_for_timeout(300)
        except Exception:
            pass

        try:
            focused_is_recipient = page.evaluate("""() => {
              const el = document.activeElement;
              if (!el) return false;
              const cb = el.closest('[role="combobox"], [role="search"]');
              if (cb) return true;
              const r = el.getBoundingClientRect();
              if (r.top < 200) {
                const ph = ((el.getAttribute('aria-placeholder') || '')
                  + ' ' + (el.getAttribute('placeholder') || '')
                  + ' ' + (el.getAttribute('data-placeholder') || '')
                  + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
                const isMsgLike = ph.includes('сообщ') || ph.includes('message')
                  || ph.includes('aa') || ph.includes('напиш')
                  || ph.includes('type') || ph.includes('write');
                if (!isMsgLike) return true;
              }
              return false;
            }""")
            if focused_is_recipient:
                logger.warning("try_send_dm_on_open_thread_page: focused on recipient field")
                return False, "focused_on_recipient_field"
        except Exception:
            pass

        try:
            _type_messenger_composer_natural(page, safe, throttle_preset=throttle_preset)
        except Exception as e:
            return False, f"type:{e!s}"[:120]
        page.wait_for_timeout(500)

        ok_clear, vreason = _submit_messenger_composer_after_typing(page, safe)
        if not ok_clear:
            if page_shows_messenger_message_request_limit(page):
                return False, "facebook_message_request_limit"
            return False, vreason[:200]

        page.wait_for_timeout(600)

        page.wait_for_timeout(300)
        return True, "dm_sent"
    finally:
        _restore_viewport(page, saved_vp)
        _reset_fb_chat_dock_lift(page)
        settle_ms = messenger_post_dm_close_settle_ms(profile_dock_or_profile_path=False)
        logger.info(
            "Messenger (open_thread): пауза %s мс перед закрытием overlay",
            settle_ms,
        )
        try:
            page.wait_for_timeout(settle_ms)
        except Exception:
            pass
        try_close_facebook_messenger_popup(page)
        page.wait_for_timeout(250)


def _like_post_phase(
    page: Page,
    cfg: dict[str, Any],
    *,
    action_key: str,
    profile_url: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    mode = (cfg.get("post_like_mode") or "first").strip().lower()
    if mode not in ("first", "random"):
        mode = "first"
    pool_size = max(1, min(int(cfg.get("post_pool_size") or 5), 10))
    count = max(1, min(int(cfg.get("like_count") or 1), 10))
    skip = max(0, min(int(cfg.get("post_skip_likes") or 0), 5))
    ok, msg = try_like_posts_on_profile(
        page,
        mode=mode,
        pool_size=pool_size,
        count=count,
        skip_first=skip,
        timeout_ms=25_000,
    )
    detail = f"like_post:{msg}"
    return ok, detail, None if ok else _step_debug(
        action_key,
        profile_url,
        cfg,
        failure_stage="like_post",
        like_mode=mode,
        pool_size=pool_size,
        like_count=count,
        skip_first=skip,
        result_message=detail,
    )


def _comment_target_settings(cfg: dict[str, Any]) -> tuple[str, int]:
    mode = (cfg.get("comment_post_mode") or "first").strip().lower()
    if mode not in ("first", "random"):
        mode = "first"
    try:
        pool_size = max(1, min(int(cfg.get("comment_pool_size") or 5), 10))
    except (TypeError, ValueError):
        pool_size = 5
    return mode, pool_size


def _resolve_comment_text(
    db: Session,
    page: Page,
    *,
    profile_url: str,
    action_key: str,
    cfg: dict[str, Any],
    display_name: str,
    first_name: str,
    comment_mode: str | None = None,
    comment_pool_size: int | None = None,
    comment_pick_index: int = 0,
) -> tuple[bool, str, dict[str, Any] | None]:
    if comment_mode is None or comment_pool_size is None:
        comment_mode, comment_pool_size = _comment_target_settings(cfg)
    if cfg.get("ai_comment"):
        post_body = extract_timeline_post_text(
            page,
            mode=comment_mode,
            pool_size=comment_pool_size,
            pick_index=comment_pick_index,
        )
        pb = (post_body or "").strip()
        if len(pb) < 12:
            pb = (
                "[Пост в ленте профиля: мало извлекаемого текста или в основном медиа — "
                "нужен короткий нейтральный доброжелательный комментарий без выдуманных фактов.]"
            )
        hint = str(cfg.get("ai_comment_hint") or "").strip()
        base_debug = _step_debug(
            action_key,
            profile_url,
            cfg,
            failure_stage="ai_comment_generation",
            ai_comment=True,
            comment_post_mode=comment_mode,
            comment_pool_size=comment_pool_size,
            comment_pick_index=comment_pick_index,
            ai_hint=hint,
            post_excerpt=pb,
        )
        try:
            txt = generate_profile_post_comment_sync(
                db,
                post_text=pb,
                display_name=display_name,
                first_name=first_name,
                hint=hint,
            )
        except LLMError as e:
            return False, f"ai_llm:{e!s}"[:200], base_debug
        except Exception as e:
            logger.exception("ai_comment_post")
            return False, f"ai_error:{e!s}"[:200], base_debug
        if not (txt or "").strip():
            return False, "ai_empty_comment", base_debug
        return True, txt, None

    txt = _text_from_config(db, cfg, person_display=display_name, person_first=first_name)
    if not txt:
        return False, "no_comment_text", _step_debug(
            action_key,
            profile_url,
            cfg,
            failure_stage="comment_text_resolution",
            ai_comment=False,
            comment_post_mode=comment_mode,
            comment_pool_size=comment_pool_size,
            comment_pick_index=comment_pick_index,
        )
    return True, txt, None


def _comment_phase(
    db: Session,
    page: Page,
    *,
    profile_url: str,
    action_key: str,
    cfg: dict[str, Any],
    display_name: str,
    first_name: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    comment_mode, comment_pool_size = _comment_target_settings(cfg)
    comment_pick_index = 0
    if comment_mode == "random":
        comment_pick_index = random.randrange(comment_pool_size)
    text_ok, comment_text, debug = _resolve_comment_text(
        db,
        page,
        profile_url=profile_url,
        action_key=action_key,
        cfg=cfg,
        display_name=display_name,
        first_name=first_name,
        comment_mode=comment_mode,
        comment_pool_size=comment_pool_size,
        comment_pick_index=comment_pick_index,
    )
    if not text_ok:
        return False, comment_text, debug
    ok, msg = try_comment_on_timeline(
        page,
        comment_text,
        mode=comment_mode,
        pool_size=comment_pool_size,
        pick_index=comment_pick_index,
    )
    return ok, msg, None if ok else _step_debug(
        action_key,
        profile_url,
        cfg,
        failure_stage="facebook_comment_submit",
        ai_comment=bool(cfg.get("ai_comment")),
        comment_post_mode=comment_mode,
        comment_pool_size=comment_pool_size,
        comment_pick_index=comment_pick_index,
        comment_text=comment_text,
        result_message=msg,
    )


def _dm_text_from_config(
    db: Session,
    cfg: dict[str, Any] | None,
    *,
    person_display: str,
    person_first: str,
) -> str:
    c = cfg if isinstance(cfg, dict) else {}
    raw = ""
    separate_dm = bool(c.get("dm_separate"))
    template_id = c.get("dm_template_id")
    if template_id is None and not separate_dm:
        template_id = c.get("template_id")
    if str(template_id or "").strip().isdigit():
        row = db.get(Template, int(template_id))
        if row:
            raw = resolve_template_variant(row.body or "", db=db, template_row=row).strip()
    if not raw:
        raw = (c.get("dm_message") or "").strip()
        if not raw and not separate_dm:
            raw = (c.get("message") or "").strip()
        raw = resolve_template_variant(raw, db=None, template_row=None)
    disp = person_display or "друг"
    return apply_template_placeholders(
        raw,
        full_name=disp,
        first_name=person_first or disp,
    )


def _resolve_dm_text(
    db: Session,
    page: Page,
    *,
    profile_url: str,
    action_key: str,
    cfg: dict[str, Any],
    display_name: str,
    first_name: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    if cfg.get("ai_dm"):
        profile_ctx = ""
        pu = (profile_url or "").strip()
        if pu.startswith("http"):
            try:
                page.goto(pu, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(900)
            except Exception:
                logger.debug("_resolve_dm_text: goto profile for ai_dm failed", exc_info=True)
        if cfg.get("ai_dm_use_profile_post", True):
            profile_ctx = extract_timeline_post_text(page)
        dm_loc = infer_dm_output_locale_for_ai_dm(
            page,
            profile_content_sample=profile_ctx,
            recipient_label=f"{display_name}\n{first_name}".strip(),
        )
        hint = str(cfg.get("ai_dm_hint") or "").strip()
        ref = _reference_text_for_ai_dm(
            db,
            cfg,
            person_display=display_name,
            person_first=first_name,
        )
        base_debug = _step_debug(
            action_key,
            profile_url,
            cfg,
            failure_stage="ai_dm_generation",
            ai_dm=True,
            ai_hint=hint,
            reference_message=ref,
            profile_context_excerpt=profile_ctx,
        )
        try:
            txt = generate_dm_message_sync(
                db,
                display_name=display_name,
                first_name=first_name,
                hint=hint,
                reference_message=ref,
                profile_context=profile_ctx if len((profile_ctx or "").strip()) >= 12 else "",
                dm_output_locale=dm_loc,
            )
        except LLMError as e:
            return False, f"ai_llm:{e!s}"[:200], base_debug
        except Exception as e:
            logger.exception("ai_dm_send")
            return False, f"ai_error:{e!s}"[:200], base_debug
        if not (txt or "").strip():
            return False, "ai_empty_dm", base_debug
        return True, txt, None

    txt = _dm_text_from_config(db, cfg, person_display=display_name, person_first=first_name)
    if not txt:
        return False, "no_dm_text", _step_debug(
            action_key,
            profile_url,
            cfg,
            failure_stage="dm_text_resolution",
            ai_dm=False,
        )
    return True, txt, None


def execute_sequence_step(
    db: Session,
    page: Page,
    *,
    person_canonical_url: str,
    action_type: str,
    step_config: dict[str, Any] | None,
    display_name: str,
    first_name: str,
    step_side_effect: dict[str, Any] | None = None,
    throttle_preset: str = "medium",
    person_raw_meta: Mapping[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    action_type: like_post | comment_post | friend_request | send_dm | combo-actions
    step_side_effect: если передан, при успешной отправке ЛС заполняется ключ outbound_dm_text (для записи диалога в кабинете).
    """
    profile_url = profile_url_from_person(person_canonical_url)
    at = (action_type or "").strip().lower().replace("-", "_")

    if at == "like_post":
        cfg = step_config if isinstance(step_config, dict) else {}
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, cfg, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        return _like_post_phase(page, cfg, action_key=at, profile_url=profile_url)

    if at == "comment_post":
        cfg = step_config if isinstance(step_config, dict) else {}
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, cfg, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        return _comment_phase(
            db,
            page,
            profile_url=profile_url,
            action_key=at,
            cfg=cfg,
            display_name=display_name,
            first_name=first_name,
        )

    if at == "friend_request":
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, step_config, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        ok, msg = try_friend_request_on_profile(page)
        return ok, msg, None if ok else _step_debug(
            at,
            profile_url,
            step_config,
            failure_stage="friend_request",
            result_message=msg,
        )

    if at in ("send_dm", "open_dm"):
        cfg = step_config if isinstance(step_config, dict) else {}
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, cfg, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        if not profile_page_has_dm_entry_visible(page):
            return False, "message_button_not_found", _step_debug(
                at,
                profile_url,
                cfg,
                failure_stage="profile_no_message_button",
                result_message="precheck",
            )
        text_ok, txt, debug = _resolve_dm_text(
            db,
            page,
            profile_url=profile_url,
            action_key=at,
            cfg=cfg,
            display_name=display_name,
            first_name=first_name,
        )
        if not text_ok:
            return False, txt, debug
        ok, msg = try_send_dm_from_profile(
            page,
            txt,
            canonical_url=person_canonical_url,
            person_raw_meta=person_raw_meta,
            throttle_preset=throttle_preset,
        )
        if ok and step_side_effect is not None:
            step_side_effect["outbound_dm_text"] = txt
        return ok, msg, None if ok else _step_debug(
            at,
            profile_url,
            cfg,
            failure_stage="facebook_dm_send",
            ai_dm=bool(cfg.get("ai_dm")),
            message_text=txt,
            result_message=msg,
        )

    if at in ("like_and_comment", "like_comment"):
        cfg = step_config if isinstance(step_config, dict) else {}
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, cfg, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        lk_ok, lk_msg, _lk_debug = _like_post_phase(
            page,
            cfg,
            action_key=at,
            profile_url=profile_url,
        )
        humanize_between_like_and_comment(page, throttle_preset)
        ck_ok, ck_msg, ck_debug = _comment_phase(
            db,
            page,
            profile_url=profile_url,
            action_key=at,
            cfg=cfg,
            display_name=display_name,
            first_name=first_name,
        )
        if lk_ok and ck_ok:
            return True, f"like_and_comment:like={lk_msg};comment={ck_msg}", None
        return False, (
            f"like_and_comment:like={'ok' if lk_ok else 'fail'}:{lk_msg};"
            f"comment={'ok' if ck_ok else 'fail'}:{ck_msg}"
        ), _step_debug(
            at,
            profile_url,
            cfg,
            failure_stage="like_and_comment",
            ai_comment=bool(cfg.get("ai_comment")),
            like_ok=lk_ok,
            like_message=lk_msg,
            comment_ok=ck_ok,
            comment_message=ck_msg,
            comment_debug=ck_debug,
        )

    if at == "like_comment_friend_request":
        cfg = step_config if isinstance(step_config, dict) else {}
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, cfg, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        lk_ok, lk_msg, _lk_debug = _like_post_phase(
            page,
            cfg,
            action_key=at,
            profile_url=profile_url,
        )
        humanize_between_like_and_comment(page, throttle_preset)
        ck_ok, ck_msg, ck_debug = _comment_phase(
            db,
            page,
            profile_url=profile_url,
            action_key=at,
            cfg=cfg,
            display_name=display_name,
            first_name=first_name,
        )
        humanize_between_comment_and_friend(page, throttle_preset)
        fr_ok, fr_msg = try_friend_request_on_profile(page)
        if lk_ok and ck_ok and fr_ok:
            return True, f"like_comment_friend_request:like={lk_msg};comment={ck_msg};friend={fr_msg}", None
        return False, (
            f"like_comment_friend_request:like={'ok' if lk_ok else 'fail'}:{lk_msg};"
            f"comment={'ok' if ck_ok else 'fail'}:{ck_msg};"
            f"friend={'ok' if fr_ok else 'fail'}:{fr_msg}"
        ), _step_debug(
            at,
            profile_url,
            cfg,
            failure_stage="like_comment_friend_request",
            ai_comment=bool(cfg.get("ai_comment")),
            like_ok=lk_ok,
            like_message=lk_msg,
            comment_ok=ck_ok,
            comment_message=ck_msg,
            friend_ok=fr_ok,
            friend_message=fr_msg,
            comment_debug=ck_debug,
        )

    if at == "like_comment_friend_request_send_dm":
        cfg = step_config if isinstance(step_config, dict) else {}
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            return False, f"nav:{e!s}"[:200], _step_debug(at, profile_url, cfg, failure_stage="navigation")
        humanize_for_sequence_action(page, throttle_preset, at)
        if not profile_page_has_dm_entry_visible(page):
            return False, "message_button_not_found", _step_debug(
                at,
                profile_url,
                cfg,
                failure_stage="profile_no_message_button",
                result_message="precheck",
            )
        lk_ok, lk_msg, _lk_debug = _like_post_phase(
            page,
            cfg,
            action_key=at,
            profile_url=profile_url,
        )
        humanize_between_like_and_comment(page, throttle_preset)
        ck_ok, ck_msg, ck_debug = _comment_phase(
            db,
            page,
            profile_url=profile_url,
            action_key=at,
            cfg=cfg,
            display_name=display_name,
            first_name=first_name,
        )
        humanize_between_comment_and_friend(page, throttle_preset)
        fr_ok, fr_msg = try_friend_request_on_profile(page)
        humanize_between_friend_and_dm(page, throttle_preset)
        dm_ok = False
        dm_msg = ""
        dm_debug: dict[str, Any] | None = None
        dm_text_or_detail = ""
        dm_text_ok, dm_text_or_detail, dm_debug = _resolve_dm_text(
            db,
            page,
            profile_url=profile_url,
            action_key=at,
            cfg=cfg,
            display_name=display_name,
            first_name=first_name,
        )
        if dm_text_ok:
            dm_ok, dm_msg = try_send_dm_from_profile(
                page,
                dm_text_or_detail,
                canonical_url=person_canonical_url,
                person_raw_meta=person_raw_meta,
                throttle_preset=throttle_preset,
            )
        else:
            dm_msg = dm_text_or_detail
        if lk_ok and ck_ok and fr_ok and dm_ok:
            if step_side_effect is not None:
                step_side_effect["outbound_dm_text"] = dm_text_or_detail
            return True, (
                f"like_comment_friend_request_send_dm:like={lk_msg};comment={ck_msg};"
                f"friend={fr_msg};dm={dm_msg}"
            ), None
        return False, (
            f"like_comment_friend_request_send_dm:like={'ok' if lk_ok else 'fail'}:{lk_msg};"
            f"comment={'ok' if ck_ok else 'fail'}:{ck_msg};"
            f"friend={'ok' if fr_ok else 'fail'}:{fr_msg};"
            f"dm={'ok' if dm_ok else 'fail'}:{dm_msg}"
        ), _step_debug(
            at,
            profile_url,
            cfg,
            failure_stage="like_comment_friend_request_send_dm",
            ai_comment=bool(cfg.get("ai_comment")),
            ai_dm=bool(cfg.get("ai_dm")),
            like_ok=lk_ok,
            like_message=lk_msg,
            comment_ok=ck_ok,
            comment_message=ck_msg,
            friend_ok=fr_ok,
            friend_message=fr_msg,
            dm_ok=dm_ok,
            dm_message=dm_msg,
            comment_debug=ck_debug,
            dm_debug=dm_debug,
        )

    return False, f"unknown_action:{action_type}", _step_debug(
        at,
        profile_url,
        step_config,
        failure_stage="unknown_action",
    )


def throttle_action_for_step(action_type: str) -> str:
    """Ключ для resolve_delay (как в throttle.REFERENCE_DELAYS)."""
    at = (action_type or "").strip().lower().replace("-", "_")
    if at == "like_post":
        return "like_post"
    if at == "comment_post":
        return "comment_post"
    if at == "friend_request":
        return "friend_request"
    if at == "open_dm":
        return "open_dm"
    if at == "send_dm":
        return "send_dm"
    if at in (
        "like_and_comment",
        "like_comment",
        "like_comment_friend_request",
        "like_comment_friend_request_send_dm",
    ):
        return "group_action"
    return "next_person"
