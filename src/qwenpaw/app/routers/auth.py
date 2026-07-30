# -*- coding: utf-8 -*-
"""认证相关的 API 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...constant import EnvVarLoader
from ..auth import (
    authenticate,
    get_user_by_username,
    has_registered_users,
    is_auth_enabled,
    register_user,
    revoke_all_tokens,
    revoke_token,
    update_credentials,
    verify_token,
    public_user,
    resolve_client_ip,
)
from ..rate_limiter import rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    expires_in: int | None = (
        None  # token 有效期（秒），-1 或 0 表示永久 token
    )


class LoginResponse(BaseModel):
    token: str
    username: str
    user: dict | None = None


class UserResponse(BaseModel):
    id: str
    username: str
    name: str
    avatar: str = ""
    role: str
    status: str
    created_at: str = ""
    last_login: str = ""


class RegisterRequest(BaseModel):
    username: str
    password: str = Field(min_length=8)
    name: str = ""
    avatar: str = Field(default="", max_length=2_000_000)
    expires_in: int | None = (
        None  # token 有效期（秒），-1 或 0 表示永久 token
    )


class AuthStatusResponse(BaseModel):
    enabled: bool
    has_users: bool


@router.post("/login")
async def login(request: Request, req: LoginRequest):
    """使用用户名和密码登录。

    `expires_in` 是可选字段：
    - 正整数：token 在 N 秒后过期
    - 0 或 -1：永久 token（100 年）
    - ``None`` 或不传：默认 7 天
    """
    if not is_auth_enabled():
        return LoginResponse(token="", username="")

    # 限流按客户端 IP 统计，不能直接信任代理请求头。
    client_ip = resolve_client_ip(request)

    # 账号被锁定时，不再继续校验密码。
    if rate_limiter.is_user_locked(req.username):
        raise HTTPException(
            status_code=423,
            detail="Account temporarily locked. Please try again later",
        )

    # IP 被锁定或短时间请求过多时，直接拒绝登录请求。
    if rate_limiter.is_ip_locked(client_ip):
        raise HTTPException(
            status_code=423,
            detail="Too many login attempts. Please try again later",
        )

    if rate_limiter.is_ip_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please slow down",
        )

    # 业务层负责核对密码并创建 token。
    token = authenticate(req.username, req.password, req.expires_in)
    if token is None:
        # 记录失败次数，供账号锁定和 IP 限流判断使用。
        rate_limiter.record_login_attempt(
            client_ip,
            req.username,
            success=False,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    # 成功后重置相应的失败记录。
    rate_limiter.record_login_attempt(client_ip, req.username, success=True)

    user = get_user_by_username(req.username)
    return LoginResponse(
        token=token,
        username=req.username,
        user=public_user(user) if user else None,
    )


@router.post("/register")
async def register(req: RegisterRequest):
    """注册账号，并签发当前会话的 token。

    `expires_in` 是可选字段：
    - 正整数：token 在 N 秒后过期
    - 0 或 -1：永久 token（100 年）
    - ``None`` 或不传：默认 7 天
    """
    env_flag = EnvVarLoader.get_str("QWENPAW_AUTH_ENABLED", "").strip().lower()
    if env_flag not in ("true", "1", "yes"):
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    if not req.username.strip() or not req.password.strip():
        raise HTTPException(
            status_code=400,
            detail="Username and password are required",
        )

    # 注册逻辑由认证模块处理，密码不会在路由层保存。
    token = register_user(
        req.username.strip(),
        req.password,
        req.expires_in,
        name=req.name,
        avatar=req.avatar,
    )
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="Username already exists or registration failed",
        )

    user = get_user_by_username(req.username.strip())
    return LoginResponse(
        token=token,
        username=req.username.strip(),
        user=public_user(user) if user else None,
    )


@router.get("/status")
async def auth_status():
    """查询认证是否启用，以及系统中是否已经有用户。"""
    return AuthStatusResponse(
        enabled=is_auth_enabled(),
        has_users=has_registered_users(),
    )


@router.get("/verify")
async def verify(request: Request):
    """验证调用方携带的 Bearer token 是否仍然有效。"""
    if not is_auth_enabled():
        return {"valid": True, "username": ""}

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="No token provided")

    username = verify_token(token)
    if username is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )

    return {"valid": True, "username": username}

#右上角显示用户信息，本质上只需要后端提供“当前登录用户的公开资料”
#接口：GET /api/auth/me
@router.get("/me", response_model=UserResponse)
async def current_user(request: Request):
    """返回当前 Bearer token 对应用户的公开资料。"""
    #先从请求头读取 token，验证 token，并找到真正的用户。
    #再把用户转换成安全的公开资料。
    return UserResponse(**public_user(_current_user(request)))


class UpdateProfileRequest(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = Field(default=None, min_length=8)
    name: str | None = None
    avatar: str | None = Field(default=None, max_length=2_000_000)
    expires_in: int | None = (
        None  # token 有效期（秒），-1 或 0 表示永久 token
    )

#个人资料修改接口是：POST /api/auth/update-profile
@router.post("/update-profile")
async def update_profile(req: UpdateProfileRequest, request: Request):
    """修改当前已登录用户的用户名、密码或公开资料。"""
    # 先从 token 得到实际操作的账号，不能由请求体指定修改对象。
    caller = _current_user(request)
    caller_username = caller.get("username", "")
    if (
        req.name is None
        and req.avatar is None
        and not req.new_username
        and not req.new_password
    ):
        raise HTTPException(
            status_code=400,
            detail="Nothing to update",
        )

    if req.new_username is not None and not req.new_username.strip():
        raise HTTPException(
            status_code=400,
            detail="Username cannot be empty",
        )

    if req.new_password is not None and not req.new_password.strip():
        raise HTTPException(
            status_code=400,
            detail="Password cannot be empty",
        )

    # 业务层会先验证当前密码，更新成功后返回新的 token。
    token = update_credentials(
        current_password=req.current_password,
        new_username=req.new_username,
        new_password=req.new_password,
        expiry_seconds=req.expires_in,
        username=caller_username,
        name=req.name,
        avatar=req.avatar,
    )
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Current password is incorrect",
        )

    username = req.new_username.strip() if req.new_username else caller_username
    user = get_user_by_username(username)
    return LoginResponse(
        token=token,
        username=username,
        user=public_user(user) if user else None,
    )


class RevokeTokenRequest(BaseModel):
    token: str | None = (
        None  # 可选：传入时撤销指定 token，不传则撤销当前 token
    )


def _current_username(request: Request) -> str:
    # token 有效后还要确认账号未被删除且仍是 active。
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    username = verify_token(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = get_user_by_username(username)
    if not user or user.get("status") != "active":
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def _current_user(request: Request) -> dict:
    username = _current_username(request)
    user = get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

#退出登录接口：POST /api/auth/revoke-token
#它不会“删除用户”，只会撤销当前登录 token。
@router.post("/revoke-token")
async def revoke_single_token(req: RevokeTokenRequest, request: Request):
    """将一个 token 加入撤销列表。

    请求体传入 `token` 时撤销指定 token；不传时撤销用于认证的当前 token。

    这样既可以撤销其他设备泄露的 token，也可以退出当前会话。
    """
    if not is_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    # 先验证当前 token，避免未登录用户撤销任意 token。
    auth_header = request.headers.get("Authorization", "")
    caller_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not caller_token or verify_token(caller_token) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 有指定 token 时撤销指定值，否则默认退出当前会话。
    token_to_revoke = req.token if req.token else caller_token
    is_current_token = token_to_revoke == caller_token

    success = revoke_token(token_to_revoke)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to revoke token",
        )

    message = (
        "Current token has been revoked. Please login again."
        if is_current_token
        else "Specified token has been revoked."
    )

    return {
        "message": message,
        "revoked": True,
        "revoked_current_token": is_current_token,
    }


@router.post("/revoke-all-tokens")
async def revoke_all_sessions(request: Request):
    """通过轮换 JWT 密钥撤销全部已签发的 token。

    此接口需要登录。调用后，之前签发的所有 token 都会失效，用户需要重新登录。

    当需要清除所有会话时，例如密码重置或发生安全事件，轮换密钥比逐个撤销 token 更高效。
    """
    if not is_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    # 该操作影响全部会话，因此必须先验证调用方已经登录。
    auth_header = request.headers.get("Authorization", "")
    caller_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not caller_token or verify_token(caller_token) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    success = revoke_all_tokens()
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to revoke tokens",
        )

    return {
        "message": "All tokens have been revoked. Please login again.",
        "revoked": True,
    }
