# -*- coding: utf-8 -*-
"""用户隔离：全局文件预览接口的测试。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.principal import Principal
from qwenpaw.app.routers import files as files_router
from qwenpaw.config.config import AgentProfileRef, Config


def _request(user_id: str) -> SimpleNamespace:
    """构造认证中间件已写入 Principal 的最小请求。"""
    return SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(
                user_id=user_id,
                username=user_id,
                role="user",
            ),
        ),
    )


def _config(alice_workspace, bob_workspace) -> Config:
    """构造两个用户各自拥有一个工作区的根配置。"""
    config = Config()
    config.agents.profiles = {
        "alice": AgentProfileRef(
            id="alice",
            workspace_dir=str(alice_workspace),
            owner_user_id="user_alice",
        ),
        "bob": AgentProfileRef(
            id="bob",
            workspace_dir=str(bob_workspace),
            owner_user_id="user_bob",
        ),
    }
    return config


@pytest.mark.asyncio
async def test_preview_file_hides_another_users_workspace(monkeypatch, tmp_path):
    """文件预览不能跳过 Agent 选择直接读取其他用户工作区。"""
    alice_workspace = tmp_path / "alice"
    bob_workspace = tmp_path / "bob"
    alice_workspace.mkdir()
    bob_workspace.mkdir()
    (alice_workspace / "mine.txt").write_text("alice", encoding="utf-8")
    (bob_workspace / "secret.txt").write_text("bob", encoding="utf-8")

    config = _config(alice_workspace, bob_workspace)
    monkeypatch.setattr(files_router, "load_config", lambda: config)
    monkeypatch.setattr(files_router, "_ALLOWED_ROOT", tmp_path.resolve())
    monkeypatch.setattr(
        files_router,
        "_is_preview_outside_workspace_allowed",
        lambda: False,
    )

    own_response = await files_router.preview_file(
        str(alice_workspace / "mine.txt"),
        _request("user_alice"),
    )
    assert own_response.status_code == 200

    with pytest.raises(HTTPException) as exc_info:
        await files_router.preview_file(
            str(bob_workspace / "secret.txt"),
            _request("user_alice"),
        )

    assert exc_info.value.status_code == 404
