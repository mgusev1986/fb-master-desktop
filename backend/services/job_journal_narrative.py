"""Человекочитаемые описания задач журнала (рус.) для раздела «Журнал и отчёты»."""

from __future__ import annotations

import re
from typing import Any

# Короткие подписи к типам событий job_events (для вкладки «Ошибки»)
JOB_EVENT_TYPE_LABELS_RU: dict[str, str] = {
    "messenger_sync_blocked": "Мессенджер: заблокирован экраном (PIN / E2EE)",
    "messenger_sync_error": "Мессенджер: ошибка синхронизации",
    "messenger_send_failed": "Мессенджер: не удалось отправить сообщение",
    "parser_error": "Парсер: ошибка",
    "outreach_error": "Рассылка: ошибка",
    "sequence_error": "Сценарий: ошибка",
    "warmup_error": "Прогрев: ошибка",
    "natural_warmup_session_ok": "Прогрев (натуральный): сессия завершена",
    "natural_warmup_session_fail": "Прогрев (натуральный): сессия с ошибкой",
    "natural_warmup_completed": "Прогрев (натуральный): период завершён",
    "natural_warmup_action_groups_scroll": "Прогрев: скролл ленты групп",
    "natural_warmup_action_home_feed_scroll": "Прогрев: скролл главной ленты",
    "natural_warmup_action_profile_visit": "Прогрев: визит на страницу человека",
    "natural_warmup_action_timeline_like": "Прогрев: лайк в ленте",
    "natural_warmup_action_timeline_like_skip": "Прогрев: лайк не поставлен",
    "natural_warmup_action_ai_comment": "Прогрев: нейрокомментарий",
    "natural_warmup_action_friend_request": "Прогрев: заявка в друзья",
    "natural_warmup_ai_comment_skipped_no_llm": "Прогрев: нейрокомментарий пропущен (нет API-ключа)",
}


def label_job_event_type(event_type: str | None) -> str:
    et = (event_type or "").strip()
    if not et:
        return "Событие"
    return JOB_EVENT_TYPE_LABELS_RU.get(et, et.replace("_", " "))


def humanize_error_summary(msg: str | None) -> tuple[str | None, str]:
    """
    Возвращает (краткая русская подсказка или None, исходный/очищенный текст для деталей).
    Если сообщение уже по-русски и развёрнуто — подсказка может быть None.
    """
    if not msg:
        return None, ""
    raw = msg.strip()
    if not raw:
        return None, ""

    low = raw.lower()
    hints: list[str] = []

    if "wait_for_timeout" in low and ("closed" in low or "has been closed" in low):
        hints.append(
            "Браузер или вкладка закрылись, пока задача ждала ответа страницы. "
            "Не закрывайте окно Chromium до завершения синхронизации или перезапустите задачу."
        )
    elif "target page, context or browser has been closed" in low:
        hints.append(
            "Сессия браузера прервалась (вкладка или окно закрыты, либо сбой). Запустите операцию снова."
        )

    if "timeout" in low and "navigation" in low and not hints:
        hints.append("Страница не успела загрузиться за отведённое время. Проверьте сеть и повторите попытку.")

    if "net::err" in low or "econnrefused" in low:
        hints.append("Похоже на сетевую ошибку или недоступность сайта.")

    if "execution context was destroyed" in low:
        hints.append("Страница перезагрузилась или закрылась во время работы скрипта.")

    # Уже развёрнутые русские тексты (например PIN Messenger) — не дублируем шумом
    cyr_ratio = len(re.findall(r"[а-яёА-ЯЁ]", raw)) / max(len(raw), 1)
    if cyr_ratio > 0.08 and len(raw) > 80:
        return (hints[0] if hints else None), raw

    return (" ".join(hints) if hints else None), raw


def _snap(job: Any) -> dict[str, Any]:
    s = getattr(job, "config_snapshot", None)
    return s if isinstance(s, dict) else {}


