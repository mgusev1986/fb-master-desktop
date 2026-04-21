"""Workspace session helpers + launcher view builders."""

from backend.core.workspace.launcher_view import (
    LauncherView,
    WorkspaceCardView,
    build_launcher_view,
    build_switcher_view,
)
from backend.core.workspace.session import (
    WORKSPACE_SESSION_KEY,
    clear_current_workspace,
    get_current_workspace_id,
    set_current_workspace_id,
)

__all__ = [
    "LauncherView",
    "WORKSPACE_SESSION_KEY",
    "WorkspaceCardView",
    "build_launcher_view",
    "build_switcher_view",
    "clear_current_workspace",
    "get_current_workspace_id",
    "set_current_workspace_id",
]
