"""LinkedIn runtime — Playwright-обёртки для реальных действий в LinkedIn.

Все действия идут через persistent Chrome-профиль аккаунта (profile_dir),
с восстановлением сохранённых cookies перед навигацией. Это даёт LinkedIn
обычную браузерную сессию — без меток webdriver и без missing language
headers (как мы делаем для FB).

Структура:
  * playwright_session  — базовая обёртка, контекст-менеджер.
  * readiness_probe     — быстрая проверка «жив ли логин».
  * dm_sender           — отправка DM 1st-degree connection.
  * invitation_sender   — отправка connection invitation с note.
  * inbox_sync          — синхронизация входящих сообщений в БД.
  * outreach_worker     — фоновой воркер по очереди кампаний.
  * audience_scraper    — поиск аудитории через LinkedIn Search.

Контракт ошибок: каждая операция возвращает dict
`{"ok": bool, "reason_code": str|None, "details": str|None, ...}`.
Никаких неперехваченных exceptions наружу — runtime изолирован.
"""
