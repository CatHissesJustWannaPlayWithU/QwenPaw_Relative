# -*- coding: utf-8 -*-
"""Agent context utilities for multi-agent support.

Provides utilities to get the correct agent instance for each request.
"""
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from fastapi import Request
from .multi_agent_manager import MultiAgentManager
from .principal import Principal
from ..config.utils import load_config

if TYPE_CHECKING:
    from .workspace import Workspace

# Context variable to store current agent ID across async calls
_current_agent_id: ContextVar[Optional[str]] = ContextVar(
    "current_agent_id",
    default=None,
)

# Context variable to store current session id across async calls
_current_session_id: ContextVar[Optional[str]] = ContextVar(
    "current_session_id",
    default=None,
)

# Context variable to store current root session id for cross-session approval
_current_root_session_id: ContextVar[Optional[str]] = ContextVar(
    "current_root_session_id",
    default=None,
)

_current_user_id: ContextVar[Optional[str]] = ContextVar(
    "current_user_id",
    default=None,
)

_current_channel: ContextVar[Optional[str]] = ContextVar(
    "current_channel",
    default=None,
)


async def get_agent_for_request(
    request: Request,
    agent_id: Optional[str] = None,
) -> "Workspace":
    """Get agent workspace for current request.

    Priority:
    1. agent_id parameter (explicit override)
    2. request.state.agent_id (from agent-scoped router)
    3. X-Agent-Id header (from frontend)
    4. Active agent from config

    Args:
        request: FastAPI request object
        agent_id: Agent ID override (highest priority)

    Returns:
        Workspace for the specified or active agent

    Raises:
        HTTPException: If agent not found
    """
    from fastapi import HTTPException

    # 认证中间件启用时会写入 Principal；未启用认证的旧部署保留原行为。
    principal = getattr(request.state, "principal", None)
    if principal is not None and not isinstance(principal, Principal):
        raise HTTPException(
            status_code=401,
            detail="Authentication context is invalid",
        )

    # Determine which agent to use
    target_agent_id = agent_id

    # Check request.state.agent_id (set by agent-scoped router)
    if not target_agent_id and hasattr(request.state, "agent_id"):
        target_agent_id = request.state.agent_id

    # Check X-Agent-Id header
    if not target_agent_id:
        target_agent_id = request.headers.get("X-Agent-Id")

    # 统一读取配置，随后既校验 Agent 存在性，也校验它是否属于当前用户。
    config = load_config()
    if not target_agent_id:
        # 全局 active_agent 可能属于另一个用户，不能直接作为登录用户的默认 Agent。
        active_agent_id = config.agents.active_agent or "default"
        active_agent = config.agents.profiles.get(active_agent_id)
        if (
            principal is None
            or (
                active_agent is not None
                and active_agent.owner_user_id == principal.user_id
            )
        ):
            target_agent_id = active_agent_id
        else:
            # 没有显式选择时，选择当前用户在全局顺序中排在最前面的 Agent。
            ordered_ids = [
                *config.agents.agent_order,
                *config.agents.profiles.keys(),
            ]
            for candidate_id in dict.fromkeys(ordered_ids):
                candidate = config.agents.profiles.get(candidate_id)
                if candidate and candidate.owner_user_id == principal.user_id:
                    target_agent_id = candidate_id
                    break

    # Check if agent exists and is enabled
    if target_agent_id not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{target_agent_id}' not found",
        )

    agent_ref = config.agents.profiles[target_agent_id]

    # X-Agent-Id 和路径中的 agentId 都来自客户端，只能表示“想访问谁”。
    # 是否允许访问必须由服务端保存的 owner_user_id 与当前用户 ID 决定。
    if principal is not None and agent_ref.owner_user_id != principal.user_id:
        raise HTTPException(
            status_code=404,
            detail="Agent not found",
        )

    if not getattr(agent_ref, "enabled", True):
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{target_agent_id}' is disabled",
        )

    # Get MultiAgentManager
    if not hasattr(request.app.state, "multi_agent_manager"):
        raise HTTPException(
            status_code=500,
            detail="MultiAgentManager not initialized",
        )

    manager: MultiAgentManager = request.app.state.multi_agent_manager

    try:
        workspace = await manager.get_agent(target_agent_id)
        if not workspace:
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{target_agent_id}' not found",
            )
        return workspace
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get agent: {str(e)}",
        ) from e


def get_coding_dir(workspace: "Workspace") -> Path:
    """Return the active coding project directory for *workspace*.

    If the agent has set a ``coding_mode.project_dir`` in its config, that
    path is returned only when it stays inside this agent's workspace.
    Otherwise the agent's default ``workspace_dir`` is used.
    """
    from ..config.config import load_agent_config

    try:
        config = load_agent_config(workspace.agent_id)
        project_dir = (
            config.coding_mode.project_dir if config.coding_mode else None
        )
    except Exception:
        project_dir = None

    workspace_dir = workspace.workspace_dir.resolve()
    if project_dir:
        candidate = Path(project_dir).expanduser().resolve()
        projects_dir = workspace_dir / "coding_projects"
        if candidate == workspace_dir or candidate.is_relative_to(projects_dir):
            return candidate

    # 旧配置可能记录了任意绝对路径。不能让它把代码、Git 与文件接口
    # 带到另一个用户的目录，因此在边界外时安全地回退到本工作区。
    return workspace_dir


def get_active_agent_id() -> str:
    """Get current active agent ID from config.

    Returns:
        Active agent ID, defaults to "default"
    """
    try:
        config = load_config()
        return config.agents.active_agent or "default"
    except Exception:
        return "default"


def set_current_agent_id(agent_id: str) -> None:
    """Set current agent ID in context.

    Args:
        agent_id: Agent ID to set
    """
    _current_agent_id.set(agent_id)


def get_current_agent_id() -> str:
    """Get current agent ID from context or config fallback.

    Returns:
        Current agent ID, defaults to active agent or "default"
    """
    agent_id = _current_agent_id.get()
    if agent_id:
        return agent_id
    return get_active_agent_id()


def set_current_session_id(session_id: str) -> None:
    _current_session_id.set(session_id)


def get_current_session_id() -> Optional[str]:
    return _current_session_id.get()


def set_current_root_session_id(root_session_id: Optional[str]) -> None:
    """Set current root session ID in context.

    Args:
        root_session_id: Root session ID to set
    """
    _current_root_session_id.set(root_session_id)


def get_current_root_session_id() -> Optional[str]:
    """Get current root session ID from context.

    Returns:
        Root session ID or None
    """
    return _current_root_session_id.get()


def set_current_user_id(user_id: Optional[str]) -> None:
    """Set current user ID in context."""
    _current_user_id.set(user_id)


def get_current_user_id() -> Optional[str]:
    """Get current user ID from context."""
    return _current_user_id.get()


def set_current_channel(channel: Optional[str]) -> None:
    """Set current channel in context."""
    _current_channel.set(channel)


def get_current_channel() -> Optional[str]:
    """Get current channel from context."""
    return _current_channel.get()
