"""可信来源目录的已认证普通用户 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .shared_workspace import shared_ledger_dir_for_request
from .source_catalog import (
    list_trusted_source_audit,
    list_trusted_sources,
    register_trusted_source,
    set_trusted_source_status,
)


router = APIRouter()


class RegisterTrustedSourceRequest(BaseModel):
    """普通用户登记可信域名时允许提交的业务字段。

    请求模型禁止目录 ID、创建时间、创建人、状态审计、证据或评分字段。可信目录的
    创建人只能来自服务器认证身份，目录状态和审计事件由核心服务确定，从而防止模型
    或前端伪造责任归属，或借来源配置请求触及已有证据链。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    domain: str = Field(..., min_length=1, max_length=253)
    source_type: str = Field(..., min_length=1, max_length=100)
    source_level: str = Field(..., min_length=1, max_length=100)
    evidence_quality: int
    allowed_claim_types: list[str] = Field(..., min_length=1)
    change_reason: str = Field(..., min_length=1, max_length=2_000)


class SetTrustedSourceStatusRequest(BaseModel):
    """普通用户启用或停用一个可信来源目录项时允许提交的业务字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    new_status: str = Field(..., min_length=1, max_length=100)
    change_reason: str = Field(..., min_length=1, max_length=2_000)


@router.get("", status_code=status.HTTP_200_OK)
async def list_xuzhou_trusted_sources(
    request: Request,
    active_only: bool = False,
) -> dict[str, Any]:
    """读取团队共享台账中的可信来源目录，不要求管理员且不修改任何数据。

    输入为已认证 HTTP 请求及可选活动状态筛选，输出为目录副本和台账路径。服务器先
    从认证中间件读取当前身份，再经共享目录守卫确认其为管理员或名册成员，非成员一律
    403；全团队查看同一份共享台账。函数不接受或返回可改写原始证据的字段，也不会执行
    搜索、抓取、评分、预警或目录变更。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = list_trusted_sources(ledger_dir, active_only=active_only)
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法读取可信来源目录",
        ) from exc


@router.get("/audit", status_code=status.HTTP_200_OK)
async def list_xuzhou_trusted_source_audit(
    request: Request,
    limit: int = 100,
) -> dict[str, Any]:
    """读取可信来源目录的追加式变更审计，供普通团队成员追溯而不改任何记录。

    输入是认证请求和受限展示上限，输出为服务器已保存的登记、替代和状态变更审计。调用者
    先经共享工作区守卫验证为管理员或团队名册成员，普通业务用户不因非管理员被拒绝；审计
    中的操作人只来自历史服务器认证记录，客户端无法筛选、伪造或覆盖。接口不接受来源 URL、
    状态、证据、评分、复核或用户 ID 写入字段，只读函数还会先验证台账完整性，因此查看不
    会触发抓取、变更来源目录，也不会改写正式 evidence 的 URL、摘录、采集时间或哈希。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {"ok": True, **list_trusted_source_audit(ledger_dir, limit=limit)}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法读取可信来源配置审计",
        ) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_xuzhou_trusted_source(
    body: RegisterTrustedSourceRequest,
    request: Request,
) -> dict[str, Any]:
    """由已获名册授权的普通登录用户登记可信公开域名，目录替换全程保留审计且不改历史证据。

    输入是受限业务配置与请求，输出为新生效目录项和台账路径。操作者只取认证后的
    ``principal.user_id``，并经共享目录守卫确认其为管理员或名册成员，非成员一律 403；
    无需管理员角色。核心服务会限制类型可支持的等级与事实类别，同域旧配置仅标记为
    已替代并追加审计，绝不改写线索、来源 URL、原始摘录、采集时间、内容哈希、评分或
    人工复核记录。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = register_trusted_source(
            ledger_dir,
            domain=body.domain,
            source_type=body.source_type,
            source_level=body.source_level,
            evidence_quality=body.evidence_quality,
            allowed_claim_types=body.allowed_claim_types,
            change_reason=body.change_reason,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法保存可信来源目录",
        ) from exc


@router.post("/{source_id}/status", status_code=status.HTTP_200_OK)
async def set_xuzhou_trusted_source_status(
    source_id: str,
    body: SetTrustedSourceStatusRequest,
    request: Request,
) -> dict[str, Any]:
    """由已获名册授权的普通登录用户启用或停用可信目录项，并保留不可覆盖的配置审计记录。

    输入是目录 ID、受限状态和变更原因；输出为更新后的目录项。操作人严格来自认证
    主体，并经共享目录守卫确认其为管理员或名册成员，非成员一律 403。核心服务只变更
    未来页面的来源分级资格并追加审计，不会重写已入库证据的历史等级、来源链接、内容
    快照、红色预警或人工业务状态。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = set_trusted_source_status(
            ledger_dir,
            source_id=source_id,
            new_status=body.new_status,
            change_reason=body.change_reason,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法更新可信来源目录",
        ) from exc
