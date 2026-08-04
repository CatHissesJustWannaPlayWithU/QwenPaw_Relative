# -*- coding: utf-8 -*-
"""用户隔离：审批与工具调用等进程内运行时记录也必须按所属智能体过滤。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from qwenpaw.app.principal import Principal
from qwenpaw.app.routers import approval as approval_router
from qwenpaw.app.routers import tool_calls as tool_calls_router
from qwenpaw.config.config import AgentProfileRef, Config


def _config() -> Config:
    """构造两个用户分别拥有一个智能体的最小配置。"""
    config = Config()
    config.agents.profiles = {
        "alice-agent": AgentProfileRef(
            id="alice-agent",
            workspace_dir="/tmp/alice-agent",
            owner_user_id="user_alice",
        ),
        "bob-agent": AgentProfileRef(
            id="bob-agent",
            workspace_dir="/tmp/bob-agent",
            owner_user_id="user_bob",
        ),
    }
    return config


def _request() -> SimpleNamespace:
    """构造认证中间件已写入 Alice Principal 的最小请求。"""
    return SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(
                user_id="user_alice",
                username="alice",
                role="user",
            ),
        ),
        app=SimpleNamespace(state=SimpleNamespace()),
    )


def _pending(request_id: str, owner_agent_id: str) -> SimpleNamespace:
    """构造审批列表序列化所需的最小待审批记录。"""
    return SimpleNamespace(
        request_id=request_id,
        session_id="session-1",
        root_session_id="session-1",
        owner_agent_id=owner_agent_id,
        agent_id=owner_agent_id,
        tool_name="shell",
        severity="medium",
        findings_count=0,
        created_at=1.0,
        timeout_seconds=60,
        result_summary="",
    )


@pytest.mark.asyncio
async def test_approval_list_hides_other_users_pending_requests(monkeypatch):
    """同一个进程中的待审批记录不能跨账号显示。"""
    service = SimpleNamespace(
        get_pending_by_root_session=AsyncMock(
            return_value=[
                _pending("approval-alice", "alice-agent"),
                _pending("approval-bob", "bob-agent"),
            ],
        ),
    )
    monkeypatch.setattr(approval_router, "get_approval_service", lambda: service)
    monkeypatch.setattr(approval_router, "load_config", _config)
    monkeypatch.setattr(
        approval_router,
        "approval_display_fields",
        lambda _pending_record: {},
    )

    response = await approval_router.get_approval_list(
        _request(),
        session_id="session-1",
    )

    assert response.count == 1
    assert response.pending_approvals[0]["request_id"] == "approval-alice"


@pytest.mark.asyncio
async def test_approval_action_hides_other_users_pending_request(monkeypatch):
    """猜到审批 ID 也不能代替另一个用户确认工具执行。"""
    service = SimpleNamespace(
        get_request=AsyncMock(
            return_value=_pending("approval-bob", "bob-agent"),
        ),
    )
    monkeypatch.setattr(approval_router, "get_approval_service", lambda: service)
    monkeypatch.setattr(approval_router, "load_config", _config)

    body = approval_router.ApprovalActionRequest(
        request_id="approval-bob",
        session_id="session-1",
    )
    with pytest.raises(HTTPException) as exc_info:
        await approval_router.post_approval_approve(_request(), body)

    assert exc_info.value.status_code == 404


def _tool_entry(tool_call_id: str, agent_id: str) -> SimpleNamespace:
    """构造工具调用列表转换所需的最小运行时记录。"""
    return SimpleNamespace(
        ctx=SimpleNamespace(
            tool_call_id=tool_call_id,
            tool_name="shell",
            session_id="session-1",
            agent_id=agent_id,
            started_at=0.0,
            deadline=None,
            extra={},
        ),
        status=SimpleNamespace(value="running"),
        end_state=None,
        force_cancelled=False,
    )


@pytest.mark.asyncio
async def test_tool_call_list_hides_other_users_entries(monkeypatch):
    """工具调用协调器共用进程内存时，列表仍只返回当前用户的数据。"""
    coordinator = SimpleNamespace(
        list_entries=lambda **_kwargs: [
            _tool_entry("call-alice", "alice-agent"),
            _tool_entry("call-bob", "bob-agent"),
        ],
    )
    request = _request()
    request.app.state.app_services = SimpleNamespace(
        tool_coordinator=coordinator,
    )
    monkeypatch.setattr(tool_calls_router, "load_config", _config)

    response = await tool_calls_router.list_calls("session-1", request)

    assert response.total == 1
    assert response.items[0].tool_call_id == "call-alice"
