# -*- coding: utf-8 -*-
"""为用户隔离补齐智能体归属与首次可用工作区。"""
from __future__ import annotations

import logging
from pathlib import Path

from ..agents.utils import normalize_agent_language
from ..constant import WORKING_DIR
from ..config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    ChannelConfig,
    HeartbeatConfig,
    MCPConfig,
    ToolsConfig,
    generate_short_agent_id,
    save_agent_config,
)
from ..config.utils import load_config, save_config

logger = logging.getLogger(__name__)


def migrate_legacy_agents_to_first_admin() -> int:
    """把纯旧版单用户智能体一次性归属给首个有效管理员。

    只有全部 Agent 都没有 ``owner_user_id`` 时才执行。这样可将旧版
    单用户安装安全迁移为“首个管理员拥有原有资源”，而不会擅自接管
    已经开始使用新归属模型的混合配置。
    """
    from .auth import is_auth_enabled, list_public_users

    if not is_auth_enabled():
        return 0

    config = load_config()
    profiles = config.agents.profiles
    if not profiles or any(
        profile.owner_user_id is not None
        for profile in profiles.values()
    ):
        return 0

    first_admin = next(
        (
            user
            for user in list_public_users()
            if user.get("role") == "admin"
            and user.get("status") == "active"
        ),
        None,
    )
    if first_admin is None:
        return 0

    owner_user_id = str(first_admin["id"])
    for profile in profiles.values():
        profile.owner_user_id = owner_user_id
    save_config(config)
    logger.info(
        "Assigned %d legacy agents to first administrator %s",
        len(profiles),
        first_admin["username"],
    )
    return len(profiles)


def _initialize_workspace(workspace_dir: Path, language: str) -> None:
    """复用现有 Agent 初始化逻辑，建立会话、记忆和初始文件目录。"""
    # 迁移模块已经使用同样的延迟导入方式复用这段初始化逻辑，
    # 避免认证模块在导入阶段与路由包形成循环依赖。
    from .routers.agents import _initialize_agent_workspace

    _initialize_agent_workspace(
        workspace_dir,
        skill_names=[],
        language=language,
    )


def _next_personal_agent_id(existing_ids: set[str]) -> str:
    """生成不与已有 Agent 冲突的服务端标识。"""
    for _ in range(10):
        candidate = generate_short_agent_id()
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("Failed to allocate a personal agent ID")


def ensure_personal_agent(user_id: str, username: str) -> str:
    """确保已登录用户至少拥有一个独立智能体和工作区。

    资源隔离的根是 ``owner_user_id``，而会话、记忆、文件等资源都在
    该 Agent 的 ``workspace_dir`` 内。因此这里不创建一份公共目录，
    而是为没有任何 Agent 的用户创建新的私有工作区。
    """
    config = load_config()
    for agent_id, profile in config.agents.profiles.items():
        if profile.owner_user_id == user_id:
            return agent_id

    agent_id = _next_personal_agent_id(set(config.agents.profiles))
    workspace_dir = (WORKING_DIR / "workspaces" / agent_id).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    language = normalize_agent_language(config.agents.language or "zh")

    # 先在根配置登记归属。save_agent_config() 会从根配置查找工作区，
    # 因而必须在写 agent.json 前完成这一步。
    config.agents.profiles[agent_id] = AgentProfileRef(
        id=agent_id,
        workspace_dir=str(workspace_dir),
        owner_user_id=user_id,
        enabled=True,
    )
    if agent_id not in config.agents.agent_order:
        config.agents.agent_order.append(agent_id)
    save_config(config)

    try:
        _initialize_workspace(workspace_dir, language)
        agent_config = AgentProfileConfig(
            id=agent_id,
            name=f"{username} 的智能体",
            description=f"为 {username} 自动创建的个人智能体",
            workspace_dir=str(workspace_dir),
            language=language,
            channels=ChannelConfig(),
            mcp=MCPConfig(),
            heartbeat=HeartbeatConfig(),
            tools=ToolsConfig(),
        )
        save_agent_config(agent_id, agent_config)
    except Exception:
        # 用户账号已经存在，但半成品 Agent 绝不能出现在可访问列表里。
        # 工作区文件保留给运维排查，配置记录则回滚，后续登录可重试创建。
        config.agents.profiles.pop(agent_id, None)
        config.agents.agent_order = [
            item for item in config.agents.agent_order if item != agent_id
        ]
        save_config(config)
        logger.exception(
            "Failed to provision personal agent for user %s",
            user_id,
        )
        raise

    logger.info(
        "Provisioned personal agent %s for user %s",
        agent_id,
        user_id,
    )
    return agent_id
