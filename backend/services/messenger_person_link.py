"""Автопривязка диалогов Messenger к контактам в «Базе контактов» при синхронизации."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import func

from backend.services.crm_stages_registry import default_crm_stage_slug_for_new_person
from backend.services.fb_url_normalize import normalize_facebook_profile_url

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_CYR_TO_LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "є": "ye",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "і": "i",
    "ї": "yi",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
_NAME_TOKEN_SKIP = {"facebook", "profile", "php", "messages", "www", "com", "id", "people"}

# Заглушки вместо реального имени в UI Messenger — не пишем в Person.display_name.
_MESSENGER_DISPLAY_NAME_PLACEHOLDERS = frozenset(
    {
        "facebook user",
        "utilisateur facebook",
        "utilisateur de facebook",
        "usuario de facebook",
        "диалог",
    }
)


def normalize_messenger_ui_display_name(raw: str | None) -> str | None:
    """Имя из заголовка чата в webview: обрезка, длина, отсев мусора и плейсхолдеров."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if len(s) < 2 or len(s) > 255:
        return None
    if s.isdigit():
        return None
    low = s.lower()
    if low in _MESSENGER_DISPLAY_NAME_PLACEHOLDERS:
        return None
    if re.match(r"^benutzer[\s-]*facebook$", low):
        return None
    if re.search(r"пользователь\s+facebook", low) or re.search(r"пользователь\s+фейсбук", low):
        return None
    if re.search(r"facebook[-\s]?nutzer", low):
        return None
    return s[:255]


def apply_messenger_peer_display_name_to_person(
    db: Session,
    *,
    person: Person | None,
    conversation,
    messenger_display_name: str | None,
) -> bool:
    """
    Обновить Person.display_name и Conversation.peer_name из актуального имени в Messenger.
    Возвращает True, если в БД что-то изменилось (нужен commit).
    """
    from backend.models import Person as PersonModel

    normalized = normalize_messenger_ui_display_name(messenger_display_name)
    if not normalized or person is None:
        return False
    if not isinstance(person, PersonModel):
        return False

    any_change = False
    if conversation is not None:
        cur_peer = (getattr(conversation, "peer_name", None) or "").strip()
        if cur_peer != normalized:
            conversation.peer_name = normalized
            any_change = True
    display_changed = (person.display_name or "").strip() != normalized
    if display_changed:
        person.display_name = normalized
        any_change = True
    if any_change:
        db.flush()
    return any_change


def extract_linking_ids_from_profile_url(url: str | None) -> set[str]:
    """
    Числовые идентификаторы Facebook из URL профиля / переписки для сопоставления с thread_id.
    Учитывает profile.php?id=…, числовой путь facebook.com/1000…, messages/t/….
    """
    if not url:
        return set()
    s = url.strip()
    out: set[str] = set()
    for m in re.finditer(r"(?:[?&])id=(\d+)", s, re.I):
        uid = m.group(1)
        if len(uid) >= 6:
            out.add(uid)
    for m in re.finditer(r"facebook\.com/(\d{8,})(?:/|\?|#|$)", s, re.I):
        out.add(m.group(1))
    for m in re.finditer(r"/messages/t/(\d{8,})(?:/|\?|#|$)", s, re.I):
        out.add(m.group(1))
    return out


def extract_thread_id_from_messenger_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/messages/t/(\d+)", url)
    if not m:
        return None
    tid = (m.group(1) or "").strip()
    return tid if tid.isdigit() else None


def parse_messenger_thread_location(raw_url: str) -> tuple[str | None, str | None]:
    """
    Из URL вкладки Messenger (webview): нормализованный peer_url для conversations.peer_url
    и числовой thread_id, если сегмент целиком из цифр.
    Поддерживаются facebook.com, messenger.com, путь /messages/… и /t/…, в т.ч. e2ee.
    """
    s = (raw_url or "").strip()
    if not s:
        return None, None
    m = re.search(
        r"(?:facebook\.com|messenger\.com|business\.facebook\.com)/"
        r"(?:messages/(?:e2ee/)?|(?:e2ee/)?)t/([^/?#]+)",
        s,
        re.I,
    )
    if not m:
        return None, None
    seg = (m.group(1) or "").strip()
    if not seg:
        return None, None
    peer = f"https://www.facebook.com/messages/t/{seg}"[:512]
    tid = seg if seg.isdigit() else None
    return peer, tid


