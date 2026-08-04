# -*- coding: utf-8 -*-
"""当前请求用户主体与资源访问范围。"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status


@dataclass(frozen=True) #frozen=True 表示对象创建后不能被随意修改，避免请求执行中身份被改掉。
class Principal: #不是数据库用户表，也不保存密码；它只代表“这一次 HTTP 请求的调用者”。
    """已通过认证、可用于资源授权的当前用户。"""

    user_id: str #user_id 是资源归属的核心。即使用户之后修改 username，历史数据仍归同一个 user_id。
    username: str
    role: str #role 解决管理员权限；owner_user_id 将在后续解决普通用户之间的数据隔离。
    tenant_id: str = "local" #tenant_id="local" 是为未来团队/组织隔离预留，现在所有用户都在本机默认租户。

    @property
    def is_admin(self) -> bool:
        """判断当前用户是否具有管理员角色。"""
        return self.role == "admin"


def get_current_principal(request: Request) -> Principal:
    #get_current_principal() 是以后所有资源接口取得当前用户的统一入口。
    """从认证中间件写入的请求状态中获取当前用户主体。"""
    try:
        principal = request.state.principal
    except AttributeError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication context is missing",
        ) from exc

    if not isinstance(principal, Principal):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication context is invalid",
        )

    return principal