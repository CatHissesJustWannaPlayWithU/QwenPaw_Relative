"""仅管理员可调用的用户管理接口。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth import (
    create_managed_user,
    delete_managed_user,
    get_user_by_username,
    is_admin_user,
    list_public_users,
    update_managed_user,
    verify_token,
)

router = APIRouter(prefix="/users", tags=["users"])


class UserListResponse(BaseModel):
    # 用户列表只包含 public_user() 过滤后的安全字段。
    users: list[dict]


class CreateUserRequest(BaseModel):
    # 新建用户时可以提交密码，但密码只交给认证模块加密保存。
    username: str
    password: str
    name: str = ""
    avatar: str = Field(default="", max_length=2_000_000)
    role: Literal["admin", "user"] = "user"
    status: Literal["active", "disabled"] = "active"


class UpdateUserRequest(BaseModel):
    # 所有字段均为可选；未传字段不会覆盖原有数据。
    username: str | None = None
    password: str | None = None
    name: str | None = None
    avatar: str | None = Field(default=None, max_length=2_000_000)
    role: Literal["admin", "user"] | None = None
    status: Literal["active", "disabled"] | None = None


def _current_username(request: Request) -> str:
    # 先验证 token，再确认用户仍存在且账号状态为 active。
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    username = verify_token(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = get_user_by_username(username)
    if not user or user.get("status") != "active":
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def _require_admin(request: Request) -> str:
    # 所有用户管理接口都会先经过这里，统一进行管理员权限校验。
    username = _current_username(request) #验证请求中的 token，找到当前用户，并确认账号仍然是 active。
    if not is_admin_user(username): #检查是不是管理员
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, #不是则后端返回403，越权了
            #这才是真正的权限控制。菜单是否显示只是界面行为，后端 403 才是安全规则。
            detail="Administrator permission required",
        )
    return username


@router.get("", response_model=UserListResponse)
async def list_users(request: Request): #list_users()为查询用户接口函数
    _require_admin(request)
    # 返回公开资料，避免把密码摘要等内部字段返回给管理页面。
    return UserListResponse(users=list_public_users())


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(request: Request, body: CreateUserRequest): #create_user()为新增用户接口函数
    _require_admin(request)
    try:
        # 创建、密码摘要和数据落盘都由认证模块统一处理。
        return create_managed_user(
            username=body.username,
            password=body.password,
            name=body.name,
            avatar=body.avatar,
            role=body.role,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{user_id}")
async def update_user( 
    user_id: str,
    request: Request,
    body: UpdateUserRequest,
): #update_user()为修改用户接口函数
    current_username = _require_admin(request)
    # 找到当前管理员本人，用于限制危险的自我修改操作。
    target = get_user_by_username(current_username)
    if not target:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # exclude_unset=True 保证前端未传的字段不会被写成 None。
    updates = body.model_dump(exclude_unset=True)
    # 用户 ID 是稳定主键，不能依靠可能被修改的用户名定位用户。
    target_user = next(
        (item for item in list_public_users() if item["id"] == user_id),
        None,
    )
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    if target_user["id"] == target.get("id"):
        # 管理员修改自己时，不能把自己改成无法继续管理系统的状态。
        if "username" in updates and updates["username"] != current_username:
            raise HTTPException(
                status_code=400,
                detail="Cannot change the current username from user management",
            )
        if updates.get("status") == "disabled":
            raise HTTPException(
                status_code=400,
                detail="Cannot disable the current user",
            )
        if updates.get("role") == "user":
            # 不能把系统中最后一个管理员降级为普通用户。
            admin_count = sum(
                item["role"] == "admin" for item in list_public_users()
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove the last administrator",
                )

    try:
        # 认证模块负责真正更新资料、密码摘要并写回用户数据文件。
        updated = update_managed_user(user_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    return updated


@router.delete("/{user_id}")
async def delete_user(user_id: str, request: Request): #delete_user()为删除用户接口函数
    current_username = _require_admin(request)
    try:
        # 业务层会阻止删除自己，以及删除系统中最后一个管理员。
        deleted = delete_managed_user(user_id, current_username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"deleted": True, "id": user_id}
