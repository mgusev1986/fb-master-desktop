"""Стоп-темы для нейрокомментариев при натуральном прогреве: секс, эротика, религия и близкие слова (БД + значения по умолчанию)."""

from __future__ import annotations

import re
from typing import Sequence

from sqlalchemy.orm import Session

from backend.models import Setting

NATURAL_WARMUP_TOPIC_STOPWORDS_KEY = "cabinet_natural_warmup_topic_stopwords"

# Базовый набор при пустой/отсутствующей записи в БД (можно расширить в разделе «Прогрев аккаунтов»).
DEFAULT_TOPIC_STOPWORDS: tuple[str, ...] = (
    # — секс / эротика (RU)
    "секс",
    "сексуал",
    "эротик",
    "порно",
    "порнограф",
    "интим",
    "проститут",
    "эскорт",
    "фетиш",
    "мастурб",
    "возбужд",
    "генитал",
    "пенис",
    "вагин",
    "клитор",
    "эрекц",
    "минет",
    "сперм",
    "оргазм",
    "страпон",
    "онлифанс",
    "onlyfans",
    # — секс / эротика (EN)
    "sex",
    "sexual",
    "porn",
    "porno",
    "erotic",
    "erotica",
    "nsfw",
    "nude",
    "naked",
    "nudity",
    "pornography",
    "xxx",
    "escort",
    "prostitut",
    "masturbat",
    "orgasm",
    "dildo",
    "blowjob",
    "handjob",
    "fetish",
    "hardcore",
    "softcore",
    "hentai",
    "milf",
    "bdsm",
    # — религия (RU)
    "религи",
    "аллах",
    "аллаху",
    "ислам",
    "мусульман",
    "молитв",
    "намаз",
    "халифат",
    "джихад",
    "шариат",
    "коран",
    "хиджаб",
    "нимб",
    "икона",
    "иконы",
    "церков",
    "храм",
    "монастыр",
    "священник",
    "священнослужител",
    "епископ",
    "протоиерей",
    "митрополит",
    "патриарх",
    "мулла",
    "имам",
    "раввин",
    "синагог",
    "мечет",
    "кааба",
    "иудей",
    "иудаизм",
    "крещен",
    "исповед",
    "причаст",
    "евангел",
    "библи",
    "ветхий завет",
    "новый завет",
    "христос",
    "христиан",
    "православ",
    "католик",
    "протестант",
    "иисус",
    "буддизм",
    "буддист",
    "индуизм",
    "кришна",
    "реинкарнац",
    "сикх",
    "даос",
    "оккульт",
    "сатан",
    "культ",
    "секта",
    # — религия (EN)
    "allah",
    "islam",
    "islamic",
    "muslim",
    "quran",
    "koran",
    "mosque",
    "imam",
    "jihad",
    "sharia",
    "jewish",
    "judaism",
    "synagogue",
    "rabbi",
    "torah",
    "talmud",
    "bible",
    "scripture",
    "gospel",
    "jesus",
    "christ",
    "christian",
    "catholic",
    "protestant",
    "orthodox",
    "church",
    "pastor",
    "priest",
    "bishop",
    "pope",
    "vatican",
    "prayer",
    "pray",
    "praying",
    "religion",
    "religious",
    "theolog",
    "buddh",
    "hindu",
    "krishna",
    "cult",
    "sect",
    "occult",
    "satan",
    "lucifer",
    "pagan",
    "wicca",
)


def _normalize_words(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[\n,;]+", raw)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def get_effective_topic_stopwords(db: Session) -> list[str]:
    row = db.get(Setting, NATURAL_WARMUP_TOPIC_STOPWORDS_KEY)
    parsed = _normalize_words(row.value) if row and row.value is not None else []
    if not parsed:
        return list(DEFAULT_TOPIC_STOPWORDS)
    out: list[str] = []
    seen: set[str] = set()
    for w in parsed:
        k = w.casefold()
        if len(k) < 2 or k in seen:
            continue
        seen.add(k)
        out.append(w.strip())
        if len(out) >= 600:
            break
    return out if out else list(DEFAULT_TOPIC_STOPWORDS)


def post_text_matches_topic_stopwords(post_text: str, words: Sequence[str]) -> bool:
    if not (post_text or "").strip():
        return False
    hay = (post_text or "").casefold()
    for raw in words:
        w = (raw or "").strip().casefold()
        if len(w) < 2:
            continue
        if " " in w:
            if w in hay:
                return True
            continue
        pat = r"(?<!\w)" + re.escape(w) + r"(?!\w)"
        if re.search(pat, hay, re.UNICODE):
            return True
    return False


def parse_stopwords_form_text(text: str) -> list[str]:
    """Разбор многострочного поля: строки и запятые, без пустых дублей."""
    parts = re.split(r"[\n\r,;]+", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        s = p.strip()
        if len(s) < 2 or len(s) > 80:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= 600:
            break
    return out


def save_topic_stopwords(db: Session, words: list[str]) -> None:
    row = db.get(Setting, NATURAL_WARMUP_TOPIC_STOPWORDS_KEY)
    if not words:
        if row:
            db.delete(row)
        db.commit()
        return
    if row:
        row.value = words
    else:
        db.add(Setting(key=NATURAL_WARMUP_TOPIC_STOPWORDS_KEY, value=words))
    db.commit()


__all__ = [
    "DEFAULT_TOPIC_STOPWORDS",
    "NATURAL_WARMUP_TOPIC_STOPWORDS_KEY",
    "get_effective_topic_stopwords",
    "parse_stopwords_form_text",
    "post_text_matches_topic_stopwords",
    "save_topic_stopwords",
]
