"""Понятные для оператора тексты ошибок рассылки (вместо dm=fail:dm_composer_not_found:none)."""

from __future__ import annotations

import re

# Ошибки ротации / слотов (целиком в row.error)
_ROTATION_RU: dict[str, str] = {
    "daily_cap_all_accounts": "На всех аккаунтах кампании исчерпан суточный лимит рассылки.",
    "need_active_slot": "Нет аккаунта в активном слоте 1–3 — назначьте слот или включите авто-слот.",
    "no_slot_in_pool": "Не удалось переназначить слот между аккаунтами пула.",
    "no_active_slot": "Выбранный аккаунт не в активном слоте.",
    "empty_pool": "Не выбраны аккаунты кампании.",
    "missing_account": "Аккаунт не найден или недоступен.",
}

# Коды из try_send_dm_from_profile / run_outreach_on_profile (хвост после dm=fail:)
_DM_TAIL_EXACT: dict[str, str] = {
    "empty_url": "У контакта нет корректной ссылки на профиль Facebook.",
    "empty_message": "Пустой текст сообщения.",
    "empty_dm": "Пустой текст личного сообщения.",
    "redirected_to_messages_new": (
        "Facebook открыл экран «Новое сообщение» вместо чата с этим человеком — "
        "не удалось перейти в обычный диалог Messenger. Часто так бывает при ограничениях чата, "
        "редиректе на мобильную версию или смене интерфейса. Попробуйте позже или откройте переписку вручную."
    ),
    "redirected_to_messages_new_before_type": (
        "Перед вводом текста Facebook оставил страницу «Новое сообщение» вместо окна переписки — "
        "автоматика прервала отправку, чтобы сообщение не ушло не тому адресату. "
        "Проверьте, открывается ли чат с контактом вручную; при необходимости снимите контакт с очереди и повторите позже."
    ),
    "focused_on_recipient_field": (
        "Курсор оказался в поле «Кому», а не в поле сообщения — отправка отменена, чтобы текст не попал адресату по ошибке."
    ),
    "typed_in_recipient_field_on_messages_new": (
        "После ввода текста всё ещё открыт экран «Новое сообщение» — похоже, символы попали в поле получателя, а не в сообщение. Отправка отменена."
    ),
    "message_not_sent_still_on_messages_new": (
        "После нажатия «Отправить» страница осталась на «Новое сообщение» — не удалось убедиться, что ЛС ушло в чат."
    ),
    "dm_sent": "Сообщение отправлено (служебная отметка; при сбое отображения см. диалог в Messenger).",
    "facebook_message_request_limit": (
        "Facebook ограничил отправку запросов в переписку (лимит Meta, обычно на 24 ч). "
        "Рассылка и остальная автоматизация по этому аккаунту переведены на паузу — "
        "дождитесь снятия ограничения и продолжите вручную."
    ),
}

_CYRILLIC_RE = re.compile(r"[а-яё]", re.I)


def _looks_like_machine_code_only(s: str) -> bool:
    """Строка без кириллицы и без ';' — вероятно внутренний код ошибки (один токен или err:…)."""
    t = (s or "").strip()
    if not t or len(t) > 600:
        return False
    if _CYRILLIC_RE.search(t):
        return False
    if ";" in t:
        return False
    if "\n" in t:
        return False
    return True


def _map_err_payload(rest: str) -> str:
    """Суффикс после err: — исключение Playwright / сеть."""
    t = (rest or "").strip()[:500]
    low = t.lower()
    if "timeout" in low:
        return "таймаут: страница или элемент не успели загрузиться (сеть или перегрузка Facebook)."
    if "net::" in low or "econnrefused" in low:
        return "ошибка сети при загрузке страницы."
    if "target closed" in low or "browser has been closed" in low:
        return "браузер был закрыт во время шага."
    return "технический сбой: " + t[:280]