def describe_job_intent(
    job: Any,
    *,
    conv_peer_names: dict[int, str],
    outreach_names: dict[int, str],
    sequence_names: dict[int, str],
    warmup_names: dict[int, str],
    donor_name_map: dict[int, str],
    account_map: dict[int, str],
) -> str:
    jt = (getattr(job, "job_type", None) or "").strip()
    snap = _snap(job)

    if jt == "messenger_sync":
        return _describe_messenger(snap, account_map=account_map, conv_peer_names=conv_peer_names)
    if jt == "donor_friends_parse":
        return _describe_parser(snap, donor_name_map=donor_name_map, account_map=account_map)
    if jt == "outreach":
        return _describe_outreach(snap, outreach_names=outreach_names)
    if jt == "sequence":
        return _describe_sequence(snap, sequence_names=sequence_names)
    if jt == "warmup":
        return _describe_warmup(snap, warmup_names=warmup_names)
    if jt == "natural_warmup":
        return _describe_natural_warmup(snap, account_map=account_map)

    return f"Фоновая задача типа «{jt or 'неизвестно'}»."


def _describe_messenger(
    snap: dict[str, Any],
    *,
    account_map: dict[int, str],
    conv_peer_names: dict[int, str],
) -> str:
    sm = snap.get("send_message")
    if isinstance(sm, dict) and (str(sm.get("text") or "")).strip():
        cid_raw = sm.get("conversation_id")
        peer = "диалог"
        if cid_raw is not None and str(cid_raw).strip().isdigit():
            cid = int(cid_raw)
            peer = conv_peer_names.get(cid, f"чат #{cid}")
        return f"Отправка сообщения в Messenger в диалог «{peer}»."

    lt = snap.get("load_thread_conversation_id")
    if lt is not None and str(lt).strip().isdigit():
        cid = int(lt)
        peer = conv_peer_names.get(cid, f"чат #{cid}")
        return f"Загрузка истории сообщений для диалога «{peer}» в Messenger."

    aids = snap.get("account_ids")
    if isinstance(aids, list) and aids:
        labels: list[str] = []
        for a in aids:
            if str(a).strip().isdigit():
                i = int(a)
                labels.append(account_map.get(i, f"аккаунт #{i}"))
        if labels:
            return "Синхронизация списка чатов Messenger с Facebook для: " + ", ".join(labels) + "."

    return "Синхронизация списка чатов Messenger с Facebook (общий запуск без выбора аккаунтов в конфигурации задачи)."


def _describe_parser(
    snap: dict[str, Any],
    *,
    donor_name_map: dict[int, str],
    account_map: dict[int, str],
) -> str:
    dids = snap.get("donor_ids")
    fb = snap.get("fb_account_id")
    fb_l = "аккаунта Facebook"
    if fb is not None and str(fb).strip().isdigit():
        fb_l = account_map.get(int(fb), f"аккаунт #{fb}")

    names: list[str] = []
    if isinstance(dids, list):
        for d in dids:
            if str(d).strip().isdigit():
                names.append(donor_name_map.get(int(d), f"донор #{d}"))

    if names:
        return f"Парсинг списка друзей для {', '.join(names)} с {fb_l}."
    return f"Парсинг списка друзей доноров с {fb_l}."


def _describe_outreach(snap: dict[str, Any], *, outreach_names: dict[int, str]) -> str:
    cid = snap.get("campaign_id")
    name_snap = snap.get("name")
    if isinstance(name_snap, str) and name_snap.strip():
        return f"Рассылка сообщений: «{name_snap.strip()}»."
    if cid is not None and str(cid).strip().isdigit():
        c = int(cid)
        return f"Рассылка сообщений, кампания «{outreach_names.get(c, '#' + str(c))}»."
    return "Рассылка сообщений (кампания в Facebook / очередь)."


def _describe_sequence(snap: dict[str, Any], *, sequence_names: dict[int, str]) -> str:
    cid = snap.get("campaign_id")
    manual = snap.get("manual")
    autotick = snap.get("autotick")
    tail = ""
    if manual:
        tail = " (ручной запуск шага)"
    elif autotick:
        tail = " (автотик сценария)"
    if cid is not None and str(cid).strip().isdigit():
        c = int(cid)
        return f"Сценарий по дням «{sequence_names.get(c, '#' + str(c))}»{tail}."
    return f"Выполнение сценария по дням{tail}."


