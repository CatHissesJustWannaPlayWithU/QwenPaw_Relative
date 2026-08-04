# -*- coding: utf-8 -*-
"""用户隔离：通过 Agent 上下文访问工作区的测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from qwenpaw.app import agent_context
from qwenpaw.app.principal import Principal
from qwenpaw.config.config import AgentProfileRef, Config


def _make_config() -> Config:
    """构造包含两个不同所属用户的最小 Agent 配置。"""
    config = Config()
    config.agents.active_agent = "admin_agent"
    config.agents.agent_order = ["admin_agent", "alice_agent"]
    config.agents.profiles = {
        "admin_agent": AgentProfileRef(
            id="admin_agent",
            workspace_dir="/tmp/admin_agent",
            owner_user_id="user_admin",
        ),
        "alice_agent": AgentProfileRef(
            id="alice_agent",
            workspace_dir="/tmp/alice_agent",
            owner_user_id="user_alice",
        ),
    }
    return config


def _make_request(user_id: str, manager) -> SimpleNamespace:
    """构造认证中间件已经写入 Principal 的最小请求对象。"""
    return SimpleNamespace(
        headers={},
        state=SimpleNamespace(
            principal=Principal(
                user_id=user_id,
                username=user_id,
                role="user",
            ),
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(multi_agent_manager=manager),
        ),
    )


@pytest.mark.asyncio
async def test_user_cannot_resolve_another_users_agent(monkeypatch):
    """客户端给出其他用户的 Agent ID 时，不能取得其工作区。"""
    manager = SimpleNamespace(get_agent=AsyncMock())
    monkeypatch.setattr(agent_context, "load_config", _make_config)

    with pytest.raises(HTTPException) as exc_info:
        await agent_context.get_agent_for_request(
            _make_request("user_alice", manager),
            agent_id="admin_agent",
        )

    assert exc_info.value.status_code == 404
    manager.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_without_agent_header_uses_own_agent(monkeypatch):
    """全局 active_agent 属于别人时，默认选择当前用户自己的 Agent。"""
    workspace = SimpleNamespace(agent_id="alice_agent")
    manager = SimpleNamespace(get_agent=AsyncMock(return_value=workspace))
    monkeypatch.setattr(agent_context, "load_config", _make_config)

    result = await agent_context.get_agent_for_request(
        _make_request("user_alice", manager),
    )

    assert result is workspace
    manager.get_agent.assert_awaited_once_with("alice_agent")