def _map_dm_body(rest: str) -> str:
    r = (rest or "").strip()
    if r in _DM_TAIL_EXACT:
        return _DM_TAIL_EXACT[r]
    if r.startswith("dm_send_unverified:"):
        sub = r.split(":", 2)
        kind = sub[1].strip() if len(sub) > 1 else ""
        if kind == "composer_still_full":
            return (
                "отправка не подтверждена: почти весь текст остался в поле ввода — "
                "Facebook, скорее всего, не принял сообщение. Проверьте диалог в Messenger."
            )
        if kind == "not_visible_in_thread":
            return (
                "отправка не подтверждена: в ленте чата не найден ваш пузырь с этим текстом — "
                "сообщение могло не уйти или интерфейс Messenger не обновился. Проверьте диалог вручную."
            )
        return (
            "отправка не подтверждена автоматикой — проверьте, появилось ли сообщение в диалоге."
        )
    if r.startswith("dm_composer_not_found:"):
        sub = r.split(":", 1)[1].strip() if ":" in r else ""
        if sub == "none":
            return (
                "не найдено поле ввода сообщения — чат Messenger не открылся, "
                "окно перекрыто или интерфейс Facebook изменился (попробуйте ещё раз позже)."
            )
        if sub == "low_confidence":
            return "поле для текста сообщения на странице не распознано (попробуйте обновить страницу)."
        return f"не найдено поле ввода сообщения ({sub})."
    if r.startswith("dm_restricted_or_no_access:"):
        sub = r.split(":", 1)[1].strip() if ":" in r else ""
        return (
            "у этого человека закрыты или недоступны личные сообщения — контакт пропущен, "
            f"аккаунт рассылки не ограничен ({sub})."
        )
    if r in ("message_button_not_found", "message_button_click_failed"):
        return (
            "на профиле нет кнопки «Сообщение» (закрытый профиль, не друг и т.п.) — "
            "контакт пропущен."
        )
    if r.startswith("type:"):
        tail = r[5:].strip()[:200]
        if "timeout" in tail.lower():
            return "не удалось ввести текст: таймаут при наборе (страница подвисла или поле недоступно)."
        return "не удалось ввести текст в поле сообщения (ошибка ввода в браузере)."
    # Неизвестный короткий латинский код — даём нейтральную формулировку + хвост для поддержки
    if len(r) <= 120 and re.match(r"^[a-z0-9_]+$", r, re.I):
        return (
            "не удалось отправить ЛС из-за нестандартного экрана Messenger. "
            f"Код для поддержки: {r}"
        )
    return r[:400] if r else "неизвестная ошибка отправки ЛС."


def _map_like_fail(tail: str) -> str:
    t = (tail or "").strip()
    if not t:
        return "не удалось поставить лайк."
    if "INCOMPLETE" in t or "last_err=" in t:
        return "не удалось поставить лайк (пост не найден, лайк уже стоит или кнопка недоступна)."
    if "js_fail" in t:
        return "не удалось поставить лайк (страница не такая, как ожидалось)."
    return "не удалось поставить лайк."


def _map_friend_fail(tail: str) -> str:
    t = (tail or "").strip()
    if t == "friend_btn_not_found":
        return "не найдена кнопка «Добавить в друзья»."
    return t[:200] if t else "заявка в друзья не отправлена."


def _map_comment_fail(tail: str) -> str:
    t = (tail or "").strip()
    if t.startswith("composer_not_found"):
        return "не найдено поле для комментария."
    if t.startswith("target_post_not_found"):
        return "не найден пост в ленте для комментария."
    if t.startswith("type_failed"):
        return "не удалось ввести текст комментария."
    if t == "empty_text":
        return "пустой текст комментария."
    if t.startswith("nav:"):
        return "ошибка при открытии страницы профиля."
    return t[:240] if t else "комментарий не отправлен."


def humanize_outreach_row_error(raw: str) -> str:
    """
    Текст для колонки «ошибка» в отчёте рассылки.
    Вход: как из Playwright (like=ok:…; dm=fail:dm_composer_not_found:none) или один код ротации.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s in _ROTATION_RU:
        return _ROTATION_RU[s]
    if s.startswith("err:"):
        return "Сбой: " + _map_err_payload(s[4:])
    if "=" not in s and "dm_composer" not in s and "dm_restricted" not in s and "dm_send_unverified" not in s:
        if _looks_like_machine_code_only(s):
            if s in _ROTATION_RU:
                return _ROTATION_RU[s]
            if s in _DM_TAIL_EXACT or s.startswith("type:") or s.startswith("dm_send_unverified:"):
                return _map_dm_body(s)
            if _CYRILLIC_RE.search(s):
                return s[:1000]
            return _map_dm_body(s)
        return s[:1000]

    chunks: list[str] = []
    for part in s.split("; "):
        part = part.strip()
        if not part:
            continue
        if part.startswith("like=ok:") or part.startswith("friend=ok:") or part.startswith("dm=ok:"):
            continue
        if part.startswith("comment=ok:"):
            continue
        if part.startswith("like=fail:"):
            chunks.append("Лайк: " + _map_like_fail(part[9:]))
        elif part.startswith("friend=fail:"):
            chunks.append("Друзья: " + _map_friend_fail(part[12:]))
        elif part.startswith("dm=fail:"):
            chunks.append("ЛС: " + _map_dm_body(part[8:]))
        elif part.startswith("comment=fail:"):
            chunks.append("Комментарий: " + _map_comment_fail(part[14:]))
        elif part.startswith("err:"):
            chunks.append("Сбой: " + _map_err_payload(part[4:]))
        elif part.startswith("dm_composer_not_found:") or part.startswith("dm_restricted"):
            chunks.append("ЛС: " + _map_dm_body(part))
        elif part.startswith("message_button_not_found"):
            chunks.append("ЛС: " + _map_dm_body(part))
        elif len(part) < 90 and part.isascii() and "_" in part and part in _ROTATION_RU:
            chunks.append(_ROTATION_RU[part])
        elif part.startswith("like=") or part.startswith("dm="):
            chunks.append(part[:200])
    if chunks:
        return " ".join(chunks)[:1000]
    return s[:1000]