def _describe_warmup(snap: dict[str, Any], *, warmup_names: dict[int, str]) -> str:
    cid = snap.get("campaign_id")
    if cid is not None and str(cid).strip().isdigit():
        c = int(cid)
        return f"Прогрев аккаунтов, кампания «{warmup_names.get(c, '#' + str(c))}»."
    return "Прогрев аккаунтов (лайки, комментарии по расписанию)."


def _describe_natural_warmup(snap: dict[str, Any], *, account_map: dict[int, str]) -> str:
    fb = snap.get("fb_account_id")
    acc_l = "аккаунта Facebook"
    if fb is not None and str(fb).strip().isdigit():
        acc_l = account_map.get(int(fb), f"аккаунт #{fb}")
    phase = (snap.get("phase") or "").strip() or "неизвестно"
    phase_ru = {
        "groups": "скролл групп",
        "profiles": "лента и профили",
        "likes": "лайки в ленте",
        "ai_comment": "нейрокомментарий",
        "add_friend": "заявка в друзья",
    }.get(phase, phase.replace("_", " "))
    title = (snap.get("preset_title") or "").strip()
    days = snap.get("duration_days")
    tail = ""
    if title:
        tail = f" ({title}"
        if days is not None and str(days).strip().isdigit():
            tail += f", {int(days)} дн."
        tail += ")"
    elif days is not None and str(days).strip().isdigit():
        tail = f" ({int(days)} дн.)"
    return f"Натуральный прогрев{tail}: шаг «{phase_ru}» для {acc_l}."


def build_journal_context_by_job_id(
    jobs: list[Any],
    *,
    conv_peer_names: dict[int, str],
    outreach_names: dict[int, str],
    sequence_names: dict[int, str],
    warmup_names: dict[int, str],
    donor_name_map: dict[int, str],
    account_map: dict[int, str],
) -> dict[int, dict[str, Any]]:
    """Для шаблона: id задачи → intent, подсказка по ошибке, сырой текст ошибки."""
    out: dict[int, dict[str, Any]] = {}
    for job in jobs:
        jid = getattr(job, "id", None)
        if jid is None:
            continue
        intent = describe_job_intent(
            job,
            conv_peer_names=conv_peer_names,
            outreach_names=outreach_names,
            sequence_names=sequence_names,
            warmup_names=warmup_names,
            donor_name_map=donor_name_map,
            account_map=account_map,
        )
        err = getattr(job, "error_summary", None)
        hint, raw_err = humanize_error_summary(err if isinstance(err, str) else None)
        out[int(jid)] = {
            "intent_ru": intent,
            "error_hint_ru": hint,
            "error_raw": raw_err,
        }
    return out


def collect_journal_lookup_ids(jobs: list[Any]) -> dict[str, set[int]]:
    conv_ids: set[int] = set()
    outreach_ids: set[int] = set()
    sequence_ids: set[int] = set()
    warmup_ids: set[int] = set()

    for job in jobs:
        snap = _snap(job)
        jt = (getattr(job, "job_type", None) or "").strip()

        if jt == "messenger_sync":
            lt = snap.get("load_thread_conversation_id")
            if lt is not None and str(lt).strip().isdigit():
                conv_ids.add(int(lt))
            sm = snap.get("send_message")
            if isinstance(sm, dict) and sm.get("conversation_id") is not None:
                cr = sm.get("conversation_id")
                if str(cr).strip().isdigit():
                    conv_ids.add(int(cr))

        cid = snap.get("campaign_id")
        if cid is not None and str(cid).strip().isdigit():
            c = int(cid)
            if jt == "outreach":
                outreach_ids.add(c)
            elif jt == "sequence":
                sequence_ids.add(c)
            elif jt == "warmup":
                warmup_ids.add(c)

    return {
        "conversation": conv_ids,
        "outreach": outreach_ids,
        "sequence": sequence_ids,
        "warmup": warmup_ids,
    }
