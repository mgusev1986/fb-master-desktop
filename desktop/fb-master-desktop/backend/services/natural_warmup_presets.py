"""Пресеты натурального прогрева на 1–7 дней: разные ритмы, лимиты и описание для UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NaturalWarmupPreset:
    """Параметры планировщика и тексты для страницы «Прогрев аккаунтов»."""

    days: int
    title: str
    summary_line: str
    plan_bullets: list[str]
    # Доля периода: группы → профили → лайки (базовая «прогулка»)
    timeline_groups_until: float
    timeline_profiles_until: float
    # Веса выбора следующей сессии (browse = groups|profiles|likes по таймлайну)
    weight_browse: float
    weight_ai_comment: float
    weight_add_friend: float
    # Вес для глубокой «прогулочной» фазы browse
    weight_browse_deep: float
    # С какой доли срока разрешены комментарии / заявки
    comment_after_progress: float
    friend_after_progress: float
    max_comments_per_day: int
    max_friends_per_day: int
    min_seconds_between_ticks: int
    max_ticks_per_calendar_day: int
    min_seconds_between_comments: int
    min_seconds_between_friends: int
    # Множитель длительности одной Playwright-сессии (база задана в actions)
    session_scale: float
    # Верхняя граница циклов прокрутки в ленте групп за сессию
    groups_scroll_rounds_cap: int


# Ключ — число дней (1..7)
PRESETS: dict[int, NaturalWarmupPreset] = {
    1: NaturalWarmupPreset(
        days=1,
        title="Экспресс (1 день)",
        summary_line="Плотный просмотр за сутки: мало соц.действий, короткие паузы между заходами.",
        plan_bullets=[
            "Группы и лента: укороченные фазы, больше смен контекста за день.",
            "Глубокие прогулки: раскрытие постов, просмотр Reels, ответы в комментариях.",
            "До 3 комментариев с помощью ИИ и до 3 заявок в друзья (если выпадет фаза и лимит времени).",
            "Интервал между заходами ~2 ч, до 7 заходов за сутки. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут: глубокий скролл, чтение, Reels.",
        ],
        timeline_groups_until=0.22,
        timeline_profiles_until=0.48,
        weight_browse=5.5,
        weight_ai_comment=1.6,
        weight_add_friend=1.4,
        weight_browse_deep=3.5,
        comment_after_progress=0.48,
        friend_after_progress=0.52,
        max_comments_per_day=3,
        max_friends_per_day=3,
        min_seconds_between_ticks=7_200,     # 2 часа
        max_ticks_per_calendar_day=7,
        min_seconds_between_comments=5_400,
        min_seconds_between_friends=7_200,
        session_scale=1.1,
        groups_scroll_rounds_cap=30,
    ),
    2: NaturalWarmupPreset(
        days=2,
        title="Ускоренный (2 дня)",
        summary_line="Чуть больше социальных действий, чем за 1 день; частые заходы с глубоким чтением.",
        plan_bullets=[
            "Таймлайн смещён к раннему просмотру групп и ленты.",
            "Глубокие прогулки: раскрытие постов, просмотр Reels, ответы в комментариях.",
            "До 5 комментариев и до 5 заявок в друзья в сутки.",
            "Интервал ~2 ч между заходами, до 7 заходов в день. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут: глубокий скролл, чтение, Reels.",
        ],
        timeline_groups_until=0.26,
        timeline_profiles_until=0.52,
        weight_browse=5.0,
        weight_ai_comment=2.4,
        weight_add_friend=2.0,
        weight_browse_deep=3.8,
        comment_after_progress=0.42,
        friend_after_progress=0.48,
        max_comments_per_day=5,
        max_friends_per_day=5,
        min_seconds_between_ticks=7_200,     # 2 часа
        max_ticks_per_calendar_day=7,
        min_seconds_between_comments=6_300,
        min_seconds_between_friends=9_000,
        session_scale=1.15,
        groups_scroll_rounds_cap=28,
    ),
    3: NaturalWarmupPreset(
        days=3,
        title="Стандарт (3 дня)",
        summary_line="Сбалансированный режим: группы → профили → лайки с глубоким чтением и длинными заходами.",
        plan_bullets=[
            "Первая треть срока — упор на ленту групп, затем профили из главной, затем лайки.",
            "Глубокие прогулки: раскрытие постов, раскрытие ответов, просмотр Reels (без звука).",
            "До 10 комментариев с помощью ИИ и до 10 заявок в друзья в сутки (с длинными паузами между ними).",
            "Интервал ~2–2.5 ч между заходами, до 6 заходов в день. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут: глубокий скролл ленты, групп, профилей, Reels.",
        ],
        timeline_groups_until=0.34,
        timeline_profiles_until=0.67,
        weight_browse=4.0,
        weight_ai_comment=4.0,
        weight_add_friend=3.5,
        weight_browse_deep=4.2,
        comment_after_progress=0.34,
        friend_after_progress=0.45,
        max_comments_per_day=10,
        max_friends_per_day=10,
        min_seconds_between_ticks=9_000,     # 2.5 часа
        max_ticks_per_calendar_day=6,
        min_seconds_between_comments=7_200,
        min_seconds_between_friends=10_800,
        session_scale=1.2,
        groups_scroll_rounds_cap=28,
    ),
    4: NaturalWarmupPreset(
        days=4,
        title="Спокойный (4 дня)",
        summary_line="Чуть реже действия в ленте и длинные «тихие» заходы с глубоким чтением.",
        plan_bullets=[
            "Фазы групп/профилей растянуты: больше времени на «прогулку» без лайков.",
            "Глубокие прогулки: раскрытие постов, Reels, ответы в комментариях.",
            "До 8 комментариев и до 8 заявок в друзья в сутки.",
            "Интервал ~2.5 ч, до 6 заходов в день. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут глубокого просмотра.",
        ],
        timeline_groups_until=0.36,
        timeline_profiles_until=0.68,
        weight_browse=4.6,
        weight_ai_comment=3.2,
        weight_add_friend=2.8,
        weight_browse_deep=4.5,
        comment_after_progress=0.36,
        friend_after_progress=0.46,
        max_comments_per_day=8,
        max_friends_per_day=8,
        min_seconds_between_ticks=9_000,     # 2.5 часа
        max_ticks_per_calendar_day=6,
        min_seconds_between_comments=7_800,
        min_seconds_between_friends=11_400,
        session_scale=1.25,
        groups_scroll_rounds_cap=25,
    ),
    5: NaturalWarmupPreset(
        days=5,
        title="Плавный (5 дней)",
        summary_line="Мягкий ритм без спешки; длинные заходы, активности в ленте умеренные.",
        plan_bullets=[
            "Длиннее фаза групп, затем неспешный просмотр чужих страниц из ленты.",
            "Глубокие прогулки: раскрытие постов, просмотр Reels, ответы в комментариях.",
            "До 8 комментариев и до 9 заявок в друзья в сутки.",
            "Интервал ~2.5–3 ч, до 5 заходов в день. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут: глубокий скролл ленты, групп, Reels.",
        ],
        timeline_groups_until=0.38,
        timeline_profiles_until=0.70,
        weight_browse=4.8,
        weight_ai_comment=3.0,
        weight_add_friend=2.9,
        weight_browse_deep=4.8,
        comment_after_progress=0.37,
        friend_after_progress=0.47,
        max_comments_per_day=8,
        max_friends_per_day=9,
        min_seconds_between_ticks=10_800,    # 3 часа
        max_ticks_per_calendar_day=5,
        min_seconds_between_comments=8_400,
        min_seconds_between_friends=12_000,
        session_scale=1.3,
        groups_scroll_rounds_cap=24,
    ),
    6: NaturalWarmupPreset(
        days=6,
        title="Осторожный (6 дней)",
        summary_line="Минимум резких действий; много спокойного чтения, лимиты активности умеренные.",
        plan_bullets=[
            "Таймлайн ближе к «долгому знакомству» с лентой и группами.",
            "Глубокие прогулки: раскрытие постов, Reels, ответы в комментариях.",
            "До 7 комментариев и до 9 заявок в друзья в сутки.",
            "Интервал ~3 ч, до 5 заходов в день. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут глубокого просмотра.",
        ],
        timeline_groups_until=0.40,
        timeline_profiles_until=0.71,
        weight_browse=5.0,
        weight_ai_comment=2.6,
        weight_add_friend=2.5,
        weight_browse_deep=5.0,
        comment_after_progress=0.38,
        friend_after_progress=0.48,
        max_comments_per_day=7,
        max_friends_per_day=9,
        min_seconds_between_ticks=10_800,    # 3 часа
        max_ticks_per_calendar_day=5,
        min_seconds_between_comments=9_000,
        min_seconds_between_friends=12_600,
        session_scale=1.35,
        groups_scroll_rounds_cap=22,
    ),
    7: NaturalWarmupPreset(
        days=7,
        title="Мягкий (7 дней)",
        summary_line="Самый «человечный» темп: длинные паузы, длинные заходы с глубоким чтением.",
        plan_bullets=[
            "Почти половина срока с упором на группы и спокойное чтение лент.",
            "Глубокие прогулки: раскрытие постов, просмотр Reels, ответы в комментариях.",
            "До 6 комментариев и до 6 заявок в друзья в сутки — без спешки.",
            "Интервал ~3 ч между заходами, до 5 заходов в день. Активность с 8:00 до 22:00.",
            "Каждый заход 15–30 минут; имитация неспешных прогулок по Facebook.",
        ],
        timeline_groups_until=0.44,
        timeline_profiles_until=0.74,
        weight_browse=5.5,
        weight_ai_comment=2.2,
        weight_add_friend=2.0,
        weight_browse_deep=5.5,
        comment_after_progress=0.40,
        friend_after_progress=0.50,
        max_comments_per_day=6,
        max_friends_per_day=6,
        min_seconds_between_ticks=10_800,    # 3 часа
        max_ticks_per_calendar_day=5,
        min_seconds_between_comments=10_800,
        min_seconds_between_friends=14_400,
        session_scale=1.4,
        groups_scroll_rounds_cap=22,
    ),
}


def get_preset(duration_days: int) -> NaturalWarmupPreset:
    d = max(1, min(7, int(duration_days)))
    return PRESETS.get(d, PRESETS[3])


def preset_ui_dict(p: NaturalWarmupPreset) -> dict:
    """Сериализация для шаблона и JSON в браузере."""
    return {
        "days": p.days,
        "title": p.title,
        "summary": p.summary_line,
        "bullets": list(p.plan_bullets),
        "max_comments_per_day": p.max_comments_per_day,
        "max_friends_per_day": p.max_friends_per_day,
    }


WARMUP_PRESETS_UI: dict[str, dict] = {str(d): preset_ui_dict(PRESETS[d]) for d in sorted(PRESETS.keys())}


def _client_warmup_ui_text(s: str) -> str:
    """Подписи плана прогрева без техничных слов (сессии, календарный день и т.п.)."""
    t = s
    t = t.replace("«тихие» сессии просмотра", "«тихие» заходы только для просмотра")
    t = t.replace("Сессии слегка удлинены", "Заходы чуть длиннее")
    t = t.replace("Сессии самые длинные", "Заходы самые длинные")
    t = t.replace("между сессиями", "между заходами")
    t = t.replace("Сессии ", "Заходы ")
    t = t.replace("сессиями", "заходами")
    t = t.replace("сессии чуть", "заходы чуть")
    t = t.replace("календарный день", "день")
    t = t.replace("соц.действия", "действия в ленте")
    t = t.replace("соц.лимиты", "лимиты активности")
    t = t.replace("Нейрокомментарии ", "Комментарии с помощью ИИ ")
    t = t.replace("нейрокомментариев", "комментариев с помощью ИИ")
    t = t.replace("нейрокомментарии", "комментарии с помощью ИИ")
    t = t.replace(" (модерация).", " (посты отбираются автоматически).")
    t = t.replace("(модерация)", "(автоматическая проверка постов)")
    t = t.replace("Печать комментария", "Набор текста комментария")
    return t


def preset_ui_dict_client(p: NaturalWarmupPreset) -> dict:
    d = preset_ui_dict(p)
    d["summary"] = _client_warmup_ui_text(d["summary"])
    d["bullets"] = [_client_warmup_ui_text(b) for b in d["bullets"]]
    return d


WARMUP_PRESETS_UI_CLIENT: dict[str, dict] = {
    str(d): preset_ui_dict_client(PRESETS[d]) for d in sorted(PRESETS.keys())
}
