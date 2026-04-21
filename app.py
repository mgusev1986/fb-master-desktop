"""
Локальный парсер списка друзей Facebook через Playwright.

Режимы:
- По умолчанию: отдельный Chromium + при старте импорт cookies из Google Chrome
  (полностью закройте Chrome перед запуском, иначе импорт не сработает).
- FB_PARSER_CDP_URL=http://127.0.0.1:9222 — Chrome с CDP и отдельным user-data-dir (chrome_cdp_user_data);
  cookies Facebook импортируются с диска из основного профиля Chrome.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import webbrowser
from io import BytesIO
from urllib.parse import parse_qs, quote, urlparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from openpyxl import Workbook
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from chrome_cookies import facebook_cookies_for_playwright

from backend.services.friends_list_scrape import (
    _build_friends_workbook,
    donor_friends_page_url,
    merge_friend_scan_rows,
)

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"
BASE_FB_DIR = BASE_DIR / "Base FB"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765
UI_URL = f"http://{LISTEN_HOST}:{LISTEN_PORT}/"
CDP_URL = os.environ.get("FB_PARSER_CDP_URL", "").strip()

_pw: Playwright | None = None
_ctx: BrowserContext | None = None
_cdp_browser: Browser | None = None


async def _inject_chrome_cookies(context: BrowserContext) -> int:
    items = facebook_cookies_for_playwright()
    if not items:
        return 0
    try:
        await context.add_cookies(items)
        return len(items)
    except Exception:
        ok = 0
        for c in items:
            try:
                await context.add_cookies([c])
                ok += 1
            except Exception:
                continue
        return ok


async def _random_wait_ms(page, lo_ms: float, hi_ms: float) -> None:
    """Пауза со случайной длительностью (мс), чтобы снизить риск триггеров антибота."""
    await page.wait_for_timeout(int(random.uniform(lo_ms, hi_ms)))


EXTRACT_FRIENDS_JS = """
() => {
  const root = document.querySelector('[role="main"]') || document.body;
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

  root.querySelectorAll('a[href]').forEach((a) => {
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
    const rawName = (a.innerText || '').replace(/\\s+/g, ' ').trim();
    const name = rawName;
    if (!name || name.length < 2 || name.length > 120) return;
    if (/^\\d+$/.test(name)) return;
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


def _owner_slug_from_url(u: str) -> str:
    try:
        p = urlparse(u)
        q = parse_qs(p.query)
        if "profile.php" in p.path and q.get("id") and q["id"][0].isdigit():
            return f"id{q['id'][0]}"
        parts = [x for x in p.path.strip("/").split("/") if x]
        if len(parts) >= 2 and parts[0].lower() == "groups":
            gid = parts[1]
            if gid and re.match(r"^[\w.-]+$", gid):
                return f"group_{gid}"
        skip = {"friends", "friendlist", "people", "sk", "about", "photos", "members"}
        for seg in parts:
            low = seg.lower()
            if low in skip:
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


def _friends_export_filename(owner_from_page: str, page_url: str) -> tuple[str, str]:
    """
    (имя_файла_с_именем_человека, ascii-запасное_из_username_в_URL для старых клиентов).
    """
    from_url = _owner_slug_from_url(page_url)
    raw = (owner_from_page or "").strip() or from_url
    slug = _safe_filename_fragment(raw)
    filename_utf = f"Friends_{slug}.xlsx"
    ascii_slug = _safe_filename_fragment(from_url)
    if not re.search(r"[A-Za-z0-9]", ascii_slug):
        ascii_slug = "profile"
    filename_ascii = f"Friends_{ascii_slug}.xlsx"
    return filename_utf, filename_ascii


def _startup_page_html() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>FB Friends → Excel</title>
  <style>
    :root { font-family: system-ui, sans-serif; }
    body { max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
    label { display: block; font-weight: 600; margin-bottom: .35rem; }
    input[type="url"] { width: 100%; box-sizing: border-box; padding: .5rem .65rem; font-size: 1rem; }
    button { margin-top: .75rem; padding: .55rem 1rem; font-size: 1rem; cursor: pointer; }
    .note { color: #444; font-size: .9rem; margin-top: 1.25rem; }
    code { background: #f0f0f0; padding: .1rem .35rem; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>Список друзей → Excel</h1>
  <p>1) <strong>Вход:</strong> перед запуском приложения полностью закройте Google Chrome (Cmd+Q). При старте программа пытается скопировать cookies Facebook из Chrome в окно Chromium — вы должны сразу оказаться в аккаунте. Если снова видите экран входа — закройте Chrome и перезапустите приложение.</p>
  <p>Альтернатива: <code>Запуск-с-CDP.command</code> — настоящий Google Chrome с отладкой и отдельным профилем в папке проекта; cookies Facebook подставляются из основного Chrome.</p>
  <p>2) Вставьте ссылку на список людей: друзья (<code>…/username/friends</code>), участники группы (<code>…/groups/…/members</code>) или корень группы — откроется вкладка участников.</p>
  <form method="post" action="/export">
    <label for="url">Ссылка на список друзей или участников группы</label>
    <input id="url" name="url" type="url" required placeholder="https://www.facebook.com/…/friends или …/groups/…/members"/>
    <button type="submit">Собрать и скачать Excel</button>
  </form>
  <p class="note"><strong>Как это работает:</strong> вы вставляете ссылку на страницу донора — программа в папке проекта <code>Base FB</code> создаёт файл <code>Friends_Имя_донора.xlsx</code> (имя берётся со страницы Facebook) и <strong>постепенно заполняет</strong> его при прокрутке. При обрыве сети или закрытии вкладки откройте этот же файл в <code>Base FB</code> — там уже будет накопленная база. После успешного завершения браузер также скачает итоговый Excel.</p>
  <p class="note">Автоматический сбор может нарушать правила Meta; большие списки обрабатываются долго.</p>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pw, _ctx, _cdp_browser
    _pw = await async_playwright().start()
    _cdp_browser = None

    if CDP_URL:
        try:
            _cdp_browser = await _pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            await _pw.stop()
            _pw = None
            raise RuntimeError(
                f"CDP: не удалось подключиться к {CDP_URL} (ECONNREFUSED). "
                "Запустите «Запуск-с-CDP.command» — он поднимает Chrome с отдельным профилем "
                "chrome_cdp_user_data (так требует Google). Убедитесь, что порт 9222 слушается: "
                "curl http://127.0.0.1:9222/json/version"
            ) from e
        if _cdp_browser.contexts:
            _ctx = _cdp_browser.contexts[0]
        else:
            _ctx = await _cdp_browser.new_context(locale="ru-RU")
        print(f"[FB Parser] Режим CDP: подключено к {CDP_URL} (Chrome с отдельным профилем chrome_cdp_user_data).")
        n_cdp = await _inject_chrome_cookies(_ctx)
        if n_cdp:
            print(
                f"[FB Parser] CDP: подставлено {n_cdp} cookie из основного профиля Chrome "
                "(он должен быть закрыт при старте скрипта)."
            )
        else:
            print(
                "[FB Parser] CDP: не удалось прочитать cookies основного Chrome — "
                "войдите в Facebook вручную в открывшемся окне (один раз сохранится в chrome_cdp_user_data)."
            )
        _cdp_boot = await _ctx.new_page()
        await _cdp_boot.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    else:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _ctx = await _pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        n = await _inject_chrome_cookies(_ctx)
        if n:
            print(
                f"[FB Parser] Импорт из Chrome: добавлено {n} cookie. "
                "Если Facebook не вошёл в аккаунт — полностью закройте Chrome (Cmd+Q) и перезапустите."
            )
        else:
            print(
                "[FB Parser] Cookies из Chrome не получены (закройте Chrome и перезапустите) "
                "или войдите вручную в открывшемся Chromium."
            )

    if not CDP_URL:
        page = await _ctx.new_page()
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    async def _open_ui_when_ready() -> None:
        await asyncio.sleep(0.9)
        webbrowser.open(UI_URL)

    asyncio.create_task(_open_ui_when_ready())
    yield
    if _cdp_browser:
        await _cdp_browser.close()
        _cdp_browser = None
        _ctx = None
    elif _ctx:
        await _ctx.close()
        _ctx = None
    if _pw:
        await _pw.stop()
        _pw = None


app = FastAPI(lifespan=lifespan, title="FB Friends Excel")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _startup_page_html()


def _atomic_write_workbook(wb: Workbook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    wb.save(tmp)
    tmp.replace(path)


async def _flush_live_workbook(path: Path, by_url: dict[str, str]) -> None:
    if not by_url:
        return

    def _do() -> None:
        wb = _build_friends_workbook(by_url)
        _atomic_write_workbook(wb, path)

    await asyncio.to_thread(_do)


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


async def _scroll_friends_page(page, *, live_path: Path | None = None) -> dict[str, str]:
    """
    Facebook подгружает друзей виртуально: в DOM одновременно только часть карточек,
    старые исчезают при прокрутке. Поэтому на каждом шаге сливаем найденные ссылки
    в один словарь и останавливаемся только когда долго не растёт число УНИКАЛЬНЫХ
    профилей и высота страницы почти не меняется.

    Если задан live_path — периодически перезаписываем полный Excel на диск (на случай
    обрыва сети или закрытия вкладки до завершения ответа сервера).
    """
    merged: dict[str, str] = {}
    last_total = 0
    no_new_rounds = 0
    last_doc_h = 0
    scroll_stable = 0
    max_rounds = 600
    last_save_i = -1
    last_saved_total = 0

    for i in range(max_rounds):
        rows = await page.evaluate(EXTRACT_FRIENDS_JS)
        merge_friend_scan_rows(merged, rows)
        total = len(merged)

        doc_h = int(await page.evaluate(SCROLL_DOC_HEIGHT_JS))

        if total == last_total:
            no_new_rounds += 1
        else:
            no_new_rounds = 0
        last_total = total

        if i == 0:
            last_doc_h = doc_h
        else:
            if abs(doc_h - last_doc_h) < 25:
                scroll_stable += 1
            else:
                scroll_stable = 0
            last_doc_h = max(last_doc_h, doc_h)

        if live_path and total > 0:
            need_save = (
                i == 0
                or (i - last_save_i) >= 3
                or (total - last_saved_total) >= 50
            )
            if need_save:
                await _flush_live_workbook(live_path, merged)
                last_save_i = i
                last_saved_total = total

        # Не выходим слишком рано: мало раундов или ещё крутили список
        if (
            i >= 40
            and no_new_rounds >= 24
            and scroll_stable >= 18
        ):
            if live_path and merged:
                await _flush_live_workbook(live_path, merged)
            break

        vh = await page.evaluate("() => window.innerHeight")
        if i % 6 == 0:
            await page.evaluate(SCROLL_TO_END_JS)
            await _random_wait_ms(page, 2800, 5500)
        else:
            step = max(160, int(vh * random.uniform(0.22, 0.55)))
            await page.evaluate(SCROLL_FRIENDS_STEP_JS, step)
            await _random_wait_ms(page, 2400, 5200)

        if random.random() < 0.18:
            await _random_wait_ms(page, 4000, 9500)
        if random.random() < 0.12:
            await _random_wait_ms(page, 800, 2200)

    if live_path and merged:
        await _flush_live_workbook(live_path, merged)
    return merged


@app.post("/export")
async def export_xlsx(url: str = Form(...)) -> StreamingResponse:
    if not _ctx:
        raise HTTPException(503, "Браузер ещё не готов")
    u = url.strip()
    if not u.startswith("http"):
        raise HTTPException(400, "Нужна полная ссылка https://…")

    BASE_FB_DIR.mkdir(parents=True, exist_ok=True)

    page = await _ctx.new_page()
    try:
        nav_url = donor_friends_page_url(u)
        await page.goto(nav_url, wait_until="domcontentloaded", timeout=120_000)
        await _random_wait_ms(page, 1800, 4200)
        owner_name = (await page.evaluate(OWNER_PAGE_NAME_JS) or "").strip()
        slug = _safe_filename_fragment(owner_name or _owner_slug_from_url(nav_url))
        live_path = BASE_FB_DIR / f"Friends_{slug}.xlsx"
        print(f"[FB Parser] Таблица донора (лайв): {live_path.resolve()}")
        by_url = await _scroll_friends_page(page, live_path=live_path)
    finally:
        await page.close()

    wb = _build_friends_workbook(by_url)
    bio = BytesIO()

    def _save_bio() -> None:
        wb.save(bio)

    await asyncio.to_thread(_save_bio)
    bio.seek(0)
    data = bio.getvalue()

    filename_utf, filename_ascii = _friends_export_filename(owner_name, u)
    cd = (
        f'attachment; filename="{filename_ascii}"; '
        f"filename*=UTF-8''{quote(filename_utf)}"
    )

    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": cd},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=LISTEN_HOST, port=LISTEN_PORT, reload=False)
