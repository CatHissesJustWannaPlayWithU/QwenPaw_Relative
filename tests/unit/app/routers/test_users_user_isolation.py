# -*- coding: utf-8 -*-
"""用户隔离：用户生命周期不能制造无人归属的智能体。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import users as users_router
from qwenpaw.config.config import AgentProfileRef, Config


def _admin_request() -> SimpleNamespace:
    """构造携带管理员 Bearer Token 的最小请求对象。"""
    return SimpleNamespace(
        headers={"Authorization": "Bearer admin-token"},
    )


@pytest.mark.asyncio
async def test_delete_user_rejects_when_user_still_owns_agents(monkeypatch):
    """先迁移或清理智能体，才能删除其所属账号。"""
    config = Config()
    config.agents.profiles = {
        "alice-agent": AgentProfileRef(
            id="alice-agent",
            workspace_dir="/tmp/alice-agent",
            owner_user_id="user_alice",
        ),
    }

    users = [
        {
            "id": "user_admin",
            "username": "admin",
            "role": "admin",
            "status": "active",
        },
        {
            "id": "user_alice",
            "username": "alice",
            "role": "user",
            "status": "active",
        },
    ]
    monkeypatch.setattr(users_router, "verify_token", lambda _token: "admin")
    monkeypatch.setattr(
        users_router,
        "get_user_by_username",
        lambda username: next(
            (item for item in users if item["username"] == username),
            None,
        ),
    )
    monkeypatch.setattr(users_router, "is_admin_user", lambda _username: True)
    monkeypatch.setattr(users_router, "list_public_users", lambda: users)
    monkeypatch.setattr(users_router, "load_config", lambda: config)

    with pytest.raises(HTTPException) as exc_info:
        await users_router.delete_user("user_alice", _admin_request())

    assert exc_info.value.status_code == 409
    assert "alice-agent" in exc_info.value.detail