def _latinize_nameish(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    out: list[str] = []
    for ch in s:
        if unicodedata.category(ch).startswith("M"):
            continue
        if ch in _CYR_TO_LAT:
            out.append(_CYR_TO_LAT[ch])
            continue
        if ch.isascii():
            out.append(ch)
            continue
        out.append(" ")
    return "".join(out)


def _nameish_tokens(raw: str | None) -> list[str]:
    s = _latinize_nameish(raw)
    if not s:
        return []
    parts = re.split(r"[^a-z0-9]+", s)
    return [p for p in parts if len(p) >= 2 and p not in _NAME_TOKEN_SKIP]


def _profile_url_name_tokens(url: str | None) -> list[str]:
    if not url:
        return []
    try:
        parsed = urlparse(url)
    except Exception:
        return []
    path = (parsed.path or "").strip("/")
    if not path or "profile.php" in path:
        return []
    first = path.split("/", 1)[0]
    return _nameish_tokens(first)


def _bidirectional_token_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0

    def _best_avg(src: list[str], dst: list[str]) -> float:
        return sum(
            max(SequenceMatcher(None, a, b).ratio() for b in dst)
            for a in src
        ) / max(1, len(src))

    return (_best_avg(left, right) + _best_avg(right, left)) / 2.0


def _match_person_by_nameish(db: Session, peer_name: str | None) -> int | None:
    from backend.models import Person

    peer_tokens = _nameish_tokens(peer_name)
    if len(peer_tokens) < 2:
        return None

    scored: list[tuple[float, int]] = []
    q = (
        db.query(Person.id, Person.display_name, Person.canonical_url)
        .execution_options(stream_results=True)
    )
    for pid, display_name, canonical_url in q.yield_per(500):
        best = 0.0
        disp_tokens = _nameish_tokens(display_name or "")
        if disp_tokens:
            best = max(best, _bidirectional_token_similarity(peer_tokens, disp_tokens))
        url_tokens = _profile_url_name_tokens(canonical_url or "")
        if url_tokens:
            best = max(best, _bidirectional_token_similarity(peer_tokens, url_tokens))
        if best >= 0.78:
            scored.append((best, int(pid)))

    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    top_score, top_pid = scored[0]
    if len(scored) == 1:
        return top_pid
    second_score = scored[1][0]
    if top_score >= 0.85 or top_score >= second_score + 0.06:
        return top_pid
    return None


def build_messenger_person_id_index(
    db: Session, *, organization_id: int | None = None
) -> dict[str, int]:
    """
    Карта «числовой id Facebook → person.id» по всем canonical_url в CRM.
    Если id встречается у нескольких записей, берётся первый по порядку в выборке.
    organization_id — ограничить людей организацией (для мультитенантности); None = все.
    """
    from backend.models import Person

    idx: dict[str, int] = {}
    q = db.query(Person.id, Person.canonical_url).execution_options(stream_results=True)
    if organization_id is not None:
        q = q.filter(Person.organization_id == organization_id)
    for pid, url in q.yield_per(500):
        for nid in extract_linking_ids_from_profile_url(url or ""):
            if nid not in idx:
                idx[nid] = pid
    return idx


def _match_unique_display_name(db: Session, peer_name: str | None) -> int | None:
    from backend.models import Person

    if not peer_name:
        return None
    name = re.sub(r"\s+", " ", peer_name.strip())
    if len(name) < 2:
        return None
    key = name.lower()
    rows = (
        db.query(Person)
        .filter(func.lower(func.trim(Person.display_name)) == key)
        .limit(5)
        .all()
    )
    if len(rows) == 1:
        return rows[0].id
    return None


def infer_person_canonical_url_from_conversation(
    thread_id: str | None,
    peer_url: str | None,
) -> str | None:
    def _from_segment(seg: str) -> str | None:
        seg = (seg or "").strip()
        if not seg:
            return None
        if seg.isdigit():
            return normalize_facebook_profile_url(f"https://www.facebook.com/{seg}")
        if re.match(r"^[A-Za-z0-9._+-]+\Z", seg):
            nu = normalize_facebook_profile_url(f"https://www.facebook.com/{seg}")
            if nu:
                return nu
        return None

    tid = (thread_id or "").strip()
    if tid.isdigit():
        u = _from_segment(tid)
        if u:
            return u

    pu = (peer_url or "").strip()
    m = re.search(
        r"(?:facebook\.com|messenger\.com)/(?:messages/|)(?:e2ee/)?t/([^/?#]+)",
        pu,
        re.I,
    )
    if m:
        seg = (m.group(1) or "").strip()
        u = _from_segment(seg)
        if u:
            return u
        if seg:
            return f"https://www.facebook.com/messages/t/{seg}"[:512]

    return normalize_facebook_profile_url(pu)


def resolve_messenger_conversation_person_id(
    db: Session,
    thread_id: str | None,
    peer_name: str | None,
    *,
    id_index: dict[str, int] | None = None,
) -> int | None:
    """
    Подобрать person_id для чата messages/t/<thread_id>.

    1) Точное совпадение thread_id с числом из URL профиля (индекс по всей базе).
    2) Подстрока thread_id в canonical_url (как раньше).
    3) Ровно один контакт с таким display_name (без учёта регистра и краевых пробелов).
    4) Осторожное нечёткое сопоставление по имени диалога и username в canonical_url.
    """
    if not thread_id or not str(thread_id).strip().isdigit():
        pid = _match_unique_display_name(db, peer_name)
        if pid is not None:
            return pid
        return _match_person_by_nameish(db, peer_name)
    tid = str(thread_id).strip()

    if id_index is not None and tid in id_index:
        return id_index[tid]

    from backend.services.messenger_scrape import guess_person_id_from_thread

    pid = guess_person_id_from_thread(db, tid)
    if pid is not None:
        return pid

    pid = _match_unique_display_name(db, peer_name)
    if pid is not None:
        return pid

    return _match_person_by_nameish(db, peer_name)


def ensure_conversation_person(
    db: Session,
    conversation,
    *,
    id_index: dict[str, int] | None = None,
):
    """
    Обеспечить привязку Messenger-диалога к Person.

    Порядок:
    1) Если у диалога уже есть валидный person_id — вернуть его.
    2) Попробовать найти существующего человека по thread_id / имени / username.
    3) Если не найдено — создать нового человека из текущего чата.

    Возвращает (person, created_new_person, linked_existing_person).
    """
    from backend.models import Person

    if conversation is None:
        return None, False, False

    conv_name = (getattr(conversation, "peer_name", None) or "").strip()[:255] or None

    current_pid = getattr(conversation, "person_id", None)
    if current_pid:
        person = db.get(Person, int(current_pid))
        if person is not None:
            if conv_name and not (person.display_name or "").strip():
                person.display_name = conv_name
                db.flush()
            return person, False, False
        conversation.person_id = None
        db.flush()

    thread_id = extract_thread_id_from_messenger_url(getattr(conversation, "peer_url", None))
    pid = resolve_messenger_conversation_person_id(
        db,
        thread_id,
        getattr(conversation, "peer_name", None),
        id_index=id_index,
    )
    if pid:
        person = db.get(Person, int(pid))
        if person is not None:
            if conv_name and not (person.display_name or "").strip():
                person.display_name = conv_name
            conversation.person_id = person.id
            db.flush()
            return person, False, True

    canonical = infer_person_canonical_url_from_conversation(
        thread_id,
        getattr(conversation, "peer_url", None),
    )
    if not canonical:
        return None, False, False

    person = db.query(Person).filter(Person.canonical_url == canonical).first()
    if person is not None:
        if conv_name and not (person.display_name or "").strip():
            person.display_name = conv_name
        conversation.person_id = person.id
        db.flush()
        return person, False, True

    from backend.models import FBAccount

    acc = db.get(FBAccount, int(getattr(conversation, "fb_account_id", 0) or 0))
    if acc is None:
        return None, False, False

    person = Person(
        organization_id=acc.organization_id,
        canonical_url=canonical,
        display_name=conv_name,
        crm_stage=default_crm_stage_slug_for_new_person(db),
    )
    db.add(person)
    db.flush()
    conversation.person_id = person.id
    db.flush()
    return person, True, False


def apply_messenger_crm_stage_to_conversation(
    db: Session,
    org_id: int,
    conversation,
    stage_slug: str,
    *,
    sync_display_name_from_messenger: bool = False,
) -> tuple[dict, int]:
    """
    Общая логика POST …/crm-stage: проверки, ensure_conversation_person, запись стадии и CRMActivity.
    Возвращает (тело JSON, HTTP-код).
    """
    from datetime import datetime, timezone
    from urllib.parse import urlencode

    from backend.models import CRMActivity, FBAccount
    from backend.services.crm_stages_registry import get_ordered_stages

    slug = (stage_slug or "").strip()
    allowed = {s.slug for s in get_ordered_stages(db)}
    if slug not in allowed:
        return {"ok": False, "error": "invalid_stage"}, 422

    acc_ok = (
        db.query(FBAccount)
        .filter(
            FBAccount.id == conversation.fb_account_id,
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .first()
    )
    if not acc_ok:
        return {"ok": False, "error": "not_found"}, 404

    person, created_person, linked_existing = ensure_conversation_person(db, conversation)
    if person is None:
        return {
            "ok": False,
            "error": "person_link_failed",
            "detail": "Не удалось сопоставить чат с контактом и создать новую запись в «Базе контактов».",
        }, 422

    if sync_display_name_from_messenger:
        apply_messenger_peer_display_name_to_person(
            db,
            person=person,
            conversation=conversation,
            messenger_display_name=getattr(conversation, "peer_name", None),
        )

    now = datetime.now(timezone.utc)
    person.crm_stage = slug
    person.crm_stage_changed_at = now
    db.add(
        CRMActivity(
            person_id=person.id,
            fb_account_id=conversation.fb_account_id,
            activity_type="messenger_crm_stage_set",
            detail=f"Стадия из мессенджера: {slug}",
        )
    )
    db.commit()

    funnel_query = (
        (person.display_name or person.canonical_url or getattr(conversation, "peer_name", None) or "")
        .strip()
    )
    return (
        {
            "ok": True,
            "conversation_id": conversation.id,
            "person_id": person.id,
            "person_name": person.display_name or "",
            "person_canonical_url": person.canonical_url or "",
            "crm_stage": slug,
            "created_person": created_person,
            "linked_existing_person": linked_existing,
            "funnel_url": (
                f"/crm/funnel?q={urlencode({'q': funnel_query})[2:]}"
                if funnel_query
                else "/crm/funnel"
            ),
        },
        200,
    )


def relink_messenger_conversations_to_people(db: Session) -> int:
    """
    Пересобрать индекс числовых ID из URL профилей и привязать диалоги без person_id.
    Вызывать после импорта/парсера в «Базу контактов», чтобы чаты совпали с новыми записями.
    """
    db.flush()
    idx = build_messenger_person_id_index(db)
    return relink_conversations_missing_person(db, idx)


def relink_conversations_missing_person(
    db: Session,
    id_index: dict[str, int],
) -> int:
    """
    Проставить person_id диалогам, где он ещё NULL: по thread_id из peer_url.
    Возвращает число обновлённых строк.
    """
    from backend.models import Conversation

    updated = 0
    rows = db.query(Conversation).filter(Conversation.person_id.is_(None)).all()
    for conv in rows:
        tid = extract_thread_id_from_messenger_url(conv.peer_url or "")
        if not tid:
            continue
        pid = resolve_messenger_conversation_person_id(
            db, tid, conv.peer_name, id_index=id_index
        )
        if pid:
            conv.person_id = pid
            updated += 1
    return updated
