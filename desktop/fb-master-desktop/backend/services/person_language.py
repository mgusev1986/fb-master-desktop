"""Сегментация контактов по языку: русскоязычные / англоязычные / иностранные."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import Page
from sqlalchemy import or_

from backend.models import Person

LANG_SEGMENT_UNKNOWN = "unknown"
LANG_SEGMENT_RU = "ru"
LANG_SEGMENT_EN = "en"
LANG_SEGMENT_FOREIGN = "foreign"

LANGUAGE_SEGMENTS = (
    LANG_SEGMENT_RU,
    LANG_SEGMENT_EN,
    LANG_SEGMENT_FOREIGN,
    LANG_SEGMENT_UNKNOWN,
)
LANGUAGE_FILTERS = ("all", "ru", "en", "non_ru", "unknown")

LANGUAGE_FILTER_LABELS = {
    "all": "Все языки",
    "ru": "Русскоговорящие",
    "en": "Англоговорящие",
    "non_ru": "Иностранные (не RU)",
    "unknown": "Не определено",
}

LANGUAGE_SEGMENT_LABELS = {
    LANG_SEGMENT_RU: "Русскоговорящий",
    LANG_SEGMENT_EN: "Англоговорящий",
    LANG_SEGMENT_FOREIGN: "Иностранный",
    LANG_SEGMENT_UNKNOWN: "Не определён",
}

LANGUAGE_SEGMENT_BADGES = {
    LANG_SEGMENT_RU: "badge-success",
    LANG_SEGMENT_EN: "badge-default",
    LANG_SEGMENT_FOREIGN: "badge-warning",
    LANG_SEGMENT_UNKNOWN: "badge-default",
}

_CYR_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
_RU_STRICT_RE = re.compile(r"[А-Яа-яЁё]")
_LAT_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]")
_LAT_EXT_RE = re.compile(r"[À-ÖØ-öø-ÿĀ-žĐđĂăÂâÊêÔôƠơƯưÇçÑñß]")
_UA_RE = re.compile(r"[ЇїЄєҐґІі]")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґÀ-ÖØ-öø-ÿĀ-žĐđĂăÂâÊêÔôƠơƯưÇçÑñß]+")
_VIET_RE = re.compile(r"[ăâđêôơưĂÂĐÊÔƠƯ]")
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")

_RU_STOP = {
    "и", "в", "во", "не", "что", "он", "она", "они", "мы", "вы", "ты", "это", "я",
    "на", "с", "со", "как", "а", "то", "все", "она", "так", "его", "ее", "её", "но",
    "да", "к", "у", "же", "за", "бы", "по", "только", "из", "о", "от", "для", "до",
    "или", "если", "уже", "ещё", "еще", "кто", "где", "когда", "будет", "был", "была",
    "быть", "очень", "мне", "тебя", "тебе", "нас", "вам", "наш", "ваш", "привет",
    "добрый", "день", "вечер", "спасибо", "интересно", "сейчас",
}

_EN_STOP = {
    "the", "and", "you", "your", "for", "with", "this", "that", "from", "are", "was",
    "were", "have", "has", "had", "not", "but", "about", "into", "out", "our", "their",
    "his", "her", "they", "them", "what", "when", "where", "who", "how", "hello", "hi",
    "thanks", "thank", "please", "today", "great", "new", "message", "work", "business",
    "marketing", "service", "official", "group", "team", "contact", "idea",
}

_NON_EN_HINTS = {
    "hola", "bonjour", "gracias", "merci", "guten", "danke", "ciao", "buongiorno",
    "xin", "chao", "cam", "ơn", "obrigado", "ola", "olá", "salut",
}

_EN_NAME_HINTS = {
    "official", "studio", "design", "group", "service", "marketing", "team", "digital",
    "club", "shop", "store", "agency", "beauty", "realty", "order",
}

_RUS_PATRONYMIC_SUFFIXES = (
    "ович", "евич", "ич", "овна", "евна", "ична", "кызы", "оглы",
)

_EXTRACT_PROFILE_LANGUAGE_CONTEXT_JS = """({ maxPosts }) => {
  const main = document.querySelector('[role="main"]');
  if (!main) return { introText: '', postTexts: [] };

  function cleanText(raw) {
    return String(raw || '')
      .replace(/https?:\\/\\/\\S+/g, ' ')
      .replace(/\\s*\\n\\s*/g, '\\n')
      .replace(/\\n{3,}/g, '\\n\\n')
      .replace(/[ \\t]{2,}/g, ' ')
      .trim();
  }

  function isTopLevelArticle(art) {
    let p = art.parentElement;
    while (p) {
      if (p.getAttribute && p.getAttribute('role') === 'article') return false;
      p = p.parentElement;
    }
    return true;
  }

  function isMeaningfulPost(text) {
    const head = text.slice(0, 220).toLowerCase();
    return !(
      head.includes('people you may know') ||
      head.includes('people you know') ||
      head.includes('suggested for you') ||
      head.includes('возможные друзья') ||
      head.includes('рекомендуемые друзья') ||
      head.includes('zobrazit viac') ||
      head.includes('see translation')
    );
  }

  const articles = Array.from(main.querySelectorAll('[role="article"]'));
  const posts = [];
  for (const art of articles) {
    if (!isTopLevelArticle(art)) continue;
    const r = art.getBoundingClientRect();
    if (r.height < 30) continue;
    const text = cleanText(art.innerText || '');
    if (text.length < 12 || !isMeaningfulPost(text)) continue;
    posts.push({ top: r.top + window.scrollY, text: text.slice(0, 2500) });
  }
  posts.sort((a, b) => a.top - b.top);
  const postTexts = posts.slice(0, Math.max(1, Math.min(maxPosts || 3, 3))).map((x) => x.text);

  const mainText = cleanText(main.innerText || '').slice(0, 5000);
  let introText = mainText;
  if (postTexts.length) {
    const firstChunk = postTexts[0].slice(0, 120);
    const idx = firstChunk ? mainText.indexOf(firstChunk) : -1;
    if (idx > 32) introText = mainText.slice(0, idx);
  }
  introText = cleanText(introText).slice(0, 1800);

  return {
    introText,
    postTexts,
  };
}"""


def normalize_person_language_filter(raw: Any, *, allow_all: bool = True) -> str:
    val = str(raw or "all").strip().lower()
    if val == "foreign":
        val = "non_ru"
    if val in LANGUAGE_FILTERS:
        return val
    return "all" if allow_all else LANG_SEGMENT_UNKNOWN


def normalize_person_language_segment(raw: Any) -> str:
    val = str(raw or LANG_SEGMENT_UNKNOWN).strip().lower()
    if val in LANGUAGE_SEGMENTS:
        return val
    return LANG_SEGMENT_UNKNOWN


def language_filter_label(raw: Any) -> str:
    return LANGUAGE_FILTER_LABELS.get(
        normalize_person_language_filter(raw),
        LANGUAGE_FILTER_LABELS["all"],
    )


def language_segment_label(raw: Any) -> str:
    return LANGUAGE_SEGMENT_LABELS.get(
        normalize_person_language_segment(raw),
        LANGUAGE_SEGMENT_LABELS[LANG_SEGMENT_UNKNOWN],
    )


def language_segment_badge(raw: Any) -> str:
    return LANGUAGE_SEGMENT_BADGES.get(
        normalize_person_language_segment(raw),
        LANGUAGE_SEGMENT_BADGES[LANG_SEGMENT_UNKNOWN],
    )


def person_matches_language_filter(segment: Any, language_filter: Any) -> bool:
    seg = normalize_person_language_segment(segment)
    flt = normalize_person_language_filter(language_filter)
    if flt == "all":
        return True
    if flt == "ru":
        return seg == LANG_SEGMENT_RU
    if flt == "en":
        return seg == LANG_SEGMENT_EN
    if flt == "non_ru":
        return seg in (LANG_SEGMENT_EN, LANG_SEGMENT_FOREIGN)
    if flt == "unknown":
        return seg == LANG_SEGMENT_UNKNOWN
    return True


def apply_person_language_filter(query, language_filter: Any):
    flt = normalize_person_language_filter(language_filter)
    if flt == "all":
        return query
    if flt == "ru":
        return query.filter(Person.language_segment == LANG_SEGMENT_RU)
    if flt == "en":
        return query.filter(Person.language_segment == LANG_SEGMENT_EN)
    if flt == "non_ru":
        return query.filter(Person.language_segment.in_([LANG_SEGMENT_EN, LANG_SEGMENT_FOREIGN]))
    if flt == "unknown":
        return query.filter(
            or_(
                Person.language_segment.is_(None),
                Person.language_segment == "",
                Person.language_segment == LANG_SEGMENT_UNKNOWN,
            )
        )
    return query


def _clean_text(raw: str | None, *, limit: int = 4000) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _URL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text[:limit].strip()


def _word_tokens(text: str) -> list[str]:
    cleaned = _clean_text(text, limit=8000)
    if not cleaned:
        return []
    return [m.group(0).lower() for m in _WORD_RE.finditer(cleaned)]


def _count_chars(text: str, rx: re.Pattern[str]) -> int:
    return len(rx.findall(text or ""))


def _name_signal(display_name: str) -> dict[str, Any]:
    name = _clean_text(display_name, limit=255)
    if not name:
        return {"segment": LANG_SEGMENT_UNKNOWN, "score": 0, "reason": ""}
    low = name.lower()
    cyr = _count_chars(name, _CYR_RE)
    ru_cyr = _count_chars(name, _RU_STRICT_RE)
    ua_cyr = _count_chars(name, _UA_RE)
    lat = _count_chars(name, _LAT_RE)
    lat_ext = _count_chars(name, _LAT_EXT_RE)
    viet = _count_chars(name, _VIET_RE)
    tokens = _word_tokens(name)
    if cyr >= 2:
        if ua_cyr >= 2 and ru_cyr <= max(1, ua_cyr):
            return {
                "segment": LANG_SEGMENT_FOREIGN,
                "score": 68,
                "reason": "имя на кириллице с нерусскими буквами",
            }
        if any(low.endswith(sfx) for sfx in _RUS_PATRONYMIC_SUFFIXES):
            return {
                "segment": LANG_SEGMENT_RU,
                "score": 92,
                "reason": "имя содержит русское отчество",
            }
        return {
            "segment": LANG_SEGMENT_RU,
            "score": min(88, 58 + ru_cyr * 2),
            "reason": "имя написано кириллицей",
        }
    if viet >= 1 or lat_ext >= 2:
        return {
            "segment": LANG_SEGMENT_FOREIGN,
            "score": 82,
            "reason": "имя содержит неанглийскую латиницу",
        }
    if lat >= 5 and any(tok in _EN_NAME_HINTS for tok in tokens):
        return {
            "segment": LANG_SEGMENT_EN,
            "score": 42,
            "reason": "в названии есть типичные английские слова",
        }
    return {"segment": LANG_SEGMENT_UNKNOWN, "score": 0, "reason": ""}


def _text_signal(text: str) -> dict[str, Any]:
    cleaned = _clean_text(text, limit=6000)
    if not cleaned:
        return {"segment": LANG_SEGMENT_UNKNOWN, "score": 0, "reason": ""}
    low = cleaned.lower()
    tokens = _word_tokens(cleaned)
    if not tokens:
        return {"segment": LANG_SEGMENT_UNKNOWN, "score": 0, "reason": ""}
    cyr = _count_chars(cleaned, _CYR_RE)
    ru_cyr = _count_chars(cleaned, _RU_STRICT_RE)
    ua_cyr = _count_chars(cleaned, _UA_RE)
    lat = _count_chars(cleaned, _LAT_RE)
    lat_ext = _count_chars(cleaned, _LAT_EXT_RE)
    viet = _count_chars(cleaned, _VIET_RE)
    ru_hits = sum(1 for t in tokens if t in _RU_STOP)
    en_hits = sum(1 for t in tokens if t in _EN_STOP)
    non_en_hits = sum(1 for t in tokens if t in _NON_EN_HINTS)
    if cyr >= 12:
        if ua_cyr >= 2 and ru_hits <= 1 and ru_cyr <= ua_cyr * 2:
            return {
                "segment": LANG_SEGMENT_FOREIGN,
                "score": min(90, 62 + ua_cyr * 8 + min(non_en_hits, 2) * 5),
                "reason": "текст выглядит как нерусский кириллический язык",
            }
        ru_score = 54 + min(36, ru_hits * 8 + ru_cyr // 3)
        if "ё" in low:
            ru_score += 6
        return {
            "segment": LANG_SEGMENT_RU,
            "score": min(96, ru_score),
            "reason": "текст содержит русскую кириллицу",
        }
    if lat >= 12:
        if viet >= 1 or lat_ext >= 6 or non_en_hits >= 2:
            if en_hits <= 1:
                return {
                    "segment": LANG_SEGMENT_FOREIGN,
                    "score": min(94, 66 + min(18, lat_ext * 2) + min(12, non_en_hits * 5)),
                    "reason": "текст содержит неанглийскую латиницу",
                }
        if en_hits >= 2:
            return {
                "segment": LANG_SEGMENT_EN,
                "score": min(94, 60 + min(30, en_hits * 9)),
                "reason": "текст содержит устойчивые английские слова",
            }
    return {"segment": LANG_SEGMENT_UNKNOWN, "score": 0, "reason": ""}


def classify_person_language(
    *,
    display_name: str = "",
    intro_text: str = "",
    post_texts: list[str] | None = None,
) -> dict[str, Any]:
    posts = [p for p in (post_texts or []) if _clean_text(p)]
    signals: list[tuple[str, dict[str, Any], float]] = [
        ("name", _name_signal(display_name), 1.6),
    ]
    if intro_text:
        signals.append(("intro", _text_signal(intro_text), 3.4))
    for idx, post in enumerate(posts[:3], start=1):
        signals.append((f"post_{idx}", _text_signal(post), 4.0))

    score_map = {
        LANG_SEGMENT_RU: 0.0,
        LANG_SEGMENT_EN: 0.0,
        LANG_SEGMENT_FOREIGN: 0.0,
    }
    reasons: list[str] = []
    components: dict[str, Any] = {}
    support_count = 0
    for key, signal, weight in signals:
        seg = normalize_person_language_segment(signal.get("segment"))
        score = int(signal.get("score") or 0)
        reason = str(signal.get("reason") or "").strip()
        components[key] = {
            "segment": seg,
            "score": score,
            "reason": reason,
        }
        if seg == LANG_SEGMENT_UNKNOWN or score <= 0:
            continue
        score_map[seg] += float(score) * weight
        support_count += 1
        if reason:
            reasons.append(reason)

    ordered = sorted(score_map.items(), key=lambda item: item[1], reverse=True)
    top_seg, top_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    if top_score < 85 or top_score < second_score + 28:
        final_seg = LANG_SEGMENT_UNKNOWN
    else:
        final_seg = top_seg

    confidence = 0
    if final_seg != LANG_SEGMENT_UNKNOWN:
        confidence = min(
            99,
            int(round(38 + support_count * 11 + min(42.0, (top_score - second_score) / 3.0))),
        )

    return {
        "segment": final_seg,
        "confidence": confidence,
        "reasons": list(dict.fromkeys(reasons))[:6],
        "components": components,
        "intro_excerpt": _clean_text(intro_text, limit=240),
        "post_excerpts": [_clean_text(p, limit=180) for p in posts[:3]],
    }


def classify_person_language_fast(display_name: str = "") -> dict[str, Any]:
    return classify_person_language(display_name=display_name, intro_text="", post_texts=[])


def should_profile_scan_for_language(
    quick_result: Mapping[str, Any] | None,
    *,
    language_filter: Any = "all",
) -> bool:
    result = dict(quick_result or {})
    segment = normalize_person_language_segment(result.get("segment"))
    confidence = int(result.get("confidence") or 0)
    flt = normalize_person_language_filter(language_filter)
    if segment == LANG_SEGMENT_UNKNOWN or confidence < 78:
        return True
    if flt == "en" and segment != LANG_SEGMENT_EN:
        return True
    return False


def extract_profile_language_context(
    page: Page,
    *,
    max_posts: int = 3,
    timeout_ms: int = 20_000,
) -> dict[str, Any]:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(25_000, timeout_ms))
    except Exception:
        pass
    best_intro = ""
    best_posts: list[str] = []
    for _ in range(4):
        page.wait_for_timeout(900)
        try:
            raw = page.evaluate(
                _EXTRACT_PROFILE_LANGUAGE_CONTEXT_JS,
                {"maxPosts": max(1, min(int(max_posts or 3), 3))},
            )
        except Exception:
            raw = {}
        if isinstance(raw, Mapping):
            intro = _clean_text(str(raw.get("introText") or ""), limit=1800)
            posts = [
                _clean_text(str(p or ""), limit=2200)
                for p in (raw.get("postTexts") or [])
                if _clean_text(str(p or ""), limit=2200)
            ]
            if len(intro) > len(best_intro):
                best_intro = intro
            if sum(len(p) for p in posts) > sum(len(p) for p in best_posts):
                best_posts = posts[:3]
            if best_intro and len(best_posts) >= 3:
                break
        try:
            page.mouse.wheel(0, 600)
        except Exception:
            pass
    return {"intro_text": best_intro, "post_texts": best_posts[:3]}


def apply_language_result_to_person(
    person: Person,
    result: Mapping[str, Any] | None,
) -> bool:
    payload = dict(result or {})
    segment = normalize_person_language_segment(payload.get("segment"))
    confidence = int(payload.get("confidence") or 0)
    checked_at = datetime.now(timezone.utc)
    changed = False
    if (person.language_segment or "") != segment:
        person.language_segment = segment
        changed = True
    if int(person.language_confidence or 0) != confidence:
        person.language_confidence = confidence
        changed = True
    if person.language_checked_at != checked_at:
        person.language_checked_at = checked_at
        changed = True

    meta = dict(person.raw_meta) if isinstance(person.raw_meta, Mapping) else {}
    prev = meta.get("language_profile")
    new_meta = {
        "segment": segment,
        "confidence": confidence,
        "reasons": list(payload.get("reasons") or [])[:6],
        "components": payload.get("components") or {},
        "intro_excerpt": str(payload.get("intro_excerpt") or "")[:240],
        "post_excerpts": [str(x or "")[:180] for x in (payload.get("post_excerpts") or [])[:3]],
        "checked_at": checked_at.isoformat(),
    }
    if prev != new_meta:
        meta["language_profile"] = new_meta
        person.raw_meta = meta
        changed = True

    if changed:
        person.updated_at = checked_at
    return changed


__all__ = [
    "LANGUAGE_FILTER_LABELS",
    "LANGUAGE_SEGMENT_LABELS",
    "apply_language_result_to_person",
    "apply_person_language_filter",
    "classify_person_language",
    "classify_person_language_fast",
    "extract_profile_language_context",
    "language_filter_label",
    "language_segment_badge",
    "language_segment_label",
    "normalize_person_language_filter",
    "normalize_person_language_segment",
    "person_matches_language_filter",
    "should_profile_scan_for_language",
]
