# -*- coding: utf-8 -*-
"""用户隔离：当前用户主体的基础测试。"""
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from qwenpaw.app.auth import AuthMiddleware
from qwenpaw.app.principal import Principal, get_current_principal


def _make_request() -> Request:
    """构造一个最小 HTTP 请求，用于测试 Request.state。"""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "headers": [],
            "state": {},
        },
    )


def test_get_current_principal_returns_middleware_value():
    request = _make_request()
    request.state.principal = Principal(
        user_id="user_alice",
        username="alice",
        role="user",
    )

    principal = get_current_principal(request)

    assert principal.user_id == "user_alice"
    assert principal.username == "alice"
    assert principal.is_admin is False


def test_get_current_principal_rejects_missing_context():
    request = _make_request()

    with pytest.raises(HTTPException) as exc_info:
        get_current_principal(request)

    assert exc_info.value.status_code == 401


def test_auth_middleware_attaches_principal(monkeypatch):
    import qwenpaw.app.auth as auth_module

    application = FastAPI()

    @application.get("/api/check-principal")
    async def check_principal(request: Request):
        principal = get_current_principal(request)
        return {
            "user_id": principal.user_id,
            "username": principal.username,
            "role": principal.role,
        }

    application.add_middleware(AuthMiddleware)

    monkeypatch.setattr(
        AuthMiddleware,
        "_should_skip_auth",
        staticmethod(lambda _request: False),
    )
    monkeypatch.setattr(
        auth_module,
        "verify_token",
        lambda _token: "alice",
    )
    monkeypatch.setattr(
        auth_module,
        "get_user_by_username",
        lambda _username: {
            "id": "user_alice",
            "username": "alice",
            "role": "user",
            "status": "active",
        },
    )

    client = TestClient(application)
    response = client.get(
        "/api/check-principal",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_alice",
        "username": "alice",
        "role": "user",
    }


def test_auth_middleware_blocks_global_operations_for_regular_users(
    monkeypatch,
):
    """普通用户可访问自己的资源，但不能改动服务器共享配置。"""
    import qwenpaw.app.auth as auth_module

    application = FastAPI()

    @application.get("/api/providers")
    async def providers():
        return {"ok": True}

    @application.get("/api/agents/alice/chats")
    async def own_workspace():
        return {"ok": True}

    @application.get("/api/console/debug/backend-logs")
    async def backend_logs():
        return {"ok": True}

    @application.put("/api/agents/alice/workspace/transcription-provider")
    async def scoped_transcription_settings():
        return {"ok": True}

    application.add_middleware(AuthMiddleware)
    monkeypatch.setattr(
        AuthMiddleware,
        "_should_skip_auth",
        staticmethod(lambda _request: False),
    )
    monkeypatch.setattr(auth_module, "verify_token", lambda _token: "alice")
    monkeypatch.setattr(
        auth_module,
        "get_user_by_username",
        lambda _username: {
            "id": "user_alice",
            "username": "alice",
            "role": "user",
            "status": "active",
        },
    )

    client = TestClient(application)
    headers = {"Authorization": "Bearer test-token"}

    assert client.get("/api/providers", headers=headers).status_code == 403
    assert (
        client.get(
            "/api/console/debug/backend-logs",
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/agents/alice/workspace/transcription-provider",
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.get("/api/agents/alice/chats", headers=headers).status_code
        == 200
    )


def test_public_settings_language_is_read_only(monkeypatch):
    """登录页可读取语言，但不能借公开路径写入全局设置。"""
    import qwenpaw.app.auth as auth_module

    monkeypatch.setattr(auth_module, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth_module, "has_registered_users", lambda: True)

    get_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/settings/language",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        },
    )
    put_request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/settings/language",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        },
    )

    assert AuthMiddleware._should_skip_auth(get_request) is True
    assert AuthMiddleware._should_skip_auth(put_request) is False
