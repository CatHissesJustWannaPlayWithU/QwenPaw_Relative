"""人工补充公开来源的已认证 HTTP 接口。

接口只允许徐州团队成员提交一个公开网页网址和可选登记说明。用户身份、检测时间、来源
状态、目录 ID、抓取范围与所有审计字段都由服务器生成；浏览器不能提交 Cookie、账号、
API Key、请求头、抓取深度、搜索词、证据正文或操作者 ID。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .public_source_supplement import (
    add_public_source_supplement,
    list_public_source_supplement_status,
    preflight_public_source,
    retest_public_source_supplement,
    set_public_source_supplement_status,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class PublicSourcePreflightRequest(BaseModel):
    """只允许提交一个待测公开网址；前置检测不会写入台账。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_url: str = Field(..., min_length=1, max_length=2_000)


class CreatePublicSourceSupplementRequest(PublicSourcePreflightRequest):
    """检测并加入来源时的最小业务输入；说明可空，由服务器补默认审计文案。"""

    note: str | None = Field(default=None, max_length=1_000)


class SetPublicSourceSupplementStatusRequest(BaseModel):
    """来源启停只接受固定状态；重新启用会由服务器重新检测原网址。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    new_status: Literal["active", "disabled"]


def _raise_public_source_error(exc: Exception, *, operation: str) -> None:
    """统一返回不泄露服务器路径、连接对端或内部网络细节的业务错误。"""

    if isinstance(exc, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, OSError):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"无法{operation}",
        ) from exc
    raise exc


@router.get("", status_code=status.HTTP_200_OK)
async def list_xuzhou_public_source_supplements(request: Request) -> dict[str, Any]:
    """读取团队已登记的补充公开来源及其上次检测结果；读取不触发网页访问。"""

    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {"ok": True, **list_public_source_supplement_status(ledger_dir)}
    except (OSError, RuntimeError, ValueError) as exc:
        _raise_public_source_error(exc, operation="读取补充公开来源")
    raise RuntimeError("unreachable")


@router.post("/preflight", status_code=status.HTTP_200_OK)
async def preflight_xuzhou_public_source(
    body: PublicSourcePreflightRequest,
    request: Request,
) -> dict[str, Any]:
    """仅检测一个公开网址是否适合机器直接抓取，不保存该网址或修改任何目录。"""

    # 先检查团队权限，避免未授权访客把接口用作网络探测器。检测函数本身也只会访问一个
    # 已通过严格 URL 校验、robots 允许且同域重定向受限的公开网页入口。
    shared_ledger_dir_for_request(request)
    get_current_principal(request)
    return {"ok": True, "preflight": preflight_public_source(body.source_url)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_xuzhou_public_source_supplement(
    body: CreatePublicSourceSupplementRequest,
    request: Request,
) -> dict[str, Any]:
    """再次检测并登记一条可直接抓取的公开来源；失败网址不会写入团队目录。"""

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = add_public_source_supplement(
            ledger_dir,
            source_url=body.source_url,
            note=body.note,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        _raise_public_source_error(exc, operation="检测并登记公开来源")
    raise RuntimeError("unreachable")


@router.post("/{source_id}/retest", status_code=status.HTTP_200_OK)
async def retest_xuzhou_public_source_supplement(
    source_id: str,
    request: Request,
) -> dict[str, Any]:
    """重新检测已登记网址；不会接受浏览器换网址，也不会自动写入证据或结论。"""

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = retest_public_source_supplement(
            ledger_dir,
            source_id=source_id,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        _raise_public_source_error(exc, operation="重新检测公开来源")
    raise RuntimeError("unreachable")


@router.post("/{source_id}/status", status_code=status.HTTP_200_OK)
async def set_xuzhou_public_source_supplement_status(
    source_id: str,
    body: SetPublicSourceSupplementStatusRequest,
    request: Request,
) -> dict[str, Any]:
    """暂停无需联网；启用时服务器必须重测原网址的公开访问与 robots 规则。"""

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = set_public_source_supplement_status(
            ledger_dir,
            source_id=source_id,
            new_status=body.new_status,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        _raise_public_source_error(exc, operation="更新公开来源状态")
    raise RuntimeError("unreachable")
