"""Parser diagnostic harness — запускает парсер-JS на живой FB-сессии
и сохраняет всё, что мы извлекаем, в parser_diag.json + screenshots.

Запуск (из корня воркtree):
    .venv/bin/python tools/parser_diag.py

С кастомными URL:
    .venv/bin/python tools/parser_diag.py \
        --url "https://facebook.com/groups/611353048997016/members" \
        --url "https://facebook.com/groups/1735576603325381/members" \
        --profile fb_account_1

Что делает:
1) Открывает Chromium с уже сохранённой FB-сессией (browser_profiles/fb_account_X)
2) Навигируется на URL
3) Прогоняет PARSE_EXPECTED_FRIENDS_COUNT_JS, EXTRACT_FRIENDS_JS,
   CONNECTIONS_LIST_STATE_JS — сохраняет ВСЁ что вернулось
4) Делает 5 быстрых scroll'ов и снова EXTRACT — видим прирост per round
5) Сохраняет:
   - parser_diag_<ts>.json (всё в одном)
   - parser_diag_<ts>/screenshots/{N}.png
6) В конце говорит ASCII-сводку: что нашли / что нет

Прислать в чат: zip всей папки parser_diag_<ts>/ + json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.friends_list_scrape import (  # noqa: E402
    EXTRACT_FRIENDS_JS,
    PARSE_EXPECTED_FRIENDS_COUNT_JS,
    CONNECTIONS_LIST_STATE_JS,
    SCROLL_TO_END_ENHANCED_JS,
)

DEFAULT_URLS = [
    "https://www.facebook.com/groups/611353048997016/members",
    "https://www.facebook.com/groups/1735576603325381/members",
]


def parse_account_file(path: Path) -> tuple[str, list[dict]]:
    """phone:pwd:UA|[cookies] → (user_agent, cookies_list)."""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    line = None
    for ln in raw.splitlines():
        if "|[" in ln and ln.count(":") >= 2:
            line = ln.strip()
            break
    if not line:
        raise RuntimeError(f"В {path} не нашёл строку формата phone:pwd:UA|[cookies]")
    head, _, tail = line.partition("|")
    parts = head.split(":")
    user_agent = ":".join(parts[2:]) if len(parts) >= 3 else ""
    cookies_raw = json.loads(tail)
    out = []
    for c in cookies_raw:
        co = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        if "sameSite" in c and c["sameSite"] in ("Strict", "Lax", "None"):
            co["sameSite"] = c["sameSite"]
        if "expirationDate" in c:
            try:
                exp = float(c["expirationDate"])
                # FB иногда даёт unrealistically big values — скипаем
                if exp < 4_000_000_000:
                    co["expires"] = int(exp)
            except Exception:
                pass
        out.append(co)
    return user_agent, out


def parse_proxy_str(s: str) -> dict:
    parts = s.split(":")
    if len(parts) < 2:
        raise RuntimeError(f"Bad proxy format: {s}")
    proxy = {"server": f"http://{parts[0]}:{parts[1]}"}
    if len(parts) >= 4:
        proxy["username"] = parts[2]
        proxy["password"] = parts[3]
    return proxy


def run_js(page, js: str, label: str) -> dict:
    t0 = time.monotonic()
    try:
        result = page.evaluate(js)
        return {
            "ok": True,
            "result": result,
            "ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "ms": int((time.monotonic() - t0) * 1000),
        }


def diag_one_url(page, url: str, screenshots_dir: Path, idx: int) -> dict:
    print(f"\n=== [{idx}] {url}", flush=True)
    rec: dict = {"url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        rec["goto_error"] = str(e)
        return rec
    page.wait_for_timeout(3500)
    rec["final_url"] = page.url
    try:
        rec["title"] = page.title()
    except Exception:
        rec["title"] = ""

    try:
        sp = screenshots_dir / f"{idx:02d}_initial.png"
        page.screenshot(path=str(sp), full_page=False)
        rec["screenshot_initial"] = sp.name
    except Exception as e:
        rec["screenshot_error"] = str(e)

    rec["body_innertext_first_5k"] = run_js(
        page,
        '() => (document.body && document.body.innerText || "").slice(0, 5000)',
        "innerText",
    )
    rec["main_innertext_first_5k"] = run_js(
        page,
        '() => { const m = document.querySelector("[role=main]"); return m ? (m.innerText||"").slice(0,5000) : ""; }',
        "main innerText",
    )

    rec["expected_count"] = run_js(page, PARSE_EXPECTED_FRIENDS_COUNT_JS, "expected_count")
    rec["list_state"] = run_js(page, CONNECTIONS_LIST_STATE_JS, "list_state")
    rec["extract_round_0"] = run_js(page, EXTRACT_FRIENDS_JS, "extract_round_0")
    if isinstance(rec["extract_round_0"].get("result"), list):
        rec["extract_round_0"]["rows_count"] = len(rec["extract_round_0"]["result"])
        rec["extract_round_0"]["sample"] = rec["extract_round_0"]["result"][:5]
        rec["extract_round_0"].pop("result", None)

    rec["scroll_rounds"] = []
    for r in range(1, 6):
        try:
            page.evaluate(SCROLL_TO_END_ENHANCED_JS)
        except Exception as e:
            rec["scroll_rounds"].append({"round": r, "scroll_err": str(e)})
            break
        page.wait_for_timeout(1800)
        ext = run_js(page, EXTRACT_FRIENDS_JS, f"extract_round_{r}")
        rows = ext.get("result")
        rc = len(rows) if isinstance(rows, list) else 0
        ext.pop("result", None)
        ext["rows_count"] = rc
        rec["scroll_rounds"].append({"round": r, **ext})
        if r == 3:
            try:
                sp = screenshots_dir / f"{idx:02d}_round3.png"
                page.screenshot(path=str(sp), full_page=False)
                rec["screenshot_round3"] = sp.name
            except Exception:
                pass

    rec["dom_metrics"] = run_js(
        page,
        '''() => {
            const m = document.querySelector("[role=main]");
            const all_a = document.querySelectorAll("a[href]").length;
            const main_a = m ? m.querySelectorAll("a[href]").length : 0;
            const profile_a = Array.from(document.querySelectorAll("a[href]")).filter(a => {
                const h = a.getAttribute("href") || "";
                return /^\\/[^/]+(\\/?\\?|$)/.test(h) || /\\/profile\\.php\\?id=\\d+/.test(h);
            }).length;
            return {
                all_anchors: all_a,
                main_anchors: main_a,
                profile_like_anchors: profile_a,
                viewport_h: window.innerHeight,
                scroll_y: window.scrollY,
                doc_height: document.documentElement.scrollHeight,
            };
        }''',
        "dom_metrics",
    )

    print(
        f"  expected={rec['expected_count'].get('result')} "
        f"r0={rec['extract_round_0'].get('rows_count', '?')} "
        f"r5={rec['scroll_rounds'][-1].get('rows_count', '?') if rec['scroll_rounds'] else '?'}",
        flush=True,
    )
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[], help="FB URL для теста (можно несколько)")
    ap.add_argument("--profile", default="fb_account_1", help="browser_profiles/<name>")
    ap.add_argument("--headless", action="store_true", help="Headless режим (по умолчанию headed)")
    ap.add_argument("--out-dir", default="parser_diag_results", help="Куда сохранять")
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="Использовать пустой профиль /tmp/parser_diag_fresh — авторизоваться вручную при первом запуске",
    )
    ap.add_argument(
        "--account-file",
        default=None,
        help="Путь к файлу с phone:pwd:UA|[cookies] (DarkStore формат). Использует cookies+proxy вместо профиля.",
    )
    ap.add_argument(
        "--proxy",
        default=None,
        help="host:port:user:pass (для --account-file)",
    )
    args = ap.parse_args()

    urls = args.url or DEFAULT_URLS
    print(f">>> Запуск parser_diag для {len(urls)} URL", flush=True)
    print(f">>> Профиль: {args.profile}", flush=True)
    print(f">>> Режим: {'headless' if args.headless else 'headed (видимый)'}", flush=True)

    # --account-file + --proxy: новый режим через инъекцию cookies + прокси.
    # Самый чистый: новая Chromium-сессия, никаких профильных конфликтов.
    if args.account_file:
        if not args.proxy:
            print("!!! --account-file требует --proxy", flush=True)
            sys.exit(2)
        account_user_agent, account_cookies = parse_account_file(Path(args.account_file))
        proxy_dict = parse_proxy_str(args.proxy)
        print(f">>> Account file: {args.account_file}", flush=True)
        print(f">>> User-Agent: {account_user_agent[:80]}", flush=True)
        print(f">>> Cookies: {len(account_cookies)}", flush=True)
        print(f">>> Proxy: {proxy_dict['server']}", flush=True)
        is_fresh = False
        is_account_mode = True
        profile_dir = None  # не используется
    elif args.fresh:
        from pathlib import Path as _P
        profile_dir = _P("/tmp/parser_diag_fresh")
        profile_dir.mkdir(parents=True, exist_ok=True)
        print(f">>> Используется ПУСТОЙ профиль: {profile_dir}", flush=True)
        is_fresh = True
        is_account_mode = False
    else:
        # browser_profiles может быть в worktree ИЛИ в корне проекта.
        candidate_roots = [ROOT, ROOT.parent.parent.parent]
        src_profile = None
        for cand in candidate_roots:
            cand_dir = cand / "browser_profiles" / args.profile
            if cand_dir.exists():
                src_profile = cand_dir
                break
        if src_profile is None:
            print(f"!!! Profile '{args.profile}' НЕ НАЙДЕН.", flush=True)
            sys.exit(2)
        print(f">>> src_profile: {src_profile}", flush=True)
        # SOCMASTER lock check
        singleton = src_profile / "SingletonLock"
        if singleton.exists() or singleton.is_symlink():
            print("!!! SOCMASTER запущен — закройте его (Cmd+Q).", flush=True)
            print(f"     Или удалите lock: rm -f \"{singleton}\"", flush=True)
            sys.exit(3)
        profile_dir = src_profile
        is_fresh = False
        is_account_mode = False

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / args.out_dir / f"parser_diag_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir = out_dir / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)
    print(f">>> Output: {out_dir}", flush=True)

    from playwright.sync_api import sync_playwright

    diag = {
        "run_id": ts,
        "timestamp": datetime.now().isoformat(),
        "profile": args.profile,
        "headless": args.headless,
        "urls": urls,
        "results": [],
    }

    # Используем дефолтный Playwright Chromium — он автоматически выбирает
    # правильную архитектуру (arm64 на Apple Silicon, x64 на Intel).
    print(">>> используем дефолтный Playwright Chromium (auto-arch)", flush=True)

    # Stealth-патчи чтобы FB не банил headless Chromium как бот.
    # На headless: navigator.webdriver=true, navigator.plugins=[],
    # отсутствует chrome.runtime, и т.д. → FB показывает login page
    # вместо реальной страницы группы (то что мы получали в прошлый раз).
    STEALTH_INIT = """
    // 1. Прячем navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // 2. Эмулируем plugins (FB проверяет!)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}]
    });
    // 3. Эмулируем languages
    Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
    // 4. Прячем automation от Chrome.runtime checks
    window.chrome = window.chrome || { runtime: {} };
    // 5. Permissions query → notification (как в реальном Chrome)
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
        navigator.permissions.query = (p) =>
            p && p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery.call(navigator.permissions, p);
    }
    """

    with sync_playwright() as p:
        if is_account_mode:
            browser = p.chromium.launch(
                headless=args.headless,
                proxy=proxy_dict,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ],
            )
            ctx = browser.new_context(
                user_agent=account_user_agent or None,
                viewport={"width": 1280, "height": 900},
                locale="ru-RU",
                timezone_id="Europe/Istanbul",  # совпадает с TR прокси
            )
            ctx.add_init_script(STEALTH_INIT)
            try:
                ctx.add_cookies(account_cookies)
                print(f">>> Cookies injected: {len(account_cookies)} + stealth patches", flush=True)
            except Exception as e:
                print(f"!!! cookie injection error: {e}", flush=True)
        else:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=args.headless,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            browser = None
        try:
            page = ctx.pages[0] if ctx.pages and not is_account_mode else ctx.new_page()
            if is_fresh:
                # Открываем FB и ждём логина
                try:
                    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                print("\n", flush=True)
                print("=" * 60, flush=True)
                print("ВОЙДИТЕ В FACEBOOK В ОТКРЫВШЕМСЯ ОКНЕ", flush=True)
                print("Когда увидите свою ленту — вернитесь сюда и нажмите Enter", flush=True)
                print("=" * 60, flush=True)
                try:
                    input(">>> Нажмите Enter когда залогинились... ")
                except (KeyboardInterrupt, EOFError):
                    print("Прервано.", flush=True)
                    sys.exit(1)
            for i, url in enumerate(urls, start=1):
                rec = diag_one_url(page, url, screenshots_dir, i)
                diag["results"].append(rec)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    json_path = out_dir / "parser_diag.json"
    json_path.write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n>>> Готово", flush=True)
    print(f">>> JSON:        {json_path}", flush=True)
    print(f">>> Screenshots: {screenshots_dir}", flush=True)
    print("\n>>> Сводка по URL:", flush=True)
    for r in diag["results"]:
        exp = r.get("expected_count", {}).get("result")
        r0 = r.get("extract_round_0", {}).get("rows_count")
        r5 = r["scroll_rounds"][-1].get("rows_count") if r.get("scroll_rounds") else None
        print(f"  {r.get('url','?')[:60]:60s} expected={exp} r0={r0} r5={r5}", flush=True)
    print(f"\n>>> Заархивируйте {out_dir} и пришлите мне.", flush=True)
    print(f">>> Команда для архива:", flush=True)
    print(f"    cd '{out_dir.parent}' && zip -r {out_dir.name}.zip {out_dir.name}", flush=True)


if __name__ == "__main__":
    main()
