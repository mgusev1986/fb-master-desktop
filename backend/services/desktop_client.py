"""Определение клиента FB Master Desktop (Electron)."""

from __future__ import annotations

# Должен совпадать с суффиксом User-Agent в desktop/fb-master-desktop/main.js
FB_MASTER_DESKTOP_UA_TOKEN = "FBMasterDesktop"


def user_agent_is_fb_master_desktop(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return FB_MASTER_DESKTOP_UA_TOKEN in user_agent
