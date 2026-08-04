# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse

from qwenpaw.constant import WORKING_DIR
from qwenpaw.config import load_config
from qwenpaw.security.tool_guard.guardians.file_guardian import (
    FilePathToolGuardian,
    _normalize_path,
)

router = APIRouter(prefix="/files", tags=["files"])

_ALLOWED_ROOT: Path = WORKING_DIR.resolve()

# Reuse the FileGuard sensitive path detection for the preview endpoint.
_file_guardian = FilePathToolGuardian()


def _is_preview_outside_workspace_allowed() -> bool:
    """Check ``security.file_guard.allow_preview_outside_workspace``."""
    try:
        from qwenpaw.config import load_config

        return bool(
            load_config().security.file_guard.allow_preview_outside_workspace,
        )
    except Exception:
        return False


def _check_path(path: Path) -> str | None:
    """Return ``None`` when *path* is allowed, or an error reason string.

    When ``allow_preview_outside_workspace`` is enabled, skip the
    WORKING_DIR containment check so that console can preview files
    (e.g. media produced by tools) stored outside the workspace.
    The sensitive-file guard is **always** enforced.
    """
    resolved = path.resolve()
    # 1. Must not be a FileGuard-sensitive path.
    normalized = _normalize_path(str(resolved))
    # pylint: disable-next=protected-access
    if _file_guardian._is_sensitive(normalized):
        return "SENSITIVE_FILE_BLOCKED"
    # 2. Workspace scope check (skippable via config).
    if not _is_preview_outside_workspace_allowed():
        if not (
            resolved == _ALLOWED_ROOT or resolved.is_relative_to(_ALLOWED_ROOT)
        ):
            return "OUTSIDE_WORKSPACE"
    return None


@router.api_route(
    "/preview/{filepath:path}",
    methods=["GET", "HEAD"],
    summary="Preview file",
)
async def preview_file(
    filepath: str,
    request: Request,
):
    """Preview file."""
    normalized = unquote(filepath)

    # Normalize /C:/... to C:/... on Windows.
    if (
        len(normalized) >= 4
        and normalized[0] == "/"
        and normalized[2] == ":"
        and normalized[1].isalpha()
    ):
        normalized = normalized[1:]

    path = Path(normalized).expanduser()
    if not path.is_absolute():
        path = Path("/" + normalized)
    path = path.resolve()

    # 文件预览没有经过 Agent 路由，因此必须在这里按工作区归属再次收口。
    # 主体来自认证中间件，绝不能接受 URL 参数或前端传来的用户 ID。
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        config = load_config()
        owned_workspaces = [
            Path(agent_ref.workspace_dir).expanduser().resolve()
            for agent_ref in config.agents.profiles.values()
            if agent_ref.owner_user_id == principal.user_id
        ]
        if not any(
            path == workspace_dir or path.is_relative_to(workspace_dir)
            for workspace_dir in owned_workspaces
        ):
            # 使用 404 而不是 403，不向其他账号暴露文件路径是否真实存在。
            raise HTTPException(status_code=404, detail="Not found")

    reason = _check_path(path)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    if not os.access(path, os.R_OK):
        raise HTTPException(status_code=500, detail="Permission denied")
    return FileResponse(path, filename=path.name)
