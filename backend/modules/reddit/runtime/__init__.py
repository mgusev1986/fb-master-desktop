"""Reddit runtime (browser-mode) — Playwright-обёртки.

Аналог `backend/modules/linkedin/runtime/`. Все действия идут через
persistent Chromium-профиль аккаунта, с восстановлением cookies
(`reddit_session` / `token_v2`) перед навигацией.

Структура:
  * playwright_session  — базовая обёртка, контекст-менеджер.
  * human_behavior      — anti-detect helpers (мышь, скролл, idle).
  * readiness_probe     — проверка живости сессии.
  * dm_sender           — отправка PM (старый /message/compose) или Chat (новый overlay).
  * comment_sender      — публикация комментария к посту.
  * outreach_worker     — фоновой воркер по очереди кампаний.

Контракт ошибок: каждая операция возвращает dict
`{ok, reason_code?, details?, ...}`. Ничего не падает наружу.
"""
