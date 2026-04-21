"""Playwright: чтение списка диалогов и последних сообщений в Facebook Messenger (эвристики DOM)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from playwright.sync_api import Page

logger = logging.getLogger(__name__)

MESSAGES_URL = "https://www.facebook.com/messages/"


class MessengerUIBlockedError(RuntimeError):
    """Модалка Facebook (E2EE PIN, восстановление чатов и т.п.) мешает скрапингу."""


def is_messenger_ui_blocked_message(text: str | None) -> bool:
    """Текст ошибки задачи / API: блокировка PIN/E2EE (для паузы авто-pull в UI)."""
    if not text:
        return False
    t = text.lower()
    if "сквозного шифрования" in t:
        return True
    if "отсутствует история чатов" in t:
        return True
    if "введите pin" in t:
        return True
    if "pin / восстановление" in t:
        return True
    if "end-to-end encrypted" in t and "pin" in t:
        return True
    return False


_DETECT_MESSENGER_BLOCKER_JS = r"""
() => {
  let t = '';
  try {
    t = (document.body && document.body.innerText) || '';
  } catch (e) {
    return null;
  }
  const slice = t.slice(0, 80000);
  const needles = [
    'Введите PIN-код',
    'Введите PIN',
    'Отсутствует история чатов',
    'не загружены на этом устройстве',
    'PIN-код, чтобы восстановить',
    'Введите PIN-код, чтобы восстановить чаты',
    'end-to-end encrypted',
    'Enter PIN',
    'encrypted chats',
    'Некоторые сообщения отсутствуют',
    'Some messages are missing',
    'Использовать одноразовый код',
    'Use a one-time code',
    'восстановить историю чатов',
    'restore chat history'
  ];
  for (const n of needles) {
    if (slice.includes(n)) return n;
  }
  const dialogs = document.querySelectorAll('[role="dialog"]');
  for (const d of dialogs) {
    const dt = (d.innerText || '').slice(0, 3000);
    if (dt.length < 30) continue;
    if (/PIN|pin|шифрован|encrypt|восстанов.*чат/i.test(dt) && /сообщен|message|чат|chat/i.test(dt))
      return 'dialog_e2ee';
  }
  return null;
}
"""

MESSENGER_E2EE_BASE_HINT = (
    "Facebook показывает окно сквозного шифрования (PIN / восстановление чатов). "
    "Откройте обычный Chrome с тем же аккаунтом, зайдите в Messenger, введите PIN и дождитесь загрузки чатов, "
    "закройте окно. Затем снова запустите синхронизацию в FB Master. "
    "Пока открыт чат в кабинете, автообновление тоже поднимает браузер по таймеру — при необходимости "
    "увеличьте интервал в «Система → Скорость и паузы» или закройте диалог в мессенджере."
)


def _e2ee_pin_try_submit(page: Page) -> None:
    """Подтверждение PIN — только кнопки внутри модалки, не «Продолжить» под затемнением в чате."""
    try:
        dialogs = page.locator('[role="dialog"]')
        n_d = min(dialogs.count(), 6)
        for i in range(n_d):
            d = dialogs.nth(i)
            try:
                txt = d.inner_text(timeout=2000)[:1200]
            except Exception:
                continue
            if not re.search(r"pin|пин|восстанов|encrypted|missing|отсутств|restore", txt, re.I):
                continue
            for sub in ("Продолжить", "Continue", "Submit", "Далее", "Next", "OK"):
                try:
                    btn = d.get_by_role("button", name=re.compile(f"^{re.escape(sub)}$", re.I))
                    if btn.count() > 0:
                        btn.first.click(timeout=4000)
                        logger.info("Messenger: в модалке PIN нажато «%s»", sub)
                        return
                except Exception:
                    continue
    except Exception:
        logger.debug("_e2ee_pin_try_submit dialog scope", exc_info=True)
    for sub in ("Продолжить", "Continue", "Submit", "Далее", "Next", "OK"):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(sub)}$", re.I))
            if btn.count() > 0:
                btn.first.click(timeout=4000)
                return
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass


_E2EE_OTP_FILL_JS = r"""
(digits) => {
  const want = String(digits || '').replace(/\D/g, '');
  if (want.length < 4) return { ok: false, reason: 'short' };
  let d = null;
  let bestArea = 0;
  for (const cand of document.querySelectorAll('[role="dialog"]')) {
    const txt = (cand.innerText || '').slice(0, 4000);
    if (!/pin|пин|восстанов|encrypted|шифрован|missing|отсутств/i.test(txt)) continue;
    const r = cand.getBoundingClientRect();
    const area = Math.max(1, r.width) * Math.max(1, r.height);
    if (area > bestArea) {
      bestArea = area;
      d = cand;
    }
  }
  if (!d) return { ok: false, reason: 'no_dialog' };

  const seen = new Set();
  const inputs = [];
  function add(inp) {
    if (!inp || seen.has(inp)) return;
    seen.add(inp);
    inputs.push(inp);
  }
  d.querySelectorAll('input').forEach(add);
  d.querySelectorAll('*').forEach((host) => {
    try {
      if (host.shadowRoot) host.shadowRoot.querySelectorAll('input').forEach(add);
    } catch (e) {}
  });

  function looksOtp(inp) {
    const t = (inp.type || '').toLowerCase();
    if (t === 'hidden' || t === 'checkbox' || t === 'radio' || t === 'file') return false;
    const mx = (inp.getAttribute('maxlength') || '').trim();
    if (mx === '1') return true;
    const r = inp.getBoundingClientRect();
    if (r.width >= 18 && r.width <= 140 && r.height >= 18 && r.height <= 140) return true;
    return false;
  }

  let cells = inputs.filter(looksOtp);
  if (cells.length < 4) cells = inputs.filter((inp) => {
    const t = (inp.type || '').toLowerCase();
    if (t === 'hidden' || t === 'checkbox' || t === 'radio' || t === 'file') return false;
    const r = inp.getBoundingClientRect();
    return r.width >= 8 && r.height >= 8;
  });
  const ce = Array.from(d.querySelectorAll('[contenteditable="true"]')).filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 8 && r.height >= 8;
  });
  if (cells.length < 4 && ce.length >= 4) cells = ce;

  cells.sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    if (Math.abs(ra.top - rb.top) > 28) return ra.top - rb.top;
    return ra.left - rb.left;
  });
  if (cells.length < 4) return { ok: false, reason: 'few_cells', n: cells.length, raw: inputs.length };

  const n = Math.min(cells.length, want.length);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  for (let i = 0; i < n; i++) {
    const el = cells[i];
    const ch = want[i];
    el.focus();
    try {
      el.dispatchEvent(
        new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: ch })
      );
    } catch (e) {}
    if (el.tagName === 'INPUT' && setter && setter.set) {
      setter.set.call(el, ch);
    } else if (el.isContentEditable) {
      el.textContent = ch;
    } else {
      el.value = ch;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    try {
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    } catch (e2) {}
  }
  return { ok: true, n };
}
"""


def _try_autofill_e2ee_pin_digit_cells(page: Page, pin: str) -> bool:
    """
    Модалка «Введите PIN-код, чтобы восстановить чаты»: 6 ячеек (React) — JS с native value setter + клавиатура.
    """
    digits = re.sub(r"\D", "", (pin or "").strip())
    if len(digits) < 4:
        return False
    try:
        res = page.evaluate(_E2EE_OTP_FILL_JS, digits)
        if isinstance(res, dict) and res.get("ok"):
            page.wait_for_timeout(450)
            _e2ee_pin_try_submit(page)
            logger.info("Messenger: PIN в OTP-ячейки (JS), n=%s", res.get("n"))
            return True
        if isinstance(res, dict):
            logger.info(
                "Messenger: E2EE PIN JS не применился: %s n=%s raw=%s",
                res.get("reason"),
                res.get("n"),
                res.get("raw"),
            )
    except Exception:
        logger.debug("_try_autofill_e2ee_pin_digit_cells JS", exc_info=True)

    # Несколько ячеек maxlength=1 — реальные key events (React часто слушает только их).
    try:
        dlg = _pin_restore_dialog_locator(page)
        if dlg.count() == 0:
            raise RuntimeError("no_dialog")
        otp = dlg.locator(
            'input[maxlength="1"]:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])'
        )
        cnt = otp.count()
        if cnt >= 4:
            seq = digits[: min(cnt, 12)]
            for i, ch in enumerate(seq):
                if not ch.isdigit():
                    continue
                cell = otp.nth(i)
                cell.wait_for(state="visible", timeout=5000)
                cell.click(timeout=3000)
                page.wait_for_timeout(100)
                cell.press_sequentially(ch, delay=90)
            page.wait_for_timeout(450)
            _e2ee_pin_try_submit(page)
            logger.info("Messenger: PIN press_sequentially по %s ячейкам maxlength=1", len(seq))
            return True
    except Exception:
        logger.debug("_try_autofill_e2ee_pin_digit_cells press_sequential", exc_info=True)

    # Одно поле на весь PIN (6–12 цифр).
    try:
        dlg = _pin_restore_dialog_locator(page)
        if dlg.count() == 0:
            raise RuntimeError("no_dialog")
        for sel in (
            'input[inputmode="numeric"][maxlength="6"]',
            'input[maxlength="6"]',
            'input[inputmode="numeric"]',
        ):
            cand = dlg.locator(sel)
            if cand.count() == 0:
                continue
            one = cand.first
            one.wait_for(state="visible", timeout=4000)
            one.click(timeout=3000)
            one.press_sequentially(digits[:12], delay=110)
            page.wait_for_timeout(450)
            _e2ee_pin_try_submit(page)
            logger.info("Messenger: PIN press_sequentially в одно поле (%s)", sel)
            return True
        raise RuntimeError("no_single_pin_field")
    except Exception:
        logger.debug("_try_autofill_e2ee_pin_digit_cells single_field", exc_info=True)

    # Запас: фокус в первую ячейку и поочерёдный ввод (как с клавиатуры).
    try:
        dlg = _pin_restore_dialog_locator(page)
        if dlg.count() == 0:
            return False
        first = dlg.locator('input:not([type="hidden"]):not([type="checkbox"])').first
        if first.count() == 0:
            first = dlg.locator("input").first
        first.wait_for(state="visible", timeout=5000)
        first.click(timeout=3000)
        page.wait_for_timeout(250)
        chunk = digits[: min(len(digits), 12)]
        try:
            page.keyboard.insert_text(chunk)
        except Exception:
            page.keyboard.type(chunk, delay=140)
        page.wait_for_timeout(400)
        _e2ee_pin_try_submit(page)
        logger.info("Messenger: PIN введён через keyboard (insert_text/type)")
        return True
    except Exception:
        logger.debug("_try_autofill_e2ee_pin_digit_cells keyboard", exc_info=True)

    # Последний запас: Playwright fill по ячейкам (старое поведение).
    try:
        dlg = _pin_restore_dialog_locator(page)
        inputs = dlg.locator("input")
        n = inputs.count()
        if n < 4:
            return False
        cells: list[Any] = []
        for i in range(n):
            loc = inputs.nth(i)
            try:
                if not loc.is_visible(timeout=800):
                    continue
                t = (loc.get_attribute("type") or "").lower()
                if t in ("hidden", "checkbox", "radio", "file"):
                    continue
                cells.append(loc)
            except Exception:
                continue
        if len(cells) < 4 or len(cells) > 12:
            return False
        if len(digits) < len(cells):
            return False
        seq = digits[: len(cells)]
        for loc_c, ch in zip(cells, seq):
            loc_c.click(timeout=2500)
            try:
                loc_c.fill("")
            except Exception:
                pass
            loc_c.fill(ch)
            page.wait_for_timeout(100)
        page.wait_for_timeout(350)
        _e2ee_pin_try_submit(page)
        logger.info("Messenger: введён PIN по ячейкам fill() (%s цифр)", len(seq))
        return True
    except Exception:
        logger.debug("_try_autofill_e2ee_pin_digit_cells fill", exc_info=True)
    return False


def try_autofill_messenger_e2ee_pin(page: Page, pin: str) -> bool:
    """
    Пытается нажать «Ввести PIN» и ввести PIN (одно поле или несколько ячеек). Хрупко к вёрстке Facebook.
    Возвращает True, если ввод выполнен (не гарантирует успех разблокировки).
    """
    raw = (pin or "").strip()
    if not raw:
        return False
    try:
        if _try_autofill_e2ee_pin_digit_cells(page, raw):
            return True
        for label in ("Ввести PIN-код", "Ввести PIN", "Enter PIN"):
            try:
                loc = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                if loc.count() > 0:
                    loc.first.click(timeout=5000)
                    page.wait_for_timeout(700)
                    break
            except Exception:
                continue
        if _try_autofill_e2ee_pin_digit_cells(page, raw):
            return True
        dialog_pw = page.locator('[role="dialog"] input[type="password"]')
        plain_pw = page.locator('input[type="password"]')
        numeric = page.locator('input[inputmode="numeric"]')
        for loc in (dialog_pw, plain_pw, numeric):
            try:
                if loc.count() == 0:
                    continue
                el = loc.first
                el.wait_for(state="visible", timeout=4000)
                el.click(timeout=3000)
                el.fill(raw)
                page.wait_for_timeout(400)
                _e2ee_pin_try_submit(page)
                return True
            except Exception:
                continue
    except Exception:
        logger.debug("try_autofill_messenger_e2ee_pin", exc_info=True)
    return False


def detect_messenger_blocker(page: Page) -> str | None:
    """Текст маркера блокировки E2EE/PIN или None, если окна нет."""
    try:
        hit = page.evaluate(_DETECT_MESSENGER_BLOCKER_JS)
    except Exception:
        logger.exception("detect_messenger_blocker evaluate")
        return None
    return hit if hit else None


_PIN_RESTORE_DIALOG_OPEN_JS = """() => {
  for (const d of document.querySelectorAll('[role="dialog"]')) {
    const t = (d.innerText || '').slice(0, 5000);
    if (!/pin|пин|восстанов|restore chat|encrypted|шифрован|missing|отсутств|enter pin|одноразов|one-?time/i.test(t))
      continue;
    const r = d.getBoundingClientRect();
    if (r.width < 80 || r.height < 30) continue;
    if (r.bottom < 4 || r.top > innerHeight + 800) continue;
    return true;
  }
  return false;
}"""


def messenger_pin_restore_dialog_open(page: Page) -> bool:
    """Открыта модалка ввода PIN / восстановления чатов (не фоновый чат под затемнением)."""
    try:
        return bool(page.evaluate(_PIN_RESTORE_DIALOG_OPEN_JS))
    except Exception:
        return False


_SCROLL_PIN_DIALOG_CENTER_JS = """() => {
  let best = null;
  let bestArea = 0;
  for (const d of document.querySelectorAll('[role="dialog"]')) {
    const t = (d.innerText || '').slice(0, 5000);
    if (!/pin|пин|восстанов|restore|encrypted|шифрован|missing|отсутств|enter pin/i.test(t)) continue;
    const r = d.getBoundingClientRect();
    const area = Math.max(1, r.width) * Math.max(1, r.height);
    if (area > bestArea) {
      bestArea = area;
      best = d;
    }
  }
  if (!best) return false;
  try {
    window.scrollTo(0, 0);
  } catch (e) {}
  try {
    best.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
  } catch (e) {}
  const r2 = best.getBoundingClientRect();
  const centerY = r2.top + r2.height / 2;
  const want = window.innerHeight * 0.42;
  const dy = want - centerY;
  if (Math.abs(dy) > 18) {
    try {
      window.scrollBy({ top: dy, left: 0, behavior: 'instant' });
    } catch (e2) {
      try { window.scrollBy(0, dy); } catch (e3) {}
    }
  }
  return true;
}"""


def scroll_messenger_e2ee_pin_dialog_into_view(page: Page) -> None:
    """Удерживает окно PIN в зоне видимости: сброс скролла страницы и центрирование модалки."""
    try:
        if page.evaluate(_SCROLL_PIN_DIALOG_CENTER_JS):
            logger.info("Messenger: модалка PIN/E2EE подогнана к центру окна просмотра")
            page.wait_for_timeout(280)
    except Exception:
        logger.debug("scroll_messenger_e2ee_pin_dialog_into_view", exc_info=True)


def _pin_restore_dialog_locator(page: Page):
    """Локатор диалога с текстом про PIN (не первый попавшийся [role=dialog])."""
    root = page.locator('[role="dialog"]')
    try:
        n = min(root.count(), 8)
    except Exception:
        n = 0
    for i in range(n):
        d = root.nth(i)
        try:
            t = d.inner_text(timeout=2000)[:2200]
        except Exception:
            continue
        if re.search(r"pin|пин|восстанов|encrypted|missing|отсутств|restore", t, re.I):
            return d
    return root.first


def _clear_e2ee_pin_fields_in_open_dialog(page: Page) -> None:
    """Очистить поля PIN в открытой модалке перед второй попыткой (777777 → 111111)."""
    try:
        page.evaluate(
            """() => {
          const d = document.querySelector('[role="dialog"]');
          if (!d) return;
          d.querySelectorAll('input').forEach((inp) => {
            const t = (inp.type || '').toLowerCase();
            if (t === 'hidden' || t === 'checkbox' || t === 'radio' || t === 'file') return;
            inp.value = '';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
          });
        }"""
        )
        page.wait_for_timeout(220)
    except Exception:
        logger.debug("_clear_e2ee_pin_fields_in_open_dialog", exc_info=True)


def try_apply_messenger_e2ee_pin(page: Page) -> bool:
    """
    Ввод PIN из цепочки (из .env одна строка; иначе MESSENGER_E2EE_PIN_FALLBACK, затем FALLBACK_ALT).
    Если первый вариант не снял блокировку — очищаем поля и пробуем запасной.
    """
    from backend.config import effective_messenger_e2ee_pins_for_autofill

    try:
        if messenger_pin_restore_dialog_open(page):
            scroll_messenger_e2ee_pin_dialog_into_view(page)
            page.wait_for_timeout(200)
    except Exception:
        pass

    pins = [p for p in effective_messenger_e2ee_pins_for_autofill() if (p or "").strip()]
    if not pins:
        return False
    did = False
    for idx, pin in enumerate(pins):
        if idx > 0:
            _clear_e2ee_pin_fields_in_open_dialog(page)
            page.wait_for_timeout(400)
        if try_autofill_messenger_e2ee_pin(page, pin):
            did = True
            page.wait_for_timeout(1800)
            if not detect_messenger_blocker(page):
                return True
    return did


def raise_if_messenger_ui_blocked(page: Page) -> None:
    """
    Если на экране окно PIN / E2EE / восстановления чатов — прерываем с понятной ошибкой.
    Вызывать после open_messages_inbox / open_thread, когда DOM уже отрисован.
    """
    hit = detect_messenger_blocker(page)
    if not hit:
        return
    raise MessengerUIBlockedError(f"{MESSENGER_E2EE_BASE_HINT} (обнаружено: {hit})")


_E2EE_CONTINUE_MAIN_CLICK_JS = r"""
() => {
  const href = (location.href || '').toLowerCase();
  if (!href.includes('/messages') && !href.includes('messenger.com')) return false;
  const enc = /сквозн|шифрован|e2ee|end-to-end|encrypted|protected/i;
  const main = document.querySelector('[role="main"]') || document.body;
  function hasEncContext(el) {
    let p = el;
    for (let i = 0; i < 18 && p; i++) {
      if (enc.test((p.innerText || '').slice(0, 2800))) return true;
      p = p.parentElement;
    }
    return false;
  }
  function clickEl(el) {
    try {
      el.scrollIntoView({ block: 'center', behavior: 'instant' });
    } catch (e) {}
    try {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      return true;
    } catch (e) {
      try {
        el.click();
        return true;
      } catch (e2) {}
    }
    return false;
  }
  /* Messenger рисует синюю полосу «Продолжить» как div role="none", не [role="button"] — ищем текстовый узел. */
  try {
    const tw = document.createTreeWalker(main, NodeFilter.SHOW_TEXT, null);
    let tn;
    while ((tn = tw.nextNode())) {
      const raw = (tn.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (raw !== 'продолжить' && raw !== 'continue' && raw !== 'продолжить?') continue;
      let el = tn.parentElement;
      for (let i = 0; i < 16 && el; i++) {
        const r = el.getBoundingClientRect();
        if (r.width > 64 && r.height > 22 && hasEncContext(el)) {
          if (clickEl(el)) return true;
          break;
        }
        el = el.parentElement;
      }
    }
  } catch (e) {}
  const sel =
    '[role="button"], button, a[role="link"], span[role="link"], div[role="none"], span[role="none"]';
  const cands = [];
  for (const el of main.querySelectorAll(sel)) {
    const raw = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (raw !== 'продолжить' && raw !== 'continue' && raw !== 'продолжить?') continue;
    if (!hasEncContext(el)) continue;
    const r = el.getBoundingClientRect();
    cands.push({ el, area: Math.max(1, r.width) * Math.max(1, r.height) });
  }
  cands.sort((a, b) => b.area - a.area);
  for (const { el } of cands) {
    if (clickEl(el)) return true;
  }
  return false;
}
"""


_E2EE_CONTINUE_GATE_CLICK_JS = r"""
() => {
  const href = (location.href || '').toLowerCase();
  if (!href.includes('/messages') && !href.includes('messenger.com')) return false;
  const enc = /сквозн|шифрован|e2ee|end-to-end|encrypted|protected|невозможно отправить/i;
  function norm(s) {
    return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  }
  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 3 && r.height > 3 && r.bottom > 36 && r.top < innerHeight + 120;
  }
  function matchesLabel(t) {
    return t === 'продолжить' || t === 'продолжить?' || t === 'continue';
  }
  const sel = '[role="button"], button, a[role="link"], span[role="link"]';
  for (const el of document.querySelectorAll(sel)) {
    if (!visible(el)) continue;
    const t = norm(el.innerText || el.textContent);
    if (!matchesLabel(t)) continue;
    let hitEnc = false;
    let p = el;
    for (let i = 0; i < 14 && p; i++) {
      const chunk = (p.innerText || '').slice(0, 2200);
      if (enc.test(chunk)) {
        hitEnc = true;
        break;
      }
      p = p.parentElement;
    }
    if (!hitEnc) {
      p = el;
      for (let i = 0; i < 10 && p; i++) {
        const chunk = (p.innerText || '').slice(0, 500);
        if ((/\u2022|·/.test(chunk) || chunk.length < 420) && /продолжить|continue/i.test(chunk)) {
          hitEnc = true;
          break;
        }
        p = p.parentElement;
      }
    }
    if (!hitEnc && (t === 'продолжить' || t === 'continue')) {
      const r = el.getBoundingClientRect();
      if (r.width > 100 && r.top > innerHeight * 0.32 && r.left > innerWidth * 0.16) hitEnc = true;
    }
    if (!hitEnc) continue;
    try {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    } catch (e) {
      try {
        el.click();
      } catch (e2) {}
    }
    return true;
  }
  return false;
}
"""


def try_click_messenger_e2ee_continue_gate(page: Page) -> bool:
    """
    Плашка «сквозное шифрование» / E2EE: «Продолжить» в области чата или «Продолжить?» в списке.
    В Messenger кнопка часто — div с role="none", а не role="button" (см. DevTools).
    """
    href = ""
    try:
        href = (page.url or "").lower()
    except Exception:
        href = ""
    if "/messages" not in href and "messenger.com" not in href:
        return False
    # Пока открыта модалка PIN, не жмём «Продолжить» в main — клик уводит фокус и модалка «уезжает» вниз.
    try:
        if messenger_pin_restore_dialog_open(page):
            return False
    except Exception:
        pass
    try:
        if bool(page.evaluate(_E2EE_CONTINUE_MAIN_CLICK_JS)):
            logger.info("Messenger: «Продолжить» E2EE в [role=main] (JS: текст / role=none / button)")
            return True
    except Exception:
        logger.debug("try_click_messenger_e2ee_continue_gate (main JS)", exc_info=True)
    try:
        main = page.locator('[role="main"]')
        if main.count():
            for pat in (r"^\s*Продолжить\s*\??\s*$", r"^\s*Continue\s*$"):
                try:
                    loc = main.get_by_role("button", name=re.compile(pat, re.I))
                    if loc.count() == 0:
                        continue
                    el = loc.first
                    try:
                        el.scroll_into_view_if_needed(timeout=4000)
                    except Exception:
                        pass
                    el.click(timeout=6000, force=True)
                    logger.info("Messenger: «Продолжить» E2EE (Playwright main, force)")
                    return True
                except Exception:
                    continue
    except Exception:
        logger.debug("try_click_messenger_e2ee_continue_gate (playwright main)", exc_info=True)
    try:
        for label in ("Продолжить", "Continue"):
            try:
                btn = page.get_by_role("button", name=label, exact=True)
                if btn.count() == 0:
                    continue
                first = btn.first
                try:
                    first.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                first.click(timeout=6000, force=True)
                logger.info("Messenger: нажата кнопка «%s» (E2EE gate, role=button)", label)
                return True
            except Exception:
                continue
    except Exception:
        logger.debug("try_click_messenger_e2ee_continue_gate (playwright global)", exc_info=True)
    try:
        if bool(page.evaluate(_E2EE_CONTINUE_GATE_CLICK_JS)):
            logger.info("Messenger: нажата кнопка/ссылка «Продолжить» (E2EE gate, fallback по всей странице)")
            return True
    except Exception:
        logger.debug("try_click_messenger_e2ee_continue_gate (gate JS)", exc_info=True)
    return False


def try_resolve_messenger_e2ee_thread_blockers(page: Page) -> bool:
    """
    Одна итерация: E2EE «Продолжить» и/или автоподстановка PIN.
    Если виден блокер с PIN — сначала PIN, затем «Продолжить», чтобы не кликнуть чужую
    плашку под модалкой.
    """
    did = False
    try:
        if messenger_pin_restore_dialog_open(page):
            scroll_messenger_e2ee_pin_dialog_into_view(page)
    except Exception:
        pass
    blocker = detect_messenger_blocker(page) or ""
    bl = blocker.lower()
    pin_first = bool(
        re.search(
            r"pin|пин|восстанов|restore|missing|отсутств|enter pin|encrypted chat|"
            r"dialog_e2ee|одноразов|one-?time",
            bl,
            re.I,
        )
    )
    if pin_first:
        if try_apply_messenger_e2ee_pin(page):
            did = True
            page.wait_for_timeout(900)
        if try_click_messenger_e2ee_continue_gate(page):
            did = True
            page.wait_for_timeout(1200)
    else:
        if try_click_messenger_e2ee_continue_gate(page):
            did = True
            page.wait_for_timeout(1200)
        if try_apply_messenger_e2ee_pin(page):
            did = True
            page.wait_for_timeout(900)
    return did


def wait_until_messenger_unblocked_or_raise(page: Page) -> None:
    """
    Если виден PIN/E2EE — ждём MESSENGER_E2EE_WAIT_SECONDS (переменная окружения, по умолчанию 600 с),
    периодически проверяя DOM; после ввода PIN синхронизация продолжается. При 0 — как raise_if_messenger_ui_blocked.
    """
    from backend.config import (
        MESSENGER_E2EE_PIN,
        MESSENGER_E2EE_WAIT_SECONDS,
        effective_messenger_e2ee_pins_for_autofill,
    )

    hit = detect_messenger_blocker(page)
    # Баннер в сайдбаре иногда появляется после первой проверки; без второй попытки скрапинг
    # успевает завершиться и Playwright закрывает браузер до появления текста про PIN.
    if not hit:
        page.wait_for_timeout(3000)
        hit = detect_messenger_blocker(page)
    if not hit:
        return
    max_wait = max(0, int(MESSENGER_E2EE_WAIT_SECONDS))
    if max_wait <= 0:
        raise MessengerUIBlockedError(f"{MESSENGER_E2EE_BASE_HINT} (обнаружено: {hit})")

    pin_env = (MESSENGER_E2EE_PIN or "").strip()
    pins_auto = effective_messenger_e2ee_pins_for_autofill()
    if pin_env:
        logger.warning(
            "Messenger: PIN/E2EE (%s). Окно до %s с; при необходимости подставляется MESSENGER_E2EE_PIN.",
            hit,
            max_wait,
        )
    else:
        logger.warning(
            "Messenger: на экране PIN/E2EE (%s). Окно до %s с; автоподстановка по цепочке PIN (длины %s).",
            hit,
            max_wait,
            [len(p) for p in pins_auto] if pins_auto else [],
        )
    if try_resolve_messenger_e2ee_thread_blockers(page):
        logger.info("Messenger: попытка снять E2EE «Продолжить» / автоподстановка PIN")
        page.wait_for_timeout(4000)
        hit = detect_messenger_blocker(page)
        if not hit:
            logger.info("Messenger: окно PIN/E2EE закрыто после автоподстановки")
            return
    deadline = time.monotonic() + float(max_wait)
    poll_ms = 2500
    while True:
        try:
            if messenger_pin_restore_dialog_open(page):
                scroll_messenger_e2ee_pin_dialog_into_view(page)
        except Exception:
            pass
        try_resolve_messenger_e2ee_thread_blockers(page)
        page.wait_for_timeout(600)
        hit = detect_messenger_blocker(page)
        if not hit:
            logger.info("Messenger: окно PIN/E2EE закрыто, продолжаем синхронизацию")
            return
        if time.monotonic() >= deadline:
            tail = (
                f" За {max_wait} с PIN не был введён или окно не исчезло. "
                f"Увеличьте MESSENGER_E2EE_WAIT_SECONDS в .env (секунды ожидания) или введите PIN в этом окне Chrome."
            )
            raise MessengerUIBlockedError(
                f"{MESSENGER_E2EE_BASE_HINT}{tail} (обнаружено: {hit})"
            )
        page.wait_for_timeout(poll_ms)

# Список диалогов в левой колонке / общей ленте
SCRAPE_INBOX_JS = r"""
() => {
  function norm(t) {
    return String(t || '').replace(/\s+/g, ' ').trim();
  }
  function isTimeish(t) {
    const s = norm(t).toLowerCase();
    if (!s) return true;
    return /^(\d{1,2}:\d{2}|\d+\s*(м|мин|ч|ч\.|д|дн|н|нед\.|h|hr|d|w)|вчера|yesterday|today|сегодня|now|сейчас)$/i.test(s);
  }
  const rows = [];
  const seen = new Set();
  /* FB иногда отдаёт ссылки на messenger.com/t/… без /messages/ — тогда старый селектор их терял */
  const anchors = document.querySelectorAll(
    'a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"],' +
      'a[href*="messenger.com/t/"], a[href*="messenger.com/e2ee/t/"],' +
      'a[href*="business.facebook.com/messages/t/"]'
  );
  for (const a of anchors) {
    let href = (a.getAttribute('href') || '').trim();
    if (!href) continue;
    if (href.startsWith('/')) href = 'https://www.facebook.com' + href;
    let m = href.match(/facebook\.com\/messages\/(?:e2ee\/)?t\/([^/?#]+)/i);
    if (!m) m = href.match(/business\.facebook\.com\/messages\/(?:e2ee\/)?t\/([^/?#]+)/i);
    if (!m) m = href.match(/messenger\.com\/(?:e2ee\/)?t\/([^/?#]+)/i);
    if (!m) continue;
    const seg = (m[1] || '').trim();
    if (!seg) continue;
    if (seen.has(seg)) continue;
    seen.add(seg);
    href = href.split('?')[0].split('#')[0];
    const isNumeric = /^\d+$/.test(seg);
    const threadId = isNumeric ? seg : '';
    let name = '';
    let el = a;
    for (let i = 0; i < 12 && el; i++) {
      const sp = el.querySelector && el.querySelector('span[dir="auto"]');
      if (sp) {
        const t = (sp.textContent || '').trim();
        if (t && t.length < 200) { name = t; break; }
      }
      el = el.parentElement;
    }
    if (!name) name = (a.textContent || '').trim().slice(0, 120) || 'Диалог';
    let snippet = '';
    let row = a;
    for (let i = 0; i < 10 && row; i++) {
      const txt = norm(row.innerText || row.textContent || '');
      if (txt.length > Math.max(24, name.length + 4)) break;
      row = row.parentElement;
    }
    const bucket = [];
    function pushCandidate(t) {
      const v = norm(t);
      if (!v || bucket.includes(v)) return;
      bucket.push(v);
    }
    if (row) {
      for (const line of String(row.innerText || '').split('\n')) pushCandidate(line);
      const spans = row.querySelectorAll ? row.querySelectorAll('span[dir="auto"]') : [];
      for (const sp of spans) pushCandidate(sp.textContent || '');
    } else {
      pushCandidate(a.innerText || a.textContent || '');
    }
    for (const cand of bucket) {
      if (cand === name) continue;
      if (cand.length >= 500) continue;
      if (isTimeish(cand)) continue;
      if (cand.toLowerCase() === 'facebook') continue;
      snippet = cand;
      break;
    }
    if (!snippet) {
      el = a;
      for (let i = 0; i < 12 && el; i++) {
        const spans = el.querySelectorAll ? el.querySelectorAll('span[dir="auto"]') : [];
        if (spans.length > 1) {
          const t = norm(spans[spans.length - 1].textContent || '');
          if (t && t !== name && !isTimeish(t) && t.length < 500) { snippet = t; break; }
        }
        el = el.parentElement;
      }
    }
    rows.push({
      thread_id: threadId,
      peer_url: href,
      peer_name: name.slice(0, 255),
      snippet: snippet.slice(0, 2000),
    });
  }
  return rows;
}
"""

# Открытый тред: текст + направление (исходящие обычно справа в LTR)
SCRAPE_THREAD_MESSAGES_JS = r"""
() => {
  function norm(t) {
    return String(t || '').replace(/\s+/g, ' ').trim();
  }
  function composerCandidates() {
    return Array.from(document.querySelectorAll(
      'div[role="textbox"][contenteditable="true"], [contenteditable="true"][data-lexical-editor="true"], [contenteditable="true"][aria-placeholder], [contenteditable="true"][data-placeholder], textarea'
    ));
  }
  function composerScore(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 12) return -999;
    if (r.bottom < 0 || r.top > window.innerHeight + 40) return -999;
    let score = 0;
    const ph = (
      (el.getAttribute('aria-placeholder') || '') + ' ' +
      (el.getAttribute('data-placeholder') || '') + ' ' +
      (el.getAttribute('placeholder') || '')
    ).toLowerCase();
    if (el.getAttribute('role') === 'textbox') score += 6;
    if (el.tagName === 'TEXTAREA') score += 4;
    if (el.getAttribute('data-lexical-editor') === 'true') score += 8;
    if (ph.includes('message') || ph.includes('сообщ') || ph.includes('напишите') || ph === 'aa') score += 18;
    if (r.width > 180) score += 6;
    if (r.top > window.innerHeight * 0.45) score += 10;
    return score;
  }
  const root =
    document.querySelector('[role="main"]') ||
    document.querySelector('[data-pagelet="MWThread"]') ||
    document.body;
  let composer = null;
  let composerScoreBest = -999;
  for (const cand of composerCandidates()) {
    const s = composerScore(cand);
    if (s > composerScoreBest) {
      composerScoreBest = s;
      composer = cand;
    }
  }
  const composerRect = composer ? composer.getBoundingClientRect() : null;
  const rtl = document.documentElement.getAttribute('dir') === 'rtl';
  const rr = root.getBoundingClientRect();
  const w = Math.max(rr.width, 240);
  const left = rr.left;
  const midLine = left + w * 0.5;
  /** Порог: правее/левее центра с запасом — «мои» сообщения */
  const outThreshold = rtl ? left + w * 0.42 : left + w * 0.58;
  const bounds = composerRect
    ? {
        left: composerRect.left - 140,
        right: composerRect.right + 140,
        top: Math.max(rr.top - 20, 0),
        bottom: composerRect.top - 10
      }
    : {
        left: left + w * 0.18,
        right: left + w * 0.96,
        top: rr.top,
        bottom: rr.bottom - 90
      };

  function isLeafDirAuto(el) {
    const inner = el.querySelectorAll('[dir="auto"]');
    for (const x of inner) {
      if (x === el) continue;
      if ((x.textContent || '').trim().length > 0) return false;
    }
    return true;
  }

  function directionFromAria(el) {
    let p = el;
    for (let i = 0; i < 16 && p; i++) {
      const al = ((p.getAttribute('aria-label') || '') + ' ' + (p.getAttribute('title') || '')).toLowerCase();
      if (
        /you sent|sent an attachment|вы отправили|отправлено вами|tu enviaste|du hast gesendet|vous avez envoyé/.test(
          al
        )
      )
        return 'out';
      p = p.parentElement;
    }
    return null;
  }

  function directionFromStyles(el) {
    let p = el;
    for (let i = 0; i < 22 && p; i++) {
      const st = getComputedStyle(p);
      const as = st.alignSelf;
      if (as === 'flex-end' || as === 'end') return 'out';
      const ml = st.marginLeft;
      const mr = st.marginRight;
      if (!rtl && ml === 'auto' && mr !== 'auto') return 'out';
      if (!rtl && mr === 'auto' && ml !== 'auto') return 'in';
      if (rtl && mr === 'auto' && ml !== 'auto') return 'out';
      if (rtl && ml === 'auto' && mr !== 'auto') return 'in';
      p = p.parentElement;
    }
    return null;
  }

  function directionFromPosition(br) {
    const cx = (br.left + br.right) / 2;
    if (rtl) return cx < outThreshold ? 'out' : 'in';
    return cx > outThreshold ? 'out' : 'in';
  }

  const autos = root.querySelectorAll('[dir="auto"]');
  const raw = [];
  for (const el of autos) {
    const body = norm(el.textContent || '');
    if (body.length < 1 || body.length > 8000) continue;
    if (!isLeafDirAuto(el)) continue;
    if (el.closest('header, footer, nav, aside, [role="navigation"], [role="banner"]')) continue;
    const br = el.getBoundingClientRect();
    if (br.width < 2 || br.height < 2) continue;
    if (br.bottom > bounds.bottom || br.top < bounds.top) continue;
    if (br.right < bounds.left || br.left > bounds.right) continue;
    let dir = directionFromAria(el) || directionFromStyles(el);
    if (!dir) {
      const cx = (br.left + br.right) / 2;
      if (Math.abs(cx - midLine) < w * 0.06) dir = 'in';
      else dir = directionFromPosition(br);
    }
    raw.push({ top: br.top, left: br.left, body, direction: dir });
  }
  raw.sort((a, b) => a.top - b.top || a.left - b.left);

  const out = [];
  const seen = new Set();
  for (const r of raw) {
    const key =
      String(Math.round(r.top / 4)) +
      ':' +
      String(Math.round(r.left / 4)) +
      ':' +
      r.direction +
      ':' +
      r.body.slice(0, 120);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ body: r.body, direction: r.direction });
  }
  return out.slice(-80);
}
"""

_MESSENGER_UI_NOISE_TEXTS = {
    "meta",
    "условия использования",
    "рекламные предпочтения",
    "файлы cookie",
    "вакансии",
    "разработчикам",
    "справка",
    "facebook",
    "media, files and links",
    "media files, files and links",
}
_MESSENGER_UI_NOISE_PARTS = (
    "© meta",
    "загрузка контактов и лица, не являющиеся пользователями",
    "privacy",
    "terms",
    "ad choices",
    "cookies",
    "developers",
    "careers",
    "конфиденциальность",
    "медиафайлы, файлы и ссылки",
)


def _looks_like_ui_noise_message(text: str) -> bool:
    s = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not s:
        return True
    if s in _MESSENGER_UI_NOISE_TEXTS:
        return True
    return any(part in s for part in _MESSENGER_UI_NOISE_PARTS)


def open_messages_inbox(page: Page, *, timeout_ms: int = 90_000) -> None:
    page.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def scrape_inbox_threads(page: Page) -> list[dict[str, Any]]:
    try:
        raw = page.evaluate(SCRAPE_INBOX_JS)
        if isinstance(raw, list):
            return [
                x
                for x in raw
                if isinstance(x, dict) and str(x.get("peer_url") or "").strip()
            ]
    except Exception:
        logger.exception("scrape_inbox_threads evaluate")
    return []


_SCROLL_MESSENGER_THREAD_LIST_JS = r"""
() => {
  const grids = Array.from(document.querySelectorAll('[role="grid"], [role="feed"]'));
  for (const el of grids) {
    try {
      if (el.scrollHeight > el.clientHeight + 60) {
        const step = Math.min(520, Math.max(180, el.clientHeight * 0.72));
        el.scrollTop = el.scrollTop + step;
        return true;
      }
    } catch (e) {}
  }
  const main = document.querySelector('[role="main"]');
  if (main) {
    try {
      if (main.scrollHeight > main.clientHeight + 40) {
        main.scrollTop = main.scrollTop + Math.min(500, main.clientHeight * 0.75);
        return true;
      }
    } catch (e) {}
  }
  window.scrollBy(0, 420);
  return true;
}
"""


def scroll_messenger_inbox_list_step(page: Page) -> None:
    """Прокрутка списка диалогов (виртуализированный список — без скролла видны не все треды)."""
    try:
        page.evaluate(_SCROLL_MESSENGER_THREAD_LIST_JS)
    except Exception:
        logger.debug("scroll_messenger_inbox_list_step", exc_info=True)


def scrape_inbox_threads_with_scroll(
    page: Page, *, max_rounds: int = 40, stagnant_limit: int = 6
) -> list[dict[str, Any]]:
    """
    Собирает ссылки на треды, периодически прокручивая ленту: иначе в DOM попадают только
    видимые строки, и свежие чаты внизу списка не попадают в синхронизацию.
    """
    by_tid: dict[str, dict[str, Any]] = {}
    stagnant = 0
    for r in range(max(5, max_rounds)):
        batch = scrape_inbox_threads(page)
        before = len(by_tid)
        for t in batch:
            tid = str(t.get("thread_id") or "").strip()
            peer = str(t.get("peer_url") or "").strip()
            key = tid if tid.isdigit() else peer
            if key:
                by_tid[key] = t
        after = len(by_tid)
        if after <= before:
            stagnant += 1
            if stagnant >= stagnant_limit and r >= 5:
                break
        else:
            stagnant = 0
        scroll_messenger_inbox_list_step(page)
        page.wait_for_timeout(min(1100, 380 + r * 15))
    return list(by_tid.values())


def open_thread(page: Page, peer_url: str, *, timeout_ms: int = 90_000) -> None:
    url = (peer_url or "").strip()
    if not url.startswith("http"):
        url = "https://www.facebook.com" + (url if url.startswith("/") else "/" + url)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(4000)
    try:
        page.wait_for_load_state("networkidle", timeout=25_000)
    except Exception:
        pass
    page.wait_for_timeout(2000)


def scrape_thread_messages(page: Page) -> list[dict[str, str]]:
    """Сообщения открытого треда: body и direction in|out."""
    try:
        raw = page.evaluate(SCRAPE_THREAD_MESSAGES_JS)
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for x in raw:
            if not isinstance(x, dict):
                continue
            body = str(x.get("body") or "").strip()
            if not body:
                continue
            d = str(x.get("direction") or "in").lower()
            if d not in ("in", "out"):
                d = "in"
            out.append({"body": body, "direction": d})
        if out:
            noise_n = sum(1 for m in out if _looks_like_ui_noise_message(m.get("body") or ""))
            if noise_n >= max(3, len(out) - 1):
                logger.warning("scrape_thread_messages: rejected suspicious UI chrome payload")
                return []
        return out
    except Exception:
        logger.exception("scrape_thread_messages")
        return []


# allowMain: на странице профиля с док-чатом [role="main"] — это лента профиля; скролл main/window ломает UX.
_SCROLL_MESSENGER_OPEN_THREAD_TO_BOTTOM_JS = r"""
(allowMain) => {
  function scrollGridsIn(root) {
    if (!root) return false;
    const grids = root.querySelectorAll('[role="grid"]');
    for (const g of grids) {
      try {
        if (g.scrollHeight > g.clientHeight + 24) {
          g.scrollTop = g.scrollHeight;
          return true;
        }
      } catch (e) {}
    }
    try {
      if (root.scrollHeight > root.clientHeight + 40) {
        root.scrollTop = root.scrollHeight;
        return true;
      }
    } catch (e) {}
    return false;
  }
  for (const root of document.querySelectorAll('[data-pagelet="MWThread"]')) {
    if (scrollGridsIn(root)) return true;
  }
  const dockRoots = document.querySelectorAll(
    '[role="dialog"] > div, [role="presentation"], [data-pagelet="MWChatTab"]'
  );
  for (const el of dockRoots) {
    try {
      const st = window.getComputedStyle(el);
      if (st.position !== 'fixed') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 160) continue;
      if (r.bottom < window.innerHeight * 0.42) continue;
      if (r.right < window.innerWidth * 0.38) continue;
      if (!el.querySelector('[role="grid"]')) continue;
      if (scrollGridsIn(el)) return true;
    } catch (e) {}
  }
  if (!allowMain) return false;
  for (const root of document.querySelectorAll('[role="main"]')) {
    if (scrollGridsIn(root)) return true;
  }
  try {
    window.scrollTo(0, document.body.scrollHeight);
  } catch (e) {}
  return false;
}
"""


def scroll_messenger_open_thread_to_bottom(page: Page) -> None:
    """Прокрутка области переписки вниз, чтобы свежий пузырь попал в DOM для scrape_thread_messages."""
    try:
        u = (page.url or "").lower()
        allow_main = "/messages/" in u or "messenger.com" in u
        page.evaluate(_SCROLL_MESSENGER_OPEN_THREAD_TO_BOTTOM_JS, allow_main)
    except Exception:
        logger.debug("scroll_messenger_open_thread_to_bottom", exc_info=True)


def _norm_thread_snippet(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _thread_body_matches_sent(body: str, sent: str) -> bool:
    """Текст пузыря в треде соответствует отправленному (учёт нормализации пробелов и регистра)."""
    w_sent = _norm_thread_snippet(sent)
    w_body = _norm_thread_snippet(body)
    if not w_sent or not w_body:
        return False
    sl = w_sent.casefold()
    bl = w_body.casefold()
    if len(w_sent) < 14:
        if bl == sl:
            return True
        if bl.startswith(sl) and len(w_body) <= len(w_sent) + 14:
            return True
        if sl.startswith(bl) and len(w_body) >= max(4, len(w_sent) - 4):
            return True
        return False
    n = min(40, len(w_sent))
    prefix = sl[:n]
    if prefix in bl:
        return True
    if len(bl) >= n and bl[:n] == prefix:
        return True
    return False


def verify_outgoing_message_appeared_in_thread(
    page: Page,
    sent_text: str,
    *,
    max_attempts: int = 48,
    pause_ms: int = 640,
) -> tuple[bool, str]:
    """
    После очистки композера: убедиться, что текст реально появился в ленте чата как исходящее.

    Использует ту же эвристику DOM, что и scrape_thread_messages (направление out|in).
    Если эвристика «out» промахивается, для длинных ЛС допускается совпадение по последним
    сообщениям без учёта направления (редко совпадёт с входящим).
    """
    sent = _norm_thread_snippet(sent_text)
    if not sent:
        return True, ""
    last_detail = "no_messages"
    for attempt in range(max(1, max_attempts)):
        scroll_messenger_open_thread_to_bottom(page)
        page.wait_for_timeout(pause_ms)
        messages = scrape_thread_messages(page)
        if not messages:
            last_detail = "thread_scrape_empty"
        else:
            for m in messages:
                if str(m.get("direction") or "").lower() != "out":
                    continue
                if _thread_body_matches_sent(str(m.get("body") or ""), sent):
                    logger.info(
                        "Messenger: сообщение подтверждено в треде (исходящий пузырь), попытка %s/%s",
                        attempt + 1,
                        max_attempts,
                    )
                    return True, ""
            # Длинный текст: иногда FB/вёрстка помечает наш пузырь как in — смотрим только хвост ленты
            if len(sent) >= 32 and attempt >= max(8, max_attempts // 4):
                tail = messages[-6:] if len(messages) >= 6 else messages
                for m in reversed(tail):
                    if _thread_body_matches_sent(str(m.get("body") or ""), sent):
                        logger.info(
                            "Messenger: сообщение подтверждено в треде (хвост, без строгого out), попытка %s/%s",
                            attempt + 1,
                            max_attempts,
                        )
                        return True, ""
                n_out = sum(1 for x in messages if str(x.get("direction") or "").lower() == "out")
                last_detail = f"tail_{len(tail)}_no_match_out_{n_out}"
            else:
                last_detail = f"scanned_{len(messages)}_no_out_match"
    return False, f"dm_send_unverified:not_visible_in_thread:{last_detail}"[:220]


def scrape_thread_message_texts(page: Page) -> list[str]:
    """Обратная совместимость: только текст, порядок как в треде."""
    return [m["body"] for m in scrape_thread_messages(page)]


def guess_person_id_from_thread(db, thread_id: str) -> int | None:
    """Сопоставление с people.canonical_url по числовому id в URL профиля."""
    if not thread_id or not str(thread_id).isdigit():
        return None
    tid = str(thread_id)
    try:
        from backend.models import Person

        row = (
            db.query(Person)
            .filter(Person.canonical_url.contains(tid))
            .order_by(Person.id)
            .first()
        )
        return row.id if row else None
    except Exception:
        return None


def normalize_peer_url(thread_id: str) -> str:
    return f"https://www.facebook.com/messages/t/{thread_id}"
