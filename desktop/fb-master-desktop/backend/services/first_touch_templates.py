"""Готовые англоязычные шаблоны первого сообщения в ЛС (плейсхолдер {{first_name}})."""

from __future__ import annotations

import random

# Варианты с лёгкой рандомизацией формулировок; NAME из примеров пользователя → {{first_name}}
FIRST_TOUCH_DM_EN: list[str] = [
    "{{first_name}}, hey! How are you doing? I'd be glad to connect with you on Instagram. I'm Alex, working in blockchain products and startup marketing, and it seems like we're in a similar space. Would you be open to discovering something new today?",
    "Hi {{first_name}}, how's it going? I'd really like to connect on Instagram. My name is Alex, and I'm involved in blockchain product development and startup marketing. I feel like we're in the same industry. Are you open to exploring something new right now?",
    "Hey {{first_name}}, hope you're doing well. I'd love to add you on Instagram. I'm Alex — I work in blockchain product and startup marketing, and it looks like we have similar professional interests. Are you open to learning something new at the moment?",
    "{{first_name}}, hi there! How have you been? I'd love to connect on Instagram. I'm Alex, working in blockchain products and startup marketing, and I believe we're in the same niche. Would you be interested in hearing something new right now?",
    "Hi {{first_name}}, hope all is well. I'd like to connect with you on Instagram. My name is Alex, and I'm in blockchain product and startup marketing. I think we're working in a similar field. Are you open to learning about something new today?",
    "Hi {{first_name}}, how are you? I'd love to connect with you on Instagram. I'm Alex, and I work in blockchain products and startup marketing. It seems like we're in a similar field. Would you be open to learning something new today?",
    "Hey {{first_name}}, hope you're doing well. I'd be happy to connect on Instagram. My name is Alex, and I'm involved in blockchain product development and startup marketing. I think we're in the same industry. Are you open to exploring something new right now?",
    "Hello {{first_name}}, how's everything going? I'd really like to connect with you on Instagram. I'm Alex, working in blockchain product and startup marketing, and it feels like we share the same professional space. Are you open to hearing something new today?",
    "Hi {{first_name}}, hope you're having a good day. I'd love to add you on Instagram. I'm Alex, and I work in blockchain product and startup marketing. I believe we're in a similar niche. Would you be open to discovering something new right now?",
    "Hey {{first_name}}, how are things? I'd like to connect with you on Instagram. My name is Alex, and I'm working in blockchain products and startup marketing. It looks like we're in the same field. Are you open to learning something new at the moment?",
]


def pick_first_touch_templates(k: int, rng: random.Random | None = None) -> list[str]:
    """Вернуть k шаблонов (с повтором, если k > числа шаблонов)."""
    r = rng if rng is not None else random.Random()
    pool = FIRST_TOUCH_DM_EN[:]
    if k <= len(pool):
        return r.sample(pool, k=k)
    out: list[str] = []
    for _ in range(k):
        out.append(r.choice(pool))
    return out
