"""License Watcher — фоновая проверка лицензии у клиента (v3.0+).

Запускается ТОЛЬКО на десктопе клиента (где приватного ключа нет).
На VPS — выключен через `has_private_key()` self-detect.

Алгоритм цикла (каждые 30 сек):

1) Multi-endpoint VPS check:
   POST к 3 URL (socmaster.pro, api.socmaster.pro, license.socmaster.pro)
   ├─ хотя бы один ответил {ok: true}  → state=valid, save fresh state+probes
   ├─ хотя бы один ответил {ok: false} → state=invalid (kick out)
   └─ ВСЕ недоступны → шаг 2

2) VPS-downtime grace (для нашего downtime):
   Если последний успех VPS был < 30 мин назад → silent retry, exit.
   (Покрывает короткий downtime/деплой/DDoS на нашей стороне.)

3) Probe check (6 эталонов из подписанного списка):
   ├─ 4+ из 6 живы (≥67%) → blocked → kick out (мгновенно)
   ├─ 0 из 6 живы          → offline → state не меняем
   └─ 1-3 из 6 живы        → suspect_count++
       └─ если suspect_count >= 3 (90 сек подряд) → blocked → kick out

При каждом успехе шага 1 → suspect_count = 0, last_vps_success_at = now.

Защита подписи Ed25519:
- license_state в БД → подпись Ed25519 → клиент не может подменить.
- Список probes в БД → подпись Ed25519 → клиент не может подменить.
- Ed25519 публичный ключ зашит в Electron-bundle (license-public.pem).

Защита от отката системных часов:
- last_seen_monotonic в JSON-файле — если now() < last_seen-5min → tampered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.services.daily_probes import (
    license_state_signing_payload,
    probes_signing_payload,
)
from backend.services.license_signer import has_private_key, has_public_key, verify_b64

logger = logging.getLogger(__name__)


# ── Настройки (через env с дефолтами) ─────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# 3 endpoint'а лицензионного сервера. CSV в env, дефолт — три subdomain.
LICENSE_ENDPOINTS_CSV = (
    os.environ.get("FB_MASTER_LICENSE_ENDPOINTS")
    or "https://socmaster.pro/api/public/desktop-license/check,"
       "https://api.socmaster.pro/api/public/desktop-license/check,"
       "https://license.socmaster.pro/api/public/desktop-license/check"
).strip()
LICENSE_ENDPOINTS: list[str] = [u.strip() for u in LICENSE_ENDPOINTS_CSV.split(",") if u.strip()]

# Hard-coded fallback probes, используем когда нет подписанного списка
# (например, при первом запуске или истёкшем TTL подписи).
HARDCODED_FALLBACK_PROBES: tuple[str, ...] = (
    "https://www.facebook.com/",
    "https://www.google.com/",
)

WATCHER_INTERVAL_SEC = _env_int("FB_MASTER_LICENSE_WATCHER_INTERVAL_SEC", 30)
HTTP_TIMEOUT_SEC = float(_env_int("FB_MASTER_LICENSE_HTTP_TIMEOUT_SEC", 5))
VPS_DOWNTIME_GRACE_SEC = _env_int("FB_MASTER_LICENSE_VPS_GRACE_SEC", 30 * 60)  # 30 мин
SUSPECT_THRESHOLD = _env_int("FB_MASTER_LICENSE_SUSPECT_THRESHOLD", 3)  # 3 цикла = 90 сек
QUORUM_RATIO = 2 / 3  # 4 из 6 для blocked


# ── In-memory state (для middleware) ──────────────────────────────

_STATE_LOCK = asyncio.Lock()
_STATE: dict[str, Any] = {
    # Один из: "init" | "valid" | "invalid" | "blocked" | "never_activated"
    # "init" — стартовый, до первой проверки. middleware пропускает только
    # при "valid". "never_activated" → редирект на /auth/unlock.
    "status": "init",
    "reason": None,
    "last_vps_success_at": None,
    "expires_at": None,
    "license_signature": None,
    "verified_at": None,
    "suspect_count": 0,
    # Последний валидный probes-блок (подписанный).
    "probes": list(HARDCODED_FALLBACK_PROBES),
    "probes_valid_until": None,
    "probes_signature": None,
}

# Триггер для немедленной проверки (вызывается из online-event hook).
_RECHECK_NOW: asyncio.Event | None = None
_WATCHER_TASK: asyncio.Task | None = None


# ── Persistence (HMAC-подписан Ed25519 на VPS) ────────────────────

def _state_file_path() -> Path:
    """Файл с last-known state. На macOS — Application Support."""
    env = os.environ.get("FB_MASTER_LICENSE_STATE_PATH", "").strip()
    if env:
        return Path(env)
    home = Path.home()
    if os.name == "posix":
        return home / "Library" / "Application Support" / "fb-master-desktop" / "fb-master-local-data" / "license_state.json"
    # Windows fallback:
    return home / "AppData" / "Roaming" / "fb-master-desktop" / "license_state.json"


def _last_seen_path() -> Path:
    """Файл с монотонным last_seen для детекта отката часов."""
    return _state_file_path().parent / "license_last_seen"


def _save_state_to_disk() -> None:
    try:
        path = _state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "status": _STATE["status"],
            "verified_at": _STATE["verified_at"],
            "expires_at": _STATE["expires_at"],
            "license_signature": _STATE["license_signature"],
            "key_hash": _STATE.get("key_hash"),
            "device_fingerprint": _STATE.get("device_fingerprint"),
            "probes": _STATE["probes"],
            "probes_valid_until": _STATE["probes_valid_until"],
            "probes_signature": _STATE["probes_signature"],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception("license_watcher: save state failed")


def _load_state_from_disk() -> None:
    """Загрузка last-known state при старте. Подпись Ed25519 верифицируется."""
    try:
        path = _state_file_path()
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        sig = data.get("license_signature")
        kh = data.get("key_hash")
        fp = data.get("device_fingerprint")
        if not sig or not kh or not fp or not data.get("verified_at"):
            return
        verified_at = datetime.fromisoformat(data["verified_at"].replace("Z", "+00:00"))
        expires_at = (
            datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if data.get("expires_at") else None
        )
        payload = license_state_signing_payload(
            state=data["status"],
            key_hash=kh,
            device_fingerprint=fp,
            verified_at=verified_at,
            expires_at=expires_at,
        )
        if not verify_b64(payload, sig):
            logger.warning("license_watcher: stored state signature INVALID — discarding")
            return
        _STATE.update({
            "status": data["status"],
            "verified_at": data["verified_at"],
            "expires_at": data["expires_at"],
            "license_signature": sig,
            "key_hash": kh,
            "device_fingerprint": fp,
        })
        probes = data.get("probes")
        pvu = data.get("probes_valid_until")
        psig = data.get("probes_signature")
        if probes and pvu and psig:
            valid_until = datetime.fromisoformat(pvu.replace("Z", "+00:00"))
            payload2 = probes_signing_payload(probes, valid_until)
            if verify_b64(payload2, psig) and datetime.now(timezone.utc) < valid_until:
                _STATE["probes"] = probes
                _STATE["probes_valid_until"] = pvu
                _STATE["probes_signature"] = psig
        # Tamper-check на часы.
        last_seen_path = _last_seen_path()
        if last_seen_path.exists():
            try:
                last_seen_iso = last_seen_path.read_text(encoding="utf-8").strip()
                last_seen = datetime.fromisoformat(last_seen_iso.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < last_seen - timedelta(minutes=5):
                    logger.warning("license_watcher: clock rollback detected — invalidating state")
                    _STATE["status"] = "invalid"
                    _STATE["reason"] = "clock_rollback"
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.exception("license_watcher: load state failed")


def _bump_last_seen() -> None:
    try:
        path = _last_seen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ── HTTP-проверки ──────────────────────────────────────────────────

async def _http_post_first_ok(client: httpx.AsyncClient, urls: list[str], json_body: dict) -> dict | None:
    """Пробует POST к каждому URL по очереди, возвращает первый успешный JSON."""
    for url in urls:
        try:
            resp = await client.post(url, json=json_body, timeout=HTTP_TIMEOUT_SEC)
            if resp.status_code < 500:
                return resp.json()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
            continue
        except Exception:  # noqa: BLE001
            logger.exception("license_watcher: unexpected error on %s", url)
            continue
    return None


async def _probe_one(client: httpx.AsyncClient, url: str) -> bool:
    try:
        resp = await client.head(url, timeout=HTTP_TIMEOUT_SEC, follow_redirects=False)
        return resp.status_code < 500
    except Exception:  # noqa: BLE001
        return False


async def _check_probes(client: httpx.AsyncClient) -> tuple[int, int]:
    """Параллельно дёргает все probes из текущего state. Возвращает (alive, total)."""
    probes = _STATE.get("probes") or list(HARDCODED_FALLBACK_PROBES)
    if not probes:
        probes = list(HARDCODED_FALLBACK_PROBES)
    results = await asyncio.gather(*[_probe_one(client, u) for u in probes], return_exceptions=True)
    alive = sum(1 for r in results if r is True)
    return alive, len(probes)


# ── Основной цикл watcher ─────────────────────────────────────────

def _credentials_for_check() -> tuple[str, str] | None:
    """Достаёт key_hash + device_fingerprint из локальной БД."""
    try:
        from backend.database import SessionLocal
        from backend.models import AccessKey

        db = SessionLocal()
        try:
            row = (
                db.query(AccessKey)
                .filter(AccessKey.revoked_at.is_(None))
                .filter(AccessKey.device_fingerprint.isnot(None))
                .order_by(AccessKey.id.desc())
                .first()
            )
            if row and row.key_hash and row.device_fingerprint:
                return row.key_hash, row.device_fingerprint
            return None
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("license_watcher: credentials fetch failed")
        return None


async def _do_one_cycle() -> None:
    creds = _credentials_for_check()
    if not creds:
        async with _STATE_LOCK:
            _STATE["status"] = "never_activated"
        return

    kh, fp = creds
    body = {"key_hash": kh, "device_fingerprint": fp}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
        # 1) Multi-endpoint VPS check.
        vps_response = await _http_post_first_ok(client, LICENSE_ENDPOINTS, body)

        if vps_response is not None:
            await _apply_vps_response(vps_response, kh, fp)
            return

        # 2) VPS-downtime grace.
        async with _STATE_LOCK:
            last_success = _STATE.get("last_vps_success_at")
        now = datetime.now(timezone.utc)
        if last_success:
            try:
                last_dt = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
                if (now - last_dt).total_seconds() < VPS_DOWNTIME_GRACE_SEC:
                    logger.info("license_watcher: vps unreachable but recent success — grace silent retry")
                    return
            except Exception:  # noqa: BLE001
                pass

        # 3) Probe check (quorum).
        alive, total = await _check_probes(client)
        logger.info("license_watcher: probes alive=%d/%d", alive, total)
        async with _STATE_LOCK:
            if alive == 0:
                _STATE["suspect_count"] = 0
                logger.info("license_watcher: full offline, keeping state=%s", _STATE["status"])
            elif alive >= int(total * QUORUM_RATIO + 0.5):
                _STATE["status"] = "blocked"
                _STATE["reason"] = "vps_blocked_via_hosts"
                _STATE["suspect_count"] = 0
                logger.warning("license_watcher: VPS UNREACHABLE but %d/%d probes alive → BLOCKED", alive, total)
                _save_state_to_disk()
            else:
                _STATE["suspect_count"] = int(_STATE.get("suspect_count") or 0) + 1
                if _STATE["suspect_count"] >= SUSPECT_THRESHOLD:
                    _STATE["status"] = "blocked"
                    _STATE["reason"] = "vps_blocked_after_suspect"
                    _save_state_to_disk()
                    logger.warning(
                        "license_watcher: %d cycles ambiguous → BLOCKED",
                        _STATE["suspect_count"],
                    )
                else:
                    logger.info(
                        "license_watcher: ambiguous (%d/%d alive), suspect=%d/%d",
                        alive, total, _STATE["suspect_count"], SUSPECT_THRESHOLD,
                    )


async def _apply_vps_response(resp: dict, kh: str, fp: str) -> None:
    """Обработка успешного ответа от VPS — verify подписи и обновление state."""
    now = datetime.now(timezone.utc)
    state_str = resp.get("state") or ("valid" if resp.get("ok") else "invalid")
    license_sig = resp.get("license_signature")
    verified_at_iso = resp.get("verified_at")
    expires_at_iso = resp.get("expires_at")
    if not license_sig or not verified_at_iso:
        logger.warning("license_watcher: vps response missing signature/verified_at")
        return
    try:
        verified_at = datetime.fromisoformat(verified_at_iso.replace("Z", "+00:00"))
        expires_at = (
            datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
            if expires_at_iso else None
        )
    except Exception:  # noqa: BLE001
        logger.exception("license_watcher: bad timestamps in vps response")
        return

    payload = license_state_signing_payload(
        state=state_str,
        key_hash=kh,
        device_fingerprint=fp,
        verified_at=verified_at,
        expires_at=expires_at,
    )
    if not verify_b64(payload, license_sig):
        logger.warning("license_watcher: VPS signature INVALID — possible MITM, ignoring response")
        return

    async with _STATE_LOCK:
        _STATE["status"] = state_str if state_str in ("valid", "invalid") else "invalid"
        _STATE["reason"] = resp.get("reason")
        _STATE["verified_at"] = verified_at_iso
        _STATE["expires_at"] = expires_at_iso
        _STATE["license_signature"] = license_sig
        _STATE["key_hash"] = kh
        _STATE["device_fingerprint"] = fp
        _STATE["last_vps_success_at"] = now.isoformat()
        _STATE["suspect_count"] = 0

        probes = resp.get("probes")
        pvu_iso = resp.get("probes_valid_until")
        psig = resp.get("probes_signature")
        if probes and pvu_iso and psig:
            try:
                pvu = datetime.fromisoformat(pvu_iso.replace("Z", "+00:00"))
                payload2 = probes_signing_payload(probes, pvu)
                if verify_b64(payload2, psig):
                    _STATE["probes"] = probes
                    _STATE["probes_valid_until"] = pvu_iso
                    _STATE["probes_signature"] = psig
            except Exception:  # noqa: BLE001
                logger.exception("license_watcher: probes signature parse failed")

        _save_state_to_disk()
        _bump_last_seen()
        logger.info("license_watcher: VPS check ok, state=%s, expires_at=%s", state_str, expires_at_iso)


async def _watcher_loop() -> None:
    global _RECHECK_NOW
    _RECHECK_NOW = asyncio.Event()
    try:
        await _do_one_cycle()
    except Exception:  # noqa: BLE001
        logger.exception("license_watcher: first cycle failed")

    while True:
        try:
            try:
                await asyncio.wait_for(_RECHECK_NOW.wait(), timeout=WATCHER_INTERVAL_SEC)
                _RECHECK_NOW.clear()
                logger.info("license_watcher: triggered by online-event")
            except asyncio.TimeoutError:
                pass
            await _do_one_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("license_watcher: cycle failed, retrying")


# ── Public API для middleware и lifespan ──────────────────────────

def is_active_on_this_machine() -> bool:
    """Watcher активен только если приватного ключа НЕТ (= это клиент, не VPS)."""
    if os.environ.get("FB_MASTER_LICENSE_WATCHER", "").strip().lower() == "off":
        return False
    if has_private_key():
        return False
    if not has_public_key():
        return False
    return True


def start_watcher() -> None:
    """Запускает фоновую задачу. Безопасно вызвать несколько раз — second call no-op."""
    global _WATCHER_TASK
    if not is_active_on_this_machine():
        logger.info("license_watcher: skipped (vps or no public key)")
        return
    if _WATCHER_TASK is not None and not _WATCHER_TASK.done():
        return
    _load_state_from_disk()
    loop = asyncio.get_event_loop()
    _WATCHER_TASK = loop.create_task(_watcher_loop())
    logger.info("license_watcher: started, interval=%ss", WATCHER_INTERVAL_SEC)


def stop_watcher() -> None:
    global _WATCHER_TASK
    if _WATCHER_TASK is not None:
        _WATCHER_TASK.cancel()
        _WATCHER_TASK = None


def trigger_recheck_now() -> None:
    """Online-event hook — запросить перепроверку немедленно."""
    if _RECHECK_NOW is not None:
        _RECHECK_NOW.set()


async def force_check_now() -> dict[str, Any]:
    """Синхронно (await) запустить один cycle проверки и вернуть текущий state.

    Используется в handler /auth/access-key/activate сразу после успешной
    активации — чтобы watcher мгновенно обновил state на 'valid' без
    ожидания 30-секундного фонового цикла.
    """
    if not is_active_on_this_machine():
        return current_state()
    try:
        await _do_one_cycle()
    except Exception:  # noqa: BLE001
        logger.exception("force_check_now failed")
    return current_state()


def current_state() -> dict[str, Any]:
    """Снимок текущего state для middleware. Возвращает копию."""
    return dict(_STATE)


def is_valid() -> bool:
    """Helper для middleware: можно ли пропустить запрос."""
    if not is_active_on_this_machine():
        return True  # на VPS не проверяем self
    s = _STATE.get("status")
    return s == "valid"


def block_reason() -> str:
    """Helper для middleware: причина блокировки (для UX)."""
    s = _STATE.get("status")
    if s == "valid":
        return ""
    if s == "init":
        return "checking"
    if s == "never_activated":
        return "not_activated"
    return _STATE.get("reason") or s or "unknown"


__all__ = [
    "block_reason",
    "current_state",
    "is_active_on_this_machine",
    "is_valid",
    "start_watcher",
    "stop_watcher",
    "trigger_recheck_now",
]
