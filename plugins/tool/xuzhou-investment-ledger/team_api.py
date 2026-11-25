"""徐州招商团队共享台账成员名册的管理员专用 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.auth import get_user_by_username
from qwenpaw.app.principal import Principal, get_current_principal

from .shared_workspace import add_team_member, list_team_members, remove_team_member


router = APIRouter()


class AddTeamMemberRequest(BaseModel):
    """管理员添加团队成员时唯一允许提交的业务字段。

    请求体只有平台登录用户名；``extra="forbid"`` 明确拒绝 ``user_id``、角色、审计或
    名册路径等字段：成员身份只能由服务端平台用户目录按用户名解析，客户端、模型或前端
    不能直传 user_id 伪造成员，也不能借本接口指定共享目录或改写台账业务数据。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(..., min_length=1, max_length=100)


class RemoveTeamMemberRequest(BaseModel):
    """管理员移除团队成员时唯一允许提交的业务字段。

    请求体只有平台登录用户名；不允许提交 ``user_id``、审计或任何名册结构字段。移除
    只影响名册授权，不删除台账、快照或该成员已产生的业务审计，操作者只能来自服务端
    认证主体。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(..., min_length=1, max_length=100)


def _require_admin_principal(request: Request) -> Principal:
    """强制当前请求必须由管理员发起，非 admin 一律 403，身份只取服务端 Principal。

    输入是已认证 HTTP 请求，输出为管理员主体。成员名册决定普通成员能否访问共享台账，
    而名册本身只能由平台角色的管理员维护；请求体、请求头或模型都不能自称管理员。
    """
    principal = get_current_principal(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员可以管理徐州招商团队名册",
        )
    return principal


@router.get("/members", status_code=status.HTTP_200_OK)
async def list_xuzhou_team_members(request: Request) -> dict[str, Any]:
    """由管理员查看共享台账的成员名册与全部授权审计，普通成员一律拒绝。

    输入只有已认证请求；输出为名册成员和追加式审计的副本。函数不接受用户 ID、筛选或
    分页参数，不修改名册，也不读取或返回台账中的线索、证据、评分等业务数据。
    """
    _require_admin_principal(request)
    try:
        return {"ok": True, **list_team_members()}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法读取招商团队名册",
        ) from exc


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def add_xuzhou_team_member(
    body: AddTeamMemberRequest,
    request: Request,
) -> dict[str, Any]:
    """由管理员把一名平台活跃用户加入共享台账团队名册，普通用户一律拒绝。

    输入是仅含用户名的受限请求体和已认证请求；输出为新成员记录与服务器追加的授权审计。
    函数先强制 ``principal.is_admin``，再用服务端平台用户目录把用户名解析为 user_id，
    拒绝客户端直传 user_id；用户不存在或已停用时明确失败。核心层在排他锁内写入名册并
    逐字保护既有成员与审计，本接口不会读取或修改台账业务数据。
    """
    principal = _require_admin_principal(request)
    user = get_user_by_username(body.username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该平台登录用户",
        )
    if user.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="该平台用户已被停用，不能加入招商团队名册",
        )
    user_id = user.get("id")
    username = user.get("username")
    if not isinstance(user_id, str) or not user_id.strip() or not isinstance(username, str):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="平台用户记录缺少有效用户ID",
        )
    try:
        result = add_team_member(
            user_id=user_id,
            username=username,
            added_by_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法保存招商团队名册",
        ) from exc


@router.post("/members/remove", status_code=status.HTTP_200_OK)
async def remove_xuzhou_team_member(
    body: RemoveTeamMemberRequest,
    request: Request,
) -> dict[str, Any]:
    """由管理员按用户名把一名成员移出共享台账团队名册，普通用户一律拒绝。

    输入是仅含用户名的受限请求体和已认证请求；输出为被移除的成员记录与服务器追加的
    授权审计。操作者只取 ``principal.user_id``；名册中不存在该成员时明确失败。移除只
    撤销共享台账访问授权，不删除台账、快照或该成员已产生的任何业务审计。
    """
    principal = _require_admin_principal(request)
    try:
        result = remove_team_member(
            username=body.username,
            removed_by_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法保存招商团队名册",
        ) from exc
